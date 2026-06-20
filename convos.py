"""Command-center conversation memory: record what the owner says to Vic, call out the
weak spots automatically, and learn from confirmed corrections.

This is the logic layer over db's assistant_* tables. It is deliberately separate from
assistant.py to avoid an import cycle: assistant.run consults a learning hook that app.py
wires to convos.lookup at boot (so the router can honor a taught correction without
importing this module).

Honest scope: "learning" is a per-tenant correction memory (phrase -> answer / page / tool),
not model training. Deterministic, tenant-isolated, reversible.
"""
import json
import re
import threading

import compliance
import db

# Owner-pushback cues (normalized, no punctuation) -> a 'negative' flag.
_NEGATIVE = ("no that", "thats not", "not what i meant", "didnt work", "doesnt work",
             "that is wrong", "not right", "try again", "nope", "that is not", "not it",
             "you misunderstood", "wrong answer")


def _norm(s):
    """Lowercase, strip punctuation, collapse whitespace -- the matching form."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def _similar(a, b):
    """Jaccard word overlap >= 0.6 (or exact) -- 'did you mean the same thing again'."""
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return False
    return len(sa & sb) / len(sa | sb) >= 0.6


# ---- Record + flag ----
def record_exchange(business_id, session_key, message, result, browser_key=None, convo_id=None):
    """Log the owner's message and Vic's reply (with tool/status), then flag the weak
    spots. Also kick off an LLM grade in the background (it catches subtler misses than the
    heuristics). When the result showed records (leads/appointments), remember them on the
    assistant turn so a later "text her back" resolves. Returns (convo_id, user_turn_id)."""
    convo_id = convo_id or db.start_or_get_convo(business_id, session_key, browser_key)
    meta = (result or {}).get("meta") or {}
    reply = (result or {}).get("reply", "")
    prior = db.recent_user_turns(convo_id, business_id, limit=6)  # before this turn
    user_tid = db.log_turn(convo_id, business_id, "user", message)
    asst_tid = db.log_turn(convo_id, business_id, "assistant", reply,
                           tool=meta.get("tool"), status=meta.get("status"))
    db.record_turn_entities(business_id, convo_id, asst_tid, (result or {}).get("entities"))
    # When Vic pointed the owner to a page (a capability gap), remember WHICH page, so a
    # later proactive offer can crystallize that exact route into a learning.
    gap_route = ""
    if meta.get("status") == "capability_gap":
        for c in (result or {}).get("cards", []):
            if c.get("type") == "link" and c.get("href"):
                gap_route = c["href"]
                break
    _flag(business_id, convo_id, user_tid, message, meta, prior, gap_route)
    _spawn_grader(business_id, convo_id, user_tid, message, reply, meta)
    return convo_id, user_tid


def _flag(business_id, convo_id, user_tid, message, meta, prior, gap_route=""):
    """Call out the issues in this exchange: a capability gap, an empty result, the owner
    pushing back, or re-asking the same thing."""
    status = meta.get("status")
    nmsg = _norm(message)
    if status == "capability_gap":
        db.add_flag(business_id, convo_id, user_tid, "capability_gap",
                    ("route:" + gap_route) if gap_route
                    else "Vic had no tool for this and pointed elsewhere.")
    elif status == "empty":
        db.add_flag(business_id, convo_id, user_tid, "empty",
                    "A tool ran but came back with nothing.")
    if any(k in nmsg for k in _NEGATIVE):
        db.add_flag(business_id, convo_id, user_tid, "negative",
                    "The owner pushed back on the previous answer.")
    for p in prior:
        if _similar(nmsg, _norm(p)):
            db.add_flag(business_id, convo_id, user_tid, "repeat",
                        "The owner re-asked something very similar.")
            break


# ---- LLM grading: catch the subtle misses the heuristics pass over ----
# We only grade turns that already LOOK fine (a real reply, not a confirm prompt or a
# heuristic-flagged gap), so the grade adds signal instead of repeating it.
_GRADED_STATUSES = ("ok", "chat", "learned")


def _complete(provider, system, user):
    """One completion call, portable across both apps' ai modules."""
    import ai
    if hasattr(ai, "_llm_complete"):           # FirstBack-style ai
        return ai._llm_complete(provider, system, user)
    fn = ai._claude_complete if provider == "claude" else ai._minimax_complete
    return fn(system, user)


def _grade(message, reply):
    """Ask the brain to judge the exchange. Returns {'verdict','reason'} or None (no real
    brain configured, or the call failed -> we simply don't grade)."""
    import ai
    provider = ai._active_provider()
    if provider not in ("claude", "minimax"):
        return None
    system = (
        "You are a strict QA reviewer for an assistant that helps a home-services "
        "contractor run their business. Given the OWNER's message and the ASSISTANT's "
        "reply, judge whether the assistant ACTUALLY answered the question or did the "
        "thing the owner wanted, versus dodging, giving a generic non-answer, or missing "
        "the real intent. Respond with ONLY a JSON object: "
        '{"verdict":"good|weak|miss","reason":"<one short sentence>"}. '
        "good = it answered or acted on the real ask; weak = partial, vague, or made the "
        "owner do the work; miss = it did not address what they actually wanted. Do not use "
        "dashes; use periods and commas.")
    user = f"OWNER: {message}\nASSISTANT: {reply}\n\nReturn the JSON now."
    try:
        raw = ai._strip_think(_complete(provider, system, user))
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else None
        if isinstance(data, dict) and data.get("verdict") in ("good", "weak", "miss"):
            return data
    except Exception:
        return None
    return None


def grade_exchange(business_id, convo_id, user_turn_id, message, reply, meta):
    """Grade one exchange; on a weak/miss verdict, add an 'unhelpful' flag carrying the
    brain's reason. Synchronous + testable. Returns the verdict dict (or None)."""
    if (meta or {}).get("status") not in _GRADED_STATUSES:
        return None
    if not reply or message.startswith("[confirmed"):
        return None
    verdict = _grade(message, reply)
    if not verdict:
        return None
    if verdict.get("verdict") in ("weak", "miss"):
        db.add_flag(business_id, convo_id, user_turn_id, "unhelpful",
                    (verdict.get("reason") or "")[:300])
    return verdict


def _spawn_grader(business_id, convo_id, user_turn_id, message, reply, meta):
    """Run the grade off the request path so it never slows the chat reply."""
    if (meta or {}).get("status") not in _GRADED_STATUSES or not reply:
        return
    if message.startswith("[confirmed"):
        return

    def _work():
        try:
            grade_exchange(business_id, convo_id, user_turn_id, message, reply, meta)
        except Exception:
            pass
    threading.Thread(target=_work, daemon=True).start()


# ---- Proactive teaching: at the end of a chat, offer to remember a recurring gap ----
_CLOSING = ("thanks", "thank you", "bye", "goodbye", "thats all", "that is all", "im done",
            "done for now", "appreciate it", "all set", "nothing else", "goodnight",
            "good night", "that will be all", "cheers")


def _is_closing(message):
    t = _norm(message)
    return bool(t) and len(t.split()) <= 6 and any(c in t for c in _CLOSING)


def coach_offer(business_id, convo_id, message):
    """At a natural end-of-conversation moment, if a capability gap has RECURRED, return a
    one-tap offer for Vic to remember the route he already takes for it (or None). He
    offers at most once per conversation, and never for something already taught."""
    if not convo_id or db.has_coach_offer(business_id, convo_id):
        return None
    if not (_is_closing(message) or db.convo_user_turn_count(business_id, convo_id) >= 3):
        return None
    groups = {}
    for content, route in db.coach_candidates(business_id):
        key = _norm(content)
        if not key:
            continue
        g = groups.setdefault(key, {"sample": content, "route": route, "count": 0, "norm": key})
        g["count"] += 1
    taught = {_norm(L["pattern"]) for L in db.list_learnings(business_id, confirmed_only=True)}
    cands = [g for g in groups.values() if g["count"] >= 2 and g["norm"] not in taught]
    if not cands:
        return None
    top = sorted(cands, key=lambda x: -x["count"])[0]
    db.mark_coach_offered(business_id, convo_id)
    # If the brain is confident an existing TOOL would actually satisfy this, offer to run
    # it (a real upgrade); otherwise offer to remember the route Vic already takes.
    tool = None
    suggest = globals().get("_tool_suggest_hook")
    if suggest:
        try:
            tool = suggest(top["sample"])
        except Exception:
            tool = None
    if tool:
        return {
            "pattern": top["sample"], "action": tool, "value": "", "count": top["count"],
            "prompt": (f'I noticed you have asked things like "{top["sample"]}" {top["count"]} '
                       f'times. I think I can actually do that now. Want me to run "{tool}" '
                       "whenever you say it."),
        }
    return {
        "pattern": top["sample"], "action": "route", "value": top["route"],
        "count": top["count"],
        "prompt": (f'I noticed you have asked things like "{top["sample"]}" {top["count"]} '
                   f"times and I could not do it directly, I just pointed you to {top['route']}. "
                   "Want me to remember that and take you straight there whenever you say it."),
    }


def accept_coach(business_id, pattern, action, value=""):
    """The owner accepted a proactive offer: store the learning and resolve the matching
    open gap flags so they stop showing as issues."""
    teach(business_id, pattern, action, answer=value)
    key = _norm(pattern)
    for f in db.list_flags(business_id, resolved=0, limit=200):
        if f.get("kind") in ("capability_gap", "unhelpful") and \
                _similar(_norm(f.get("turn_content") or ""), key):
            db.resolve_flag(business_id, f["id"])


# ---- Learn ----
def teach(business_id, pattern, action, answer="", source_turn_id=None):
    """Record a confirmed correction: 'when I say <pattern>, do <action>'. action is a tool
    name, 'route' (answer = href), or 'answer' (answer = canned reply)."""
    return db.add_learning(business_id, pattern, action, answer=answer,
                           source_turn_id=source_turn_id, confirmed=1)


def lookup(business, message):
    """A confirmed learning matching this message, as a directive for assistant.run, or
    None. For a tool action returns {'_run_tool': name}; for route/answer a full result.
    Accepts a business dict (from the assistant hook) or a bare id."""
    business_id = business["id"] if isinstance(business, dict) else business
    nmsg = _norm(message)
    for L in db.list_learnings(business_id, confirmed_only=True):
        pat = _norm(L["pattern"])
        if pat and (pat in nmsg or _similar(nmsg, pat)):
            db.bump_learning(business_id, L["id"])
            action = L["action"]
            if action == "answer":
                return {"reply": L["answer"] or "Here you go.", "cards": [],
                        "pending_action": None,
                        "meta": {"tool": "learned", "status": "learned"}}
            if action == "route":
                return {"reply": "Here is where that lives.",
                        "cards": [{"type": "link", "title": "Taught by you",
                                   "href": L["answer"] or "/", "label": "Open"}],
                        "pending_action": None,
                        "meta": {"tool": "learned", "status": "learned"}}
            return {"_run_tool": action}      # a tool name -> assistant runs it
    return None


def digest(business_id, days=7):
    """A short weekly digest for the command center: how often Vic fell short, how much
    you taught him, plus a one-line summary. `has_content` gates whether to show it."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    d = db.memory_digest(business_id, since)
    d["days"] = days
    d["has_content"] = (d["gaps"] + d["repeats"] + d["negatives"] + d["learnings"]) > 0
    bits = []
    if d["gaps"]:
        bits.append(f"{d['gaps']} thing{'s' if d['gaps'] != 1 else ''} I could not do")
    if d["repeats"]:
        bits.append(f"{d['repeats']} repeat ask{'s' if d['repeats'] != 1 else ''}")
    if d["learnings"]:
        bits.append(f"learned {d['learnings']} new thing{'s' if d['learnings'] != 1 else ''}")
    d["line"] = ("This week: " + ", ".join(bits) + ".") if bits else ""
    return d


def top_unmet(business_id, limit=5):
    """Rank the recurring unmet requests (open capability-gap / unhelpful flags) by how
    often they came up -- the queue of what to teach or build next. Each: {sample, count}."""
    groups = {}
    for content in db.unmet_flag_contents(business_id):
        key = _norm(content)
        if not key:
            continue
        g = groups.setdefault(key, {"sample": content, "count": 0})
        g["count"] += 1
    return sorted(groups.values(), key=lambda x: -x["count"])[:limit]


def _roi_block(business, days):
    """Return an honest ROI paragraph for the digest, or None to omit entirely.

    Only emits a dollar claim when compliance.a2p_ready is True (meaning texts
    actually reached customers). When A2P is pending, returns an honest non-dollar
    line instead. Revenue is always labeled an estimate -- never implies cash collected.
    """
    if compliance.a2p_ready(business):
        try:
            bid = business["id"]
            ana = db.analytics(bid, days)
            totals = ana.get("totals") or {}
            leads = totals.get("leads") or 0
            booked = totals.get("booked") or 0
            revenue = totals.get("revenue") or 0
            roi_multiple = ana.get("roi_multiple")
            avg_source = ana.get("avg_source") or "industry_default"
            source_label = ("your average job value"
                            if avg_source == "owner"
                            else "an industry estimate for your trade")
            roi_str = f"{roi_multiple}x its cost; " if roi_multiple else ""
            return (
                f"This week FirstBack recovered {leads} missed calls and booked "
                f"{booked} estimates -- an estimated ~${revenue:,} "
                f"({roi_str}estimate based on {source_label}). "
                f"That's revenue that would have walked without a text-back."
            )
        except Exception:
            return None
    else:
        return (
            "Your AI is answering calls; texting is still activating -- "
            "ROI tracking starts once texting is live."
        )


def digest_email(business, days=7):
    """Build the weekly digest email ({subject, body}) for a business: the activity summary
    plus the ranked 'build/teach next' list, so the queue reaches the owner's inbox."""
    bid = business["id"]
    d = digest(bid, days)
    unmet = top_unmet(bid)
    name = business.get("name") or "your business"
    lines = [f"Here is your weekly FirstBack digest for {name}.", ""]
    # ROI block -- prepended before the activity summary.
    roi = _roi_block(business, days)
    if roi:
        lines.append(roi)
        lines.append("")
    lines.append(d["line"] or "A quiet week. No gaps and nothing new to teach.")
    lines.append(f"Conversations this week: {d['convos']}. Things I learned: {d['learnings']}.")
    if unmet:
        lines += ["", "Top requests to teach me or build next:"]
        lines += [f'  {i + 1}. "{u["sample"]}" ({u["count"]}x)' for i, u in enumerate(unmet)]
    lines += ["", "Open FirstBack's Memory to teach or review what I missed."]
    return {"subject": f"Your weekly FirstBack digest: {d['gaps']} gap(s), {d['learnings']} learned",
            "body": "\n".join(lines)}


def learnings_for_prompt(business_id, limit=6):
    """Recent confirmed corrections as few-shot lines for the routing prompt, so the brain
    generalizes the owner's phrasing (not just exact matches)."""
    out = []
    for L in db.list_learnings(business_id, confirmed_only=True)[:limit]:
        tgt = L["action"] if L["action"] not in ("answer", "route") else (L["answer"] or L["action"])
        out.append(f'- when the owner says something like "{L["pattern"]}", use {tgt}')
    return "\n".join(out)
