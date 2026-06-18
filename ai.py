"""FirstBack's conversational brain.

Two modes:
  1. Claude mode -- when ANTHROPIC_API_KEY is set AND the `anthropic` package is
     installed. FirstBack texts leads using Claude.
  2. Demo mode   -- a built-in rule-based responder so the product works with
     ZERO setup. Good enough to demo the flow; replace/extend as you like.

>>> THIS FILE IS WHERE THE CONVERSATION LOGIC LIVES. Add your flow here. <<<
"""
import re
from datetime import date

import db
from config import (
    PROVIDER, MINIMAX_API_KEY, MINIMAX_MODEL, MINIMAX_BASE_URL,
    ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_DAILY_COST_CAP_USD,
)
# Provider plumbing (MiniMax/Claude/demo selection + the HTTP/SDK call) lives in the
# trades_core kernel; this file keeps FirstBack's conversation + booking logic.
from llm import active_provider as _active_provider, strip_think as _strip_think, complete as _complete

# ---- Dollar daily cap (Phase 1 cost spine) ----
# Returns True when the tenant is over their daily spend cap and the AI path should
# be paused. Cap of 0 means no cap (always allow). CLAUDE_DAILY_COST_CAP_USD is the
# app-wide default; individual businesses inherit it (per-business cap deferred).
def is_over_daily_cap(business_id):
    """True when this business has exceeded today's dollar daily cap on the AI path."""
    cap = CLAUDE_DAILY_COST_CAP_USD
    if not cap or cap <= 0:
        return False
    return db.get_llm_spend_today(business_id) >= cap


_CAP_SMS_REPLY = (
    "We're resting for a moment. Text us again in a little while and we'll pick "
    "right back up."
)

# Marker the AI emits when it wants to book a slot. We parse it out so the
# product can create a real appointment, then strip it from the visible text.
BOOK_MARKER = re.compile(r"\[\[BOOK:\s*(.+?)\]\]")


def _open_slots(business_id, exclude_ids=None):
    """The soonest open estimate windows from a business's in-house calendar,
    skipping busy/taken days. `exclude_ids` folds in a connected external calendar
    (a set of slot ids to drop). Each item: {day, time, time_key, id, label}."""
    return db.upcoming_slots(business_id, exclude_ids=exclude_ids)


# Words that mean "this job can't wait." Edit this list to fit your trade.
# NOTE: "today" was removed on purpose; it is a scheduling word ("can you come
# today?"), not an emergency, and produced false alarms.
URGENT_KEYWORDS = [
    "flood", "flooding", "burst", "leak", "leaking", "no heat", "no ac",
    "no air", "emergency", "urgent", "asap", "right away",
    "gas", "smoke", "sparking", "no power", "no water", "sewage", "overflow",
]
# Match whole words only, so "gas" no longer fires on "Las Vegas" and "ac" no
# longer fires inside ordinary words.
_URGENT_RES = [re.compile(r"\b" + re.escape(k) + r"\b") for k in URGENT_KEYWORDS]


def detect_urgency(text):
    """True if the message sounds like an emergency that can't wait."""
    t = (text or "").lower()
    return any(p.search(t) for p in _URGENT_RES)


# --------------------------------------------------------------------------
# REAL BRAINS  (MiniMax today, Claude for the public launch)
# --------------------------------------------------------------------------
def _system_prompt(business, slots):
    """Shared system prompt: who the AI is + the rules it follows."""
    if slots:
        slot_lines = "\n".join(f"  - {s['label']}  (id: {s['id']})" for s in slots)
    else:
        slot_lines = "  (no open windows in the next few weeks)"
    return (
        f"{business['ai_instructions']}\n\n"
        f"Business: {business['name']}, {business['trade']}.\n"
        f"Service area: {business['service_area']}. Hours: {business['hours']}.\n"
        f"Available estimate slots you may offer (soonest first):\n{slot_lines}\n\n"
        "RULES:\n"
        "- Tone: professional, clear, and courteous. Write in complete sentences with "
        "correct grammar. Be friendly but not casual. No slang, no filler, no emoji.\n"
        "- Punctuation: use standard punctuation only. Do NOT use dashes of any kind "
        "(no em dashes, no en dashes, no double hyphens). Use periods, commas, and "
        "semicolons instead.\n"
        "- Ask one thing at a time. Do not send a form or a long list.\n"
        "- Answer the caller's real questions (about the company, what you do, hours, "
        "pricing) before guiding them toward scheduling.\n"
        "- Only offer times from the available slots listed above; never invent a time. "
        "When you offer windows, state them in plain words (for example, 'Monday at "
        "9:00 AM'); never show the id to the caller.\n"
        "- To confirm the service area, ask the caller for their address once, phrased "
        "simply (for example, 'What is the address?'). Do not ask for their name or "
        "phone number, and do not request other personal details.\n"
        "- Booking is the goal. As soon as the caller agrees to a specific time, your "
        "entire reply must be a brief, warm confirmation that names the chosen time in "
        "words, followed by a hidden marker on its own final line, in EXACTLY this "
        "format, using the id of the slot the caller actually chose:\n"
        "  [[BOOK: 2026-06-15@14:00]]\n"
        "  Use the id exactly as written next to that slot above. The customer never "
        "sees this marker. It is the only way our system books the appointment; without "
        "it, nothing is booked. After they have agreed, do not ask any further questions."
    )


def _to_turns(history):
    """history [{direction, body}] -> [{role, content}] turns.
    out = our AI = assistant; in = caller = user. Must start on a user turn AND
    alternate roles: the Anthropic API rejects two same-role messages in a row
    (MiniMax tolerates it), so we merge consecutive same-role messages into one."""
    turns = []
    for m in history:
        role = "assistant" if m["direction"] == "out" else "user"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + m["body"]  # merge consecutive same-role
        else:
            turns.append({"role": role, "content": m["body"]})
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "(missed call, no message left)"})
    return turns


def _minimax_reply(business, history, slots):
    """MiniMax via its OpenAI-compatible chat-completions endpoint."""
    return _complete("minimax", _system_prompt(business, slots), _to_turns(history),
                     max_tokens=512, temperature=1.0)


def _claude_reply(business, history, slots, lead_id=None):
    """Call Claude for an SMS reply and log token usage to the ledger."""
    text, usage = _complete("claude", _system_prompt(business, slots), _to_turns(history),
                            max_tokens=300, return_usage=True)
    try:
        db.log_llm_usage(business["id"], "sms", usage.get("model", CLAUDE_MODEL),
                         usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                         usage.get("cost_usd", 0.0), lead_id=lead_id)
    except Exception as _e:
        import sys
        print(f"[firstback] log_llm_usage failed: {_e}", file=sys.stderr, flush=True)
    return text


# --------------------------------------------------------------------------
# DEMO MODE  (no API key needed)
# --------------------------------------------------------------------------
def _demo_reply(business, history, slots):
    """A tiny scripted qualifier so the demo works out of the box.
    Walks: greet -> job type -> area -> offer slots -> book."""
    inbound = [m for m in history if m["direction"] == "in"]
    turns = len(inbound)
    last = inbound[-1]["body"].lower() if inbound else ""
    owner = business["owner_name"]
    offered = slots[:2]

    # Did they just pick one of the windows we offered?
    if turns >= 1 and offered:
        pick = _match_slot(last, offered)
        if pick:
            return (f"Perfect -- you're booked for {pick['label']}. {owner} will "
                    f"come take a look. We'll text a reminder beforehand!\n[[BOOK: {pick['label']}]]")

    if turns == 0:
        return (f"Hi! This is {business['name']} -- sorry we missed your call. "
                "What were you looking to get painted?")
    if turns == 1:
        return ("Got it, thanks! What part of town are you in? "
                "Just making sure you're in our service area.")
    if offered:
        offer = " or ".join(s["label"] for s in offered)
        return (f"You're right in our area. I can get {owner} out for a free estimate "
                f"-- would {offer} work?")
    return ("You're right in our area! What day works best? I'll check the calendar "
            "and get you booked for a free estimate.")


_NEG_RE = re.compile(
    r"\b(?:not|n't|neither|none|no thanks|no thank you|nope|nah|cannot|can't|"
    r"won't|don't|doesn't|isn't|nothing|never)\b")
_AFFIRM_RE = re.compile(
    r"\b(?:yes|yeah|yep|yup|sure|ok|okay|sounds (?:good|great|perfect)|that works|"
    r"works for me|works|great|perfect|book it|let'?s do|go ahead|either|"
    r"that'?s fine|fine)\b")
_ORD_FIRST_RE = re.compile(r"\b(?:first|earliest|earlier|soonest|sooner|1st)\b")
_ORD_SECOND_RE = re.compile(r"\b(?:second|later|latter|2nd)\b")
# A clock time the caller named: bare hour, optional :MM, optional am/pm. The
# digit-boundary lookarounds keep us out of phone numbers and house numbers (a
# multi-digit run never yields a lone hour), and the trailing (?!\w) avoids
# ordinals like "2nd". am/pm is optional because callers say "2 works".
_TIME_MENTION_RE = re.compile(
    r"(?<![\d:])(1[0-2]|0?[1-9])(?::([0-5]\d))?(?:\s*(a\.?m\.?|p\.?m\.?))?(?!\w)",
    re.I)
# When a message looks like an address or phone number, bare hour digits in it
# are house/phone numbers, not a chosen time. Only an am/pm or :MM time counts.
_ADDRESSY_RE = re.compile(
    r"\b(?:st|street|ave|avenue|rd|road|dr|drive|blvd|boulevard|lane|ln|way|"
    r"court|ct|place|pl|apt|unit|suite|ste|floor|fl|phone|cell|number)\b"
    r"|\d[\d\s.\-]{6,}\d")


def _slot_hm(slot):
    """(hour-1-to-12, 'am'|'pm') for a slot, from its canonical time_key."""
    tk = slot.get("time_key") or db.time_key(slot.get("time", ""))
    if not tk:
        return (None, None)
    h24 = int(tk[:2])
    return (h24 % 12 or 12, "am" if h24 < 12 else "pm")


def _time_mentions(text):
    """Clock times the caller named: list of (hour12, 'am'|'pm'|None, qualified,
    negated). `qualified` means am/pm or explicit minutes were given, a strong
    signal it is a real time and not a house number. Negation is scoped to the
    mention's own clause, so 'not 9, do 2pm' negates only the 9."""
    out = []
    for m in _TIME_MENTION_RE.finditer(text):
        mer = (m.group(3) or "").lower()
        mer = "am" if mer.startswith("a") else "pm" if mer.startswith("p") else None
        qualified = bool(mer) or bool(m.group(2))
        clause = re.split(r"[,;]|\bbut\b|\band\b|\bthen\b", text[:m.start()])[-1]
        negated = bool(_NEG_RE.search(clause) or re.search(r"\bno\b", clause))
        out.append((int(m.group(1)), mer, qualified, negated))
    return out


def _explicit_slot(text, offered):
    """A confident match of the caller's words to one offered window, or None.
    Covers slot id, full label, a clock time (with optional day), a day name, and
    'morning'/'afternoon'; deliberately excludes bare affirmatives like 'yes', so
    an explicit time can never be overridden by one. Only ever runs on the
    acceptance turn, where addresses/phones were already collected earlier."""
    if not offered:
        return None
    t = (text or "").lower().strip()
    if not t:
        return None
    # Tier 0: the exact slot id or full label appears verbatim.
    for s in offered:
        if s.get("id", "").lower() in t or s["label"].lower() in t:
            return s
    day_iso = db.parse_day(t)
    # Tier 1: an explicit clock time (the strongest selection signal). A time with
    # am/pm or minutes ("2pm", "2:00") outranks a bare number (which could be a
    # house number); among equals the last non-negated mention wins, so "not 9, do
    # 2pm" books 2 PM. In an addressy message bare numbers are ignored entirely.
    qualified, bare = [], []
    for hour, mer, is_q, negated in _time_mentions(t):
        if negated:
            continue
        for s in offered:
            sh, sm = _slot_hm(s)
            if hour != sh or (mer and mer != sm):
                continue
            if day_iso and s["day"] != day_iso:
                continue
            (qualified if is_q else bare).append(s)
    if qualified:
        return qualified[-1]
    if bare and not _ADDRESSY_RE.search(t):
        return bare[-1]
    # Tier 1b: 'morning' / 'afternoon' / 'evening' when it picks out one window.
    mer_hint = ("am" if re.search(r"\bmorning\b", t)
                else "pm" if re.search(r"\b(?:afternoon|evening)\b", t) else None)
    if mer_hint:
        cand = [s for s in offered if _slot_hm(s)[1] == mer_hint
                and (not day_iso or s["day"] == day_iso)]
        if len(cand) == 1:
            return cand[0]
    # Tier 2: a day name that matches exactly one offered window.
    if day_iso:
        same_day = [s for s in offered if s["day"] == day_iso]
        if len(same_day) == 1:
            return same_day[0]
    # Tier 3: ordinal references to the offered order.
    if _ORD_SECOND_RE.search(t) and len(offered) > 1:
        return offered[1]
    if _ORD_FIRST_RE.search(t):
        return offered[0]
    return None


def _is_affirmative(text):
    """A bare yes with no negation, so 'none of those work' is never a yes."""
    t = (text or "").lower()
    return bool(_AFFIRM_RE.search(t)) and not _NEG_RE.search(t)


def _match_slot(text, offered):
    """Map a customer's reply to one offered window, or None. An explicit choice
    (time, day, or ordinal) always wins; only a bare affirmative falls back to the
    soonest offered window."""
    if not offered:
        return None
    explicit = _explicit_slot(text, offered)
    if explicit:
        return explicit
    if _is_affirmative(text):
        return offered[0]
    return None


# --------------------------------------------------------------------------
# PUBLIC ENTRY POINT
# --------------------------------------------------------------------------
def _offered_slots(history, slots):
    """The open windows we actually presented to the caller, detected from our
    outbound texts (by id, full label, or a time paired with its own weekday).
    Booking is restricted to these, so a stray day word can never book a window we
    never offered."""
    out_text = " ".join(m["body"].lower() for m in history if m["direction"] == "out")
    if not out_text:
        return []
    offered = []
    for s in slots:
        try:
            weekday = date.fromisoformat(s["day"]).strftime("%A").lower()
        except ValueError:
            weekday = ""
        if (s["id"].lower() in out_text or s["label"].lower() in out_text
                or (s["time"].lower() in out_text
                    and (not weekday or weekday in out_text or weekday[:3] in out_text))):
            offered.append(s)
    return offered


def _slot_fallback(history, slots):
    """Safety net for a brain that confirms a time but forgets the [[BOOK]] marker:
    match the caller's last message to a window we actually offered."""
    inbound = [m for m in history if m["direction"] == "in"]
    if not inbound or not slots:
        return None
    offered = _offered_slots(history, slots)
    if not offered:
        return None
    pick = _match_slot(inbound[-1]["body"], offered)
    return pick["label"] if pick else None


def _canonicalize_slot(text, slots):
    """Resolve the model's [[BOOK]] marker to the exact open slot it names, or None.
    Tries the slot id first (what we ask the model to echo), then the full label,
    then a (day, time) match. time_key handles compact spellings like '2pm'."""
    if not text or not slots:
        return None
    tl = text.strip().lower()
    for s in slots:
        if s["id"].lower() == tl or s["id"].lower() in tl:
            return s
    for s in slots:
        if s["label"].lower() in tl:
            return s
    bday = db.parse_day(text)
    tkey = db.time_key(text)
    if tkey:
        cand = [s for s in slots if s["time_key"] == tkey]
        if bday:
            cand = [s for s in cand if s["day"] == bday] or cand
        if cand:
            return cand[0]
    return None


def _canonicalize_booking(booking, slots):
    """Back-compat shim: the exact open-slot label for a marker, else unchanged."""
    slot = _canonicalize_slot(booking, slots)
    return slot["label"] if slot else booking


def _same_slot(a, b):
    return bool(a and b and a["day"] == b["day"] and a["time_key"] == b["time_key"])


def _resolve_booking(history, slots, marker_text):
    """Decide which open slot (if any) to book this turn, reconciled against what
    we actually offered. Returns (slot_or_None, conflict_bool).

    Priority: the caller's explicit choice (a time/day/ordinal in their last
    message) is ground truth and overrides a conflicting model marker; otherwise
    the marker is honored, then a bare affirmative books the soonest offered
    window. Only windows we actually offered are bookable (the marker may name any
    open slot as a last resort)."""
    inbound = [m for m in history if m["direction"] == "in"]
    last = inbound[-1]["body"] if inbound else ""
    offered = _offered_slots(history, slots)
    # Caller text can only book a window we actually offered; if we offered nothing
    # yet, a stray number (e.g. their address) must never book a slot.
    explicit = _explicit_slot(last, offered)
    marker_slot = _canonicalize_slot(marker_text, slots) if marker_text else None
    affirmative = offered[0] if (offered and not explicit and _is_affirmative(last)) else None

    conflict = False
    if explicit:
        chosen = explicit
        conflict = bool(marker_slot and not _same_slot(marker_slot, explicit))
    elif marker_slot and (not offered or marker_slot in offered):
        chosen = marker_slot
    elif affirmative:
        chosen = affirmative
    elif marker_slot:
        chosen = marker_slot  # model insisted on an open slot; honor as last resort
    else:
        chosen = None
    return chosen, conflict


def _clean_punct(text):
    """Brand voice uses standard punctuation only; never dashes. Convert any em
    dash, en dash, or double hyphen to a comma so none reach the customer."""
    text = re.sub(r"\s*[—–]\s*", ", ", text)  # em / en dash -> comma
    text = re.sub(r"\s*--+\s*", ", ", text)             # double hyphen -> comma
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)        # tidy space before punctuation
    text = re.sub(r",\s*,", ", ", text)                 # collapse doubled commas
    text = re.sub(r"\s{2,}", " ", text)                 # collapse double spaces
    return text.strip()


def generate_reply(business, history, exclude_slot_ids=None, lead_id=None):
    """Returns (visible_text, booking_slot_or_None). `exclude_slot_ids` is an
    optional set of slot ids from a connected external calendar to treat as
    unavailable (so the AI never offers a window the contractor is already busy).
    `lead_id` threads the conversation's lead into the LLM usage ledger."""
    # Dollar daily cap gate: degrade honestly rather than silently.
    if is_over_daily_cap(business["id"]):
        return _CAP_SMS_REPLY, None
    slots = _open_slots(business["id"], exclude_ids=exclude_slot_ids)
    provider = _active_provider()
    raw = None
    try:
        if provider == "claude":
            raw = _claude_reply(business, history, slots, lead_id=lead_id)
        elif provider == "minimax":
            raw = _minimax_reply(business, history, slots)
    except Exception as e:
        # Any API/network error -> log and fall back so the app never breaks.
        import sys
        print(f"[firstback] {provider} brain failed, using demo fallback: {e}",
              file=sys.stderr, flush=True)
        raw = None
    if not raw:
        raw = _demo_reply(business, history, slots)

    marker_text = None
    m = BOOK_MARKER.search(raw)
    if m:
        marker_text = m.group(1).strip()
        raw = BOOK_MARKER.sub("", raw).strip()
    # Reconcile the model's marker with what the caller actually accepted and what
    # we actually offered, so we book the agreed window (never slot 0 by accident)
    # and never a window we did not offer.
    slot, conflict = _resolve_booking(history, slots, marker_text)
    if conflict:
        import sys
        print(f"[firstback] booking conflict: marker={marker_text!r} -> honoring "
              f"caller's explicit choice {slot['label']!r}", file=sys.stderr, flush=True)
    booking = slot["label"] if slot else None
    raw = _clean_punct(raw)  # enforce dash-free, standard punctuation
    return raw, booking


def brain_mode():
    return _active_provider()


# --------------------------------------------------------------------------
# LEAD NOTES  (compress a finished conversation into structured notes)
# --------------------------------------------------------------------------
_NOTES_SYSTEM = (
    "You compress a missed-call text conversation into a short lead note for a "
    "home-services contractor. Output ONLY a JSON object (no code fences, no extra "
    "text) with these exact keys: "
    '"name" (the caller\'s name if they gave it, else ""), '
    '"address" (their address or city if stated, else ""), '
    '"project_type" (what they want done, a short phrase, else ""), '
    '"stage" (one of "new", "warm", or "scheduled"; use "scheduled" only if an '
    'appointment was booked, "warm" if they engaged, "new" if barely), '
    '"summary" (one or two plain sentences for the contractor).'
)


def _llm_complete(provider, system, user_text):
    """Single-shot completion for note extraction (low temperature, no reasoning).
    The generous MiniMax budget matters: it still emits a reasoning block despite
    thinking 'disabled', and at 400 it often ran out mid-think before producing the
    JSON (empty notes); 1024 leaves room."""
    if provider == "minimax":
        return _complete("minimax", system, [{"role": "user", "content": user_text}],
                         max_tokens=1024, temperature=0.2)
    if provider == "claude":
        return _complete("claude", system, [{"role": "user", "content": user_text}],
                         max_tokens=400)
    return ""


def _parse_json(raw):
    """Extract the FIRST complete JSON object from a model reply, ignoring any
    surrounding prose or a second object after it. The old greedy {.*} spanned
    from the first { to the last }, so two objects (or trailing junk) produced
    invalid JSON and silently dropped the notes. Returns a dict or None."""
    import json
    s = raw or ""
    dec = json.JSONDecoder()
    start = s.find("{")
    while start != -1:
        try:
            obj, _ = dec.raw_decode(s[start:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = s.find("{", start + 1)
    return None


def _normalize_notes(d):
    stage = str(d.get("stage") or "").strip().lower()
    if stage not in ("new", "warm", "scheduled"):
        stage = "warm"
    return {
        "name": str(d.get("name") or "").strip(),
        "address": str(d.get("address") or "").strip(),
        "project_type": str(d.get("project_type") or "").strip(),
        "stage": stage,
        "summary": str(d.get("summary") or "").strip(),
    }


def _notes_rule_based(messages):
    """Keyless fallback: a rough note pulled from the raw conversation."""
    inbound = [m["body"] for m in messages if m["direction"] == "in"]
    text = " ".join(inbound).lower()
    addr = ""
    m = re.search(r"\d{1,5}\s+[a-z0-9.\s]+?(street|st|ave|avenue|road|rd|drive|dr|"
                  r"lane|ln|blvd|way|court|ct)\b", text)
    if m:
        addr = m.group(0).strip().title()
    elif "downtown" in text:
        addr = "Downtown"
    proj = ""
    for kw in ("kitchen", "cabinet", "interior", "exterior", "bathroom", "deck",
               "fence", "living room", "bedroom", "trim", "ceiling", "whole house"):
        if kw in text:
            proj = kw.title()
            break
    return {"name": "", "address": addr, "project_type": proj,
            "stage": "warm" if inbound else "new",
            "summary": (inbound[0][:140] if inbound else "")}


def summarize_lead(business, messages):
    """Compress a conversation into {name, address, project_type, stage, summary}."""
    if not any(m["direction"] == "in" for m in messages):
        return {}
    transcript = "\n".join(
        ("Caller: " if m["direction"] == "in" else "Assistant: ") + m["body"]
        for m in messages)
    provider = _active_provider()
    if provider in ("minimax", "claude"):
        # MiniMax (a reasoning model) intermittently returns an empty completion,
        # which would otherwise drop us to the rough rule-based note. Retry a few
        # times before giving up; this runs off the hot path, so the extra
        # attempts cost no user-facing latency.
        for attempt in range(4):
            try:
                data = _parse_json(_llm_complete(provider, _NOTES_SYSTEM, transcript))
                if data:
                    return _normalize_notes(data)
            except Exception as e:
                import sys
                print(f"[firstback] summarize_lead ({provider}) attempt {attempt + 1} "
                      f"failed: {e}", file=sys.stderr, flush=True)
    return _notes_rule_based(messages)


# --------------------------------------------------------------------------
# CONTENT SCREEN  (Tier 3 of the phone screen: is this reply a real homeowner,
# or a sales pitch / survey / wrong number / spam? FirstBack's analog to iOS 26
# "Ask Reason for Calling" -- the SMS exchange IS the reason-for-calling step.)
# --------------------------------------------------------------------------
_INTENT_SYSTEM = (
    "You screen the FIRST reply a caller sent to a home-services contractor's "
    "automated text after a missed call. Decide whether this is a genuine potential "
    "CUSTOMER or noise. Output ONLY a JSON object (no code fences, no extra text) "
    'with these exact keys: '
    '"label" (one of "prospect", "sales", "survey", "wrong_number", "spam"), '
    '"is_prospect" (true ONLY for a real homeowner/business asking about or needing '
    'the contractor\'s services; false otherwise), '
    '"confidence" (0.0 to 1.0). '
    "A vendor pitching THE CONTRACTOR (marketing, SEO, leads, insurance, financing) "
    'is "sales". A political/research call is "survey". Someone who clearly dialed '
    'the wrong number is "wrong_number". An obvious robocall/scam is "spam". When in '
    'doubt, prefer "prospect" -- missing a real customer is far worse than engaging a '
    "time-waster.")

# Labels other than these mean "do not keep cold-pitching" (bail).
PROSPECT_LABELS = {"prospect"}


def classify_intent(business, messages):
    """Tier-3 content screen. Returns {"label", "is_prospect", "confidence"} for the
    caller's words so far. FAILS OPEN: the demo brain (no API key) and any error both
    return a confident 'prospect', so screening never silences a real caller when the
    classifier is off or down. Only meaningful once the caller has actually replied."""
    inbound = [m for m in (messages or []) if m.get("direction") == "in" and (m.get("body") or "").strip()]
    if not inbound:
        return {"label": "prospect", "is_prospect": True, "confidence": 0.0}
    provider = _active_provider()
    if provider not in ("minimax", "claude"):
        return {"label": "prospect", "is_prospect": True, "confidence": 0.0}
    transcript = "\n".join(
        ("Caller: " if m["direction"] == "in" else "Assistant: ") + m["body"]
        for m in messages)
    try:
        data = _parse_json(_llm_complete(provider, _INTENT_SYSTEM, transcript))
    except Exception as e:
        import sys
        print(f"[firstback] classify_intent ({provider}) failed: {e}",
              file=sys.stderr, flush=True)
        data = None
    if not data:
        return {"label": "prospect", "is_prospect": True, "confidence": 0.0}
    label = str(data.get("label") or "prospect").strip().lower()
    if label not in ("prospect", "sales", "survey", "wrong_number", "spam"):
        label = "prospect"
    # Trust the explicit label over a possibly-missing is_prospect flag.
    is_prospect = bool(data.get("is_prospect")) if "is_prospect" in data else (label in PROSPECT_LABELS)
    if label not in PROSPECT_LABELS:
        is_prospect = False
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {"label": label, "is_prospect": is_prospect, "confidence": confidence}
