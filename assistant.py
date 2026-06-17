"""RingBack's command center -- the conversational control surface (the "Jarvis").

ONE natural-language seam over the product. The signed-in home is a chat: the owner
types "how many leads this week", "show my booked estimates", "save this number as a
customer", "connect my Google calendar", "text my last lead back" -- and this turns it
into a real action against the existing engines (db, messaging, google_cal). No new
source of truth: every tool wraps a function the manual UI already uses.

Same three guarantees as the rest of RingBack:
  1. Provider-agnostic brain (ai._active_provider): Claude or MiniMax when keyed, with a
     deterministic keyword router as the always-works floor.
  2. Tenant-scoped: every tool runs against the signed-in business; nothing crosses tenants.
  3. Honest + gated: read freely; anything that actually texts a customer is returned as a
     pending_action and only sent after an explicit confirm, still through the gated
     messaging.send_sms seam (opt-outs + simulated-vs-live honored).

run(business, message, history) -> {reply, cards, pending_action}
execute(business, tool, args)   -> {reply, cards}
"""
import hashlib
import json
import re
from datetime import date, datetime, timezone

import db
import ai
import llm
import messaging
import connections
import growth

# Account links the chat can offer, with the words an owner is likely to say.
_CONNECT = {
    "calendar": {"label": "Google Calendar", "href": "/settings",
                 "note": "Sync your real availability so RingBack only offers open times "
                         "and drops booked estimates onto your calendar.",
                 "aliases": ["calendar", "google calendar", "gcal", "schedule", "availability"]},
    "contacts": {"label": "your contacts", "href": "/callers",
                 "note": "Import your address book so RingBack knows a customer from a stranger.",
                 "aliases": ["contacts", "address book", "import contacts", "google contacts"]},
    "texting": {"label": "texting (Twilio)", "href": "/settings",
                "note": "Provision a number so RingBack texts missed callers for real.",
                "aliases": ["twilio", "texting", "text", "phone number", "sms", "number"]},
}


def _text(body):
    return {"type": "text", "body": body}


def _note(body, tone="info"):
    return {"type": "note", "body": body, "tone": tone}


def _link(title, href, label, note=""):
    return {"type": "link", "title": title, "href": href, "label": label, "note": note}


# --------------------------------------------------------------------------
# TOOL HANDLERS
# --------------------------------------------------------------------------
def _h_get_stats(business, args):
    bid = business["id"]
    a = db.analytics(bid, days=None)["totals"]
    leads = db.leads_with_stage(bid)
    warm = sum(1 for l in leads if l["stage"] == "warm")
    new = sum(1 for l in leads if l["stage"] == "new")
    appts = db.list_appointments(bid)
    review = db.count_pending_suggestions(bid)
    rev = (f"${a['revenue']:,}" if a.get("revenue") else "set a job value")
    card = {"type": "stat", "title": "Where things stand", "groups": [
        {"label": "Leads", "value": a["leads"], "sub": f"{new} not yet replied"},
        {"label": "Booked", "value": a["booked"], "sub": f"{a['conversion']}% conversion"},
        {"label": "Need chasing", "value": warm, "sub": "warm, awaiting you"},
        {"label": "Upcoming estimates", "value": len(appts), "sub": "on the calendar"},
        {"label": "To review", "value": review, "sub": "caller suggestions"},
        {"label": "Est. revenue", "value": rev, "sub": "booked x avg job value"},
    ]}
    return {"reply": "Here is the current picture. These are running totals to date, not a "
                     "7-day slice, since that is the number I can stand behind.",
            "cards": [card]}


def _h_list_leads(business, args):
    leads = db.leads_with_stage(business["id"])[:8]
    if not leads:
        return {"reply": "No leads yet. The moment a call is missed, the lead lands here.",
                "cards": []}
    val = _job_value(business)
    items = []
    for l in leads:
        bits = [("urgent · " if l.get("urgent") else "") + l["stage"]]
        age = _age_label(l.get("created_at"))
        if age:
            bits.append(age)
        if val and l["stage"] != "scheduled":          # dollars on the open ones
            bits.append(f"~${val:,}")
        items.append({"title": (l.get("name") or l.get("phone") or "Lead"),
                      "sub": " · ".join(bits)})
    # Lead with money: open leads x avg job value = what is on the table right now.
    open_n = sum(1 for l in leads if l["stage"] != "scheduled")
    if val and open_n:
        reply = (f"{len(leads)} leads, hottest first. {open_n} still open, that is about "
                 f"${val * open_n:,} on the table.")
    else:
        reply = f"Your {len(leads)} most pressing leads, hottest first."
    return {"reply": reply, "cards": [{"type": "list", "title": "Leads", "items": items}],
            "entities": _lead_entities(leads)}


def _h_list_appointments(business, args):
    appts = db.list_appointments(business["id"])[:8]
    if not appts:
        return {"reply": "No estimates are booked yet.", "cards": []}
    items = [{"title": (a.get("lead_name") or a.get("lead_phone") or "Estimate"),
              "sub": (a.get("scheduled_for") or a.get("slot_label") or "booked")}
             for a in appts]
    ents = [{"kind": "appointment", "id": a.get("id"),
             "name": a.get("lead_name") or "", "phone": a.get("lead_phone") or "",
             "ordinal": i + 1} for i, a in enumerate(appts)]
    return {"reply": f"You have {len(appts)} booked estimate(s).",
            "cards": [{"type": "list", "title": "Booked estimates", "items": items}],
            "entities": ents}


def _lead_entities(leads):
    """Referenceable records for the leads a turn just showed, in display order -- the
    context a later "text her back" / "the second one" resolves against."""
    return [{"kind": "lead", "id": l["id"], "name": l.get("name") or "",
             "phone": l.get("phone") or "", "ordinal": i + 1}
            for i, l in enumerate(leads)]


def _job_value(business):
    """The owner's average job value as an int (0 when unset) -- used to frame leads in
    dollars, the way a foreman thinks about the pile."""
    try:
        return int(business.get("avg_job_value") or 0)
    except (ValueError, TypeError):
        return 0


def _age_label(created_at):
    """How long a lead has been waiting, in plain words ('12m ago', '3h ago', '2d ago')."""
    try:
        dt = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _h_add_contact(business, args):
    number = (args.get("phone") or "").strip()
    name = (args.get("name") or "").strip()
    category = (args.get("category") or "customer").strip().lower()
    if not number:
        return {"reply": "Give me a phone number and I will save them to your directory.",
                "cards": []}
    key = db.set_contact(business["id"], number, category, name=name or None)
    if not key:
        return {"reply": "That did not look like a valid phone number. Try a 10-digit number.",
                "cards": []}
    who = name or number
    return {"reply": f"Saved {who} as a {category}.",
            "cards": [_note(f"{who} is in your directory now. RingBack will treat their "
                            "calls accordingly.", "ok")]}


def _h_connect(business, args):
    prov = (args.get("provider") or "").strip().lower()
    if prov not in _CONNECT:
        prov = _match_provider(prov) or _match_provider(args.get("raw", ""))
    if not prov:
        return {"reply": "I can connect your Google Calendar, your contacts, or texting "
                         "(Twilio). Which one?",
                "cards": [_link("Open settings", "/settings", "Open settings")]}
    c = _CONNECT[prov]
    return {"reply": f"Let's connect {c['label']}. Open it and I will walk you through it.",
            "cards": [_link(f"Connect {c['label']}", c["href"], f"Connect {c['label']}",
                            note=c["note"])]}


def _h_import_contacts(business, args):
    return {"reply": "Bring your address book in from the caller inbox and I will sort who "
                     "is a customer from who is a stranger.",
            "cards": [_link("Import contacts", "/callers", "Open the caller inbox")]}


def _resolve_lead_target(business, args):
    """The lead an action (text / book / cancel / flag) will act on: a resolved `_lead_id`
    (from a referent like "her" / "the second one") wins; else a name/phone match; else the
    most recent lead. None when the tenant has no leads. Single source of truth so the confirm
    preview and the actual action always agree on who."""
    leads = db.leads_with_stage(business["id"])
    if not leads:
        return None
    lid = args.get("_lead_id")
    if lid:
        for l in leads:
            if l["id"] == lid:
                return l
    name = (args.get("name") or "").strip().lower()
    if name:
        for l in leads:
            if name in (l.get("name") or "").lower() or name in (l.get("phone") or ""):
                return l
    return max(leads, key=lambda l: l["id"])   # most recent


def _who(lead):
    return lead.get("name") or lead.get("phone") or "that lead"


def _text_preview(business, args):
    """Exactly what a text_lead confirm will send -- recipient (name + number), the
    verbatim body, and the live/test/opt-out mode -- so the owner never approves a blind
    send. None when there's no resolvable target or no body to send yet."""
    target = _resolve_lead_target(business, args)
    body = (args.get("message") or "").strip()
    if not target or not body:
        return None
    phone = target.get("phone", "")
    return {"recipient_name": target.get("name") or "",
            "recipient_phone": phone,
            "body": body,
            "mode": messaging.outbound_mode(business, phone)}


def _h_text_lead(business, args):
    """CONFIRM tool. Text a lead a short message through the gated messaging seam (opt-outs
    and simulated-vs-live honored). Resolves the named lead, else the most recent one."""
    target = _resolve_lead_target(business, args)
    if target is None:
        return {"reply": "There are no leads to text yet.", "cards": []}
    body = (args.get("message") or "").strip()
    if not body:
        return {"reply": f"What should I text {target.get('name') or target.get('phone')}?",
                "cards": []}
    res = messaging.send_sms(business, target.get("phone", ""), body, lead_id=target["id"])
    status = res.get("status")
    msg = {"sent": f"Texted {target.get('name') or target.get('phone')}.",
           "simulated": f"Prepared the text to {target.get('name') or target.get('phone')} "
                        "(simulated until Twilio is connected).",
           "suppressed": "That contact has opted out, so nothing was sent.",
           "skipped": "I could not send that (no usable number or empty message).",
           "error": "Twilio rejected the send, so nothing went out."}.get(status, "Done.")
    # Only a real send is green ("ok"). "simulated" is honest-neutral (no success tint) so the
    # post-confirm note never visually contradicts the "Test mode -- not sent" badge.
    tone = "ok" if status == "sent" else ("warn" if status in ("suppressed", "skipped", "error")
                                          else "")
    return {"reply": msg, "cards": [_note(msg, tone)]}


# --------------------------------------------------------------------------
# BOOKING: the core verb -- show open windows, book / cancel an estimate, flag urgent
# --------------------------------------------------------------------------
def _resolve_slot(business, text):
    """The open estimate window an owner means from a phrase ("Thursday at 2", "tomorrow"),
    else the soonest open one. None when nothing is open. The confirm always shows the slot
    that was actually chosen, so a loose match is caught before it books."""
    slots = db.upcoming_slots(business["id"], limit=8)
    if not slots:
        return None
    t = (text or "").lower()
    want_wd = next((v for k, v in _DAYNAME.items() if re.search(r"\b" + k + r"\b", t)), None)
    times = _parse_times(text or "")
    want_tk = db.time_key(times[0]) if times else None
    for s in slots:
        try:
            wd = date.fromisoformat(s["day"]).weekday()
        except (ValueError, TypeError, KeyError):
            wd = None
        if want_wd is not None and wd != want_wd:
            continue
        if want_tk and s.get("time_key") != want_tk:
            continue
        return s
    return slots[0]                                  # soonest open window


def _pin_slot(args, slot):
    """Pin the chosen slot onto args so execute() books exactly what the confirm showed."""
    args["_slot_id"] = slot.get("id")
    args["_slot_label"] = slot.get("label")
    args["_slot_day"] = slot.get("day")
    args["_slot_time"] = slot.get("time_key")


def _h_list_slots(business, args):
    slots = db.upcoming_slots(business["id"], limit=8)
    if not slots:
        return {"reply": "No open estimate windows right now. Check your scheduling, or block "
                         "fewer days.", "cards": []}
    items = [{"title": s["label"], "sub": "open"} for s in slots]
    return {"reply": f"Your next {len(slots)} open estimate windows. Say something like \"book "
                     "Maria for Thursday at 10\" and I will hold it.",
            "cards": [{"type": "list", "title": "Open windows", "items": items}]}


def _h_book_estimate(business, args):
    """CONFIRM tool. Hold an open window for a lead (the slot + lead were pinned at confirm
    time, so this books exactly what the owner saw)."""
    lead = _resolve_lead_target(business, args)
    if lead is None:
        return {"reply": "Which lead should I book? Tell me a name, or list your leads and say "
                         "\"book the first one.\"", "cards": []}
    if args.get("_slot_id"):
        slot = {"id": args["_slot_id"], "label": args.get("_slot_label"),
                "day": args.get("_slot_day"), "time_key": args.get("_slot_time")}
    else:
        slot = _resolve_slot(business, args.get("raw", ""))
    if not slot:
        return {"reply": "You have no open estimate windows right now.", "cards": []}
    ok = db.book_appointment(business["id"], lead["id"], slot["label"],
                             day=slot.get("day"), slot_time=slot.get("time_key"))
    who = _who(lead)
    if not ok:
        return {"reply": f"That window just filled. Pick another and I will hold it.",
                "cards": [_note(f"{slot['label']} is taken. Ask me for your open windows.",
                                "warn")]}
    # Auto-pause the chase sequence: once they book, stop nudging them to book.
    db.cancel_lead_growth_touches(lead["id"], ("quote_followup", "reactivation"))
    first = (lead.get("name") or "them").split()[0]
    return {"reply": f"Booked {who} for {slot['label']}.",
            "cards": [_note(f"{who} is on the calendar for {slot['label']}. Want me to text "
                            f"them the details? Say \"text {first} the time.\"", "ok")]}


def _h_cancel_estimate(business, args):
    """CONFIRM tool. Cancel a lead's booked estimate (appointment pinned at confirm time)."""
    lead = _resolve_lead_target(business, args)
    if lead is None:
        return {"reply": "Whose estimate should I cancel? Tell me a name.", "cards": []}
    appt_id = args.get("_appt_id")
    if not appt_id:
        appts = db.lead_booked_appointments(business["id"], lead["id"])
        appt_id = appts[0]["id"] if appts else None
    if not appt_id:
        return {"reply": f"{_who(lead)} has no booked estimate to cancel.", "cards": []}
    row = db.cancel_appointment(business["id"], appt_id)
    if not row:
        return {"reply": "That estimate was already canceled.", "cards": []}
    when = row.get("scheduled_for") or "the booked time"
    return {"reply": f"Canceled {_who(lead)}'s estimate ({when}). The window is open again.",
            "cards": [_note(f"{_who(lead)} is back to a lead. Want me to text them to reschedule?",
                            "ok")]}


def _h_flag_urgent(business, args):
    """Flag a lead as urgent so it sorts to the top of the pile. Low-stakes, not gated."""
    lead = _resolve_lead_target(business, args)
    if lead is None:
        return {"reply": "Which lead is urgent? Tell me a name.", "cards": []}
    db.mark_lead_urgent(lead["id"], business["id"])
    return {"reply": f"Flagged {_who(lead)} as urgent. They will sit at the top until handled.",
            "cards": [_note(f"{_who(lead)} is marked urgent.", "ok")]}


def _search_query_from(text):
    """Strip the question words off a lookup so just the name/number is left: 'whats
    John's number' -> 'John', 'look up the Maple St job' -> 'Maple St job'."""
    t = (text or "").strip()
    t = re.sub(r"^(find|look ?up|search( for)?|pull up|whats|what's|who is|who's|"
               r"do i have (a )?number for|number for)\s+", "", t, flags=re.I)
    t = re.sub(r"(['’]s)?\s+(number|phone|info|details|contact)\s*\??$", "", t, flags=re.I)
    return t.strip(" ?.")


def _h_find_lead(business, args):
    """Read tool: find a lead by name or number. Results are referenceable, so the owner can
    follow with "text the first one." """
    q = (args.get("query") or "").strip() or _search_query_from(args.get("raw", ""))
    if not q:
        return {"reply": "Who are you looking for? Give me a name or a number.", "cards": []}
    rows = db.search_leads(business["id"], q, limit=8)
    if not rows:
        return {"reply": f"No lead matches \"{q}\" yet. If they only ever called, they will land "
                         "here the next time.", "cards": []}
    items = [{"title": (r.get("name") or r.get("phone") or "Lead"),
              "sub": (r.get("phone") or "")
                     + ((" · " + r["project_type"]) if r.get("project_type") else "")}
             for r in rows]
    return {"reply": f"{len(rows)} match{'es' if len(rows) != 1 else ''} for \"{q}\".",
            "cards": [{"type": "list", "title": "Search results", "items": items}],
            "entities": _lead_entities(rows)}


# --------------------------------------------------------------------------
# THE MORNING BRIEFING -- proactive, money-ranked, foreman voice (Phase 2)
# "He opens his phone and the day is already sorted." One card, read in 12 seconds:
# what needs him right now, dollars first, one action each. Pure over signals that
# already exist (leads_with_stage / list_appointments / avg_job_value / golive), so it
# never invents a customer and never claims more than the data shows.
# --------------------------------------------------------------------------
def _briefing_who(lead):
    """A name we can stand behind, else the number -- never a guessed name."""
    return (lead.get("name") or "").strip() or lead.get("phone") or "a caller"


def _first_name(lead):
    who = _briefing_who(lead)
    if not who or who == "a caller":
        return "them"                      # nameless AND numberless: grammatical, no guess
    return who.split()[0] if who[0].isalpha() else who   # a phone number stays whole


def _compose_briefing(business):
    """The briefing card for `business`: a headline that frames the day in dollars and a
    short ranked list of what needs him now (urgent, then warm leads waiting, then today's
    estimates, then untouched new leads). Honest when it's quiet or there's nothing yet."""
    bid = business["id"]
    leads = db.leads_with_stage(bid)
    appts = db.list_appointments(bid)
    val = _job_value(business)

    urgent = [l for l in leads if l.get("urgent") and l["stage"] != "scheduled"]
    warm = [l for l in leads if l["stage"] == "warm" and not l.get("urgent")]
    new = [l for l in leads if l["stage"] == "new" and not l.get("urgent")]
    open_leads = [l for l in leads if l["stage"] != "scheduled"]

    # Cold start: don't fake a busy day. Say what's true and what would change it.
    if not leads and not appts:
        return {"type": "briefing", "tone": "quiet",
                "headline": "Nothing waiting yet.",
                "sub": "The moment a call gets missed, the lead lands here and I line it up.",
                "items": []}

    # Headline: lead with money, then capacity. Only show dollars when a job value is set.
    money = f"${val * len(open_leads):,}" if (val and open_leads) else ""
    if open_leads and money:
        headline = f"{len(open_leads)} leads open, about {money} on the table."
    elif open_leads:
        headline = f"{len(open_leads)} leads need you."
    elif appts:
        headline = "Nothing waiting. Your booked estimates are below."
    else:
        headline = "All caught up. No open leads right now."
    sub = (f"{len(appts)} estimate{'s' if len(appts) != 1 else ''} booked."
           if appts else "Nothing on the calendar yet.")

    # `label` is a screen-reader status word (the colored dot alone must never carry
    # meaning -- WCAG 1.4.1). The caps (top 2 urgent / 2 warm) keep the card to a
    # 12-second read; the rest is a tap away in the pipeline.
    items = []
    for l in urgent[:2]:
        age = _age_label(l.get("created_at"))
        bits = [f"~${val:,}"] if val else []      # money first, everywhere
        if age:
            bits.append(age)
        items.append({"title": f"Call {_first_name(l)} back now",
                      "sub": " · ".join(bits) or "flagged urgent",
                      "tone": "warn", "label": "Urgent",
                      "action": f"text {_first_name(l)} back"})
    for l in warm[:2]:
        age = _age_label(l.get("created_at"))
        bits = [f"~${val:,}"] if val else []
        if age:
            bits.append(age)
        bits.append("replied, waiting")
        items.append({"title": f"Text {_first_name(l)} back",
                      "sub": " · ".join(bits), "tone": "hot", "label": "Replied",
                      "action": f"text {_first_name(l)} back"})
    if appts:
        a = appts[0]
        first = _first_name({"name": a.get("lead_name") or "",
                             "phone": a.get("lead_phone") or ""})
        when = a.get("scheduled_for") or a.get("slot_label") or "booked"
        extra = f" (+{len(appts) - 1} more)" if len(appts) > 1 else ""
        # The Win (BRAIN §8): a booked estimate is money on the calendar -- state it flat,
        # dollars first, no celebration. "~$1,850 booked" beats a confetti burst.
        items.append({"title": f"Estimate: {first} {when}{extra}",
                      "sub": (f"~${val:,} booked" if val else "on the calendar"),
                      "tone": "ok", "label": "Booked",
                      "action": "show my booked estimates"})
    if new:
        if len(new) == 1:
            n = new[0]
            age = _age_label(n.get("created_at"))
            items.append({"title": f"New lead: text {_first_name(n)} back",
                          "sub": (f"~${val:,} · " if val else "")
                                 + (f"{age}, no reply" if age else "no reply yet"),
                          "tone": "new", "label": "New lead",
                          "action": f"text {_first_name(n)} back"})
        else:
            items.append({"title": f"{len(new)} new leads to text back",
                          "sub": (f"~${val * len(new):,} on the table" if val
                                  else "no reply yet"),
                          "tone": "new", "label": "New leads",
                          "action": "show my leads"})
    return {"type": "briefing", "tone": "active", "headline": headline,
            "sub": sub, "items": items}


def briefing(business):
    """Public: the briefing card dict for the dashboard route to server-render on load."""
    return _compose_briefing(business)


def briefing_signature(card):
    """A short stable hash of a briefing's content, so the real-time poll only re-renders
    the feed when something actually changed (a new call, a booking, a reply)."""
    parts = [card.get("headline", ""), card.get("sub", "")]
    for it in card.get("items", []):
        parts.append(f"{it.get('title','')}|{it.get('sub','')}|{it.get('tone','')}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _h_briefing(business, args):
    """Read tool: the morning briefing, summonable by chat ('what should I focus on?')."""
    card = _compose_briefing(business)
    if card.get("tone") == "quiet":
        reply = "Quiet so far. Nothing's waiting on you yet."
    elif card["items"]:
        reply = card["headline"] + " Start at the top."
    else:
        reply = card["headline"]
    return {"reply": reply, "cards": [card]}


def adaptive_suggestions(business):
    """Chip suggestions tuned to the tenant's live state (Phase 2), replacing the five
    static ones: surface the next real action (text the warm lead, book the new one, finish
    go-live) instead of generic examples. Pure over existing signals; never empty."""
    bid = business["id"]
    leads = db.leads_with_stage(bid)
    # Exclude urgent leads from the lead chips: the briefing already calls them out as
    # "Call X back now", so a "Text X back"/"Book X" chip would contradict it. Match
    # _compose_briefing's warm/new buckets exactly.
    warm = [l for l in leads if l["stage"] == "warm" and not l.get("urgent")]
    new = [l for l in leads if l["stage"] == "new" and not l.get("urgent")]
    try:
        live = connections.golive_summary(business).get("live_verified")
    except Exception:
        live = True
    try:
        money = growth.money_left_behind(business)["total"]
    except Exception:
        money = 0
    # Priority order, highest first. Go-live sits ABOVE the generic stats chip so the money
    # chip can never evict it. The money chip is a command (reveals the receipts on tap),
    # not a scare. De-duped, capped at 5; the briefing is always first.
    ordered = ["What should I focus on?"]
    if money > 0:
        ordered.append(f"~${money:,} on the table — show me")
    if warm:
        ordered.append(f"Text {_first_name(warm[0])} back")
    if new and db.upcoming_slots(bid, limit=1):
        ordered.append(f"Book {_first_name(new[0])} for an estimate")
    if not live:
        ordered.append("Finish setting me up to go live")
    ordered.append("How many leads came in this week?")     # matches suggestions() -> de-dups
    ordered += suggestions()
    seen, chips = set(), []
    for c in ordered:
        if c not in seen:
            seen.add(c); chips.append(c)
        if len(chips) >= 5:
            break
    return chips


# --------------------------------------------------------------------------
# THE GROWTH ENGINE surface (Phase 3) -- read tools over growth.plays. A play surfaces
# as a tappable briefing row whose action routes to the gated text confirm (recipient +
# body + opt-out + live/test), so nothing reaches a customer the owner didn't approve.
# --------------------------------------------------------------------------
def _growth_items(plays, limit=5):
    """growth.plays() opportunities -> briefing-shaped tappable items (reuses the briefing
    renderer, so no new card type). Money leads the sub line."""
    items = []
    for p in plays[:limit]:
        sub = " · ".join(b for b in (p.get("money_label", ""), p.get("why", "")) if b)
        items.append({"title": p["title"], "sub": sub, "tone": p["tone"],
                      "label": p["label"], "action": p["action"]})
    return items


def _h_growth_plays(business, args):
    """Read tool: the money-ranked growth plays (reviews, follow-ups, reactivations,
    win-backs, referrals). Each is one tap to a gated draft -- the owner approves."""
    ps = growth.plays(business)
    if not ps:
        return {"reply": "Nothing to chase yet. As jobs wrap and quotes go quiet, I'll line "
                         "the plays up here.", "cards": []}
    mlb = growth.money_left_behind(business)
    card = {"type": "briefing", "tone": "active", "headline": mlb["headline"],
            "sub": "Tap one and I'll draft the text. You approve before anything sends.",
            "items": _growth_items(ps)}
    return {"reply": mlb["headline"], "cards": [card]}


def _h_money_left_behind(business, args):
    """Read tool: the forensic dollar total of growth opportunities, split convert vs grow."""
    mlb = growth.money_left_behind(business)
    card = {"type": "stat", "title": "Growth plays", "groups": [
        {"label": "On the table", "value": f"${mlb['total']:,}",
         "sub": f"{mlb['play_count']} play{'s' if mlb['play_count'] != 1 else ''}"},
        {"label": "Convert", "value": f"${mlb['by_tier']['convert']:,}",
         "sub": "reviews + follow-ups"},
        {"label": "Grow", "value": f"${mlb['by_tier']['grow']:,}",
         "sub": "win-backs + campaigns"},
    ]}
    return {"reply": mlb["headline"], "cards": [card]}


_DAYNUM = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}


def _days_label(working):
    days = sorted(working)
    if days == [0, 1, 2, 3, 4, 5, 6]:
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "Mon to Fri"
    if days == [0, 1, 2, 3, 4, 5]:
        return "Mon to Sat"
    return ", ".join(_DAYNUM[d] for d in days) or "no days set"


def _buf_label(mins):
    if not mins:
        return "no buffer"
    if mins % 60 == 0:
        h = mins // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{mins} minutes"


def _h_scheduling(business, args):
    """Read tool: show the owner's current scheduling and how to change it."""
    p = db.scheduling_prefs(business["id"])
    items = [
        {"title": "Estimate windows", "sub": ", ".join(p["times"])},
        {"title": "Working days", "sub": _days_label(p["working_days"])},
        {"title": "Buffer between estimates", "sub": _buf_label(p["buffer_minutes"])},
    ]
    return {"reply": "Here is how your scheduling is set. Tell me things like \"put a 90 "
                     "minute buffer between estimates,\" \"offer estimates at 10am and 2pm,\" "
                     "or \"only book Monday through Friday,\" and I will update it.",
            "cards": [{"type": "list", "title": "Your scheduling", "items": items},
                      _link("Open scheduling settings", "/settings", "Edit in Settings")]}


def _h_set_scheduling(business, args):
    """CONFIRM tool: apply scheduling changes (buffer / windows / working days)."""
    bid = business["id"]
    changed = []
    buf = args.get("buffer_minutes")
    times = args.get("times")
    days = args.get("working_days")
    if buf is not None:
        db.set_scheduling_prefs(bid, buffer_minutes=buf)
        changed.append(f"buffer set to {_buf_label(int(buf))}")
    if times:
        db.set_scheduling_prefs(bid, times=times)
        changed.append("estimate windows updated to " + ", ".join(times))
    if days:
        db.set_scheduling_prefs(bid, working_days=days)
        changed.append("working days set to "
                       + _days_label({int(d) for d in days}))
    if not changed:
        return _h_scheduling(business, args)
    p = db.scheduling_prefs(bid)
    return {"reply": "Done. " + "; ".join(changed) + ".",
            "cards": [_note("Updated. The AI will offer "
                            + ", ".join(p["times"]) + " on " + _days_label(p["working_days"])
                            + (", never booking two estimates within "
                               + _buf_label(p["buffer_minutes"]) + " of each other"
                               if p["buffer_minutes"] else "") + ".", "ok")]}


TOOLS = {
    "briefing":          {"fn": _h_briefing, "confirm": False,
                          "desc": "The morning briefing: what needs the owner's attention right "
                                  "now, money first. CALL THIS when the owner asks what to focus "
                                  "on, where to start, what's going on, or for their day/rundown.",
                          "params": []},
    "get_stats":         {"fn": _h_get_stats, "confirm": False,
                          "desc": "Show current numbers: leads, booked estimates, conversion, revenue.",
                          "params": []},
    "growth_plays":      {"fn": _h_growth_plays, "confirm": False,
                          "desc": "Show money-making growth plays: review requests, quote "
                                  "follow-ups, reactivations, win-backs, referrals. CALL THIS "
                                  "when the owner asks what to send, what plays they have, how "
                                  "to grow, or to chase reviews or follow-ups.",
                          "params": []},
    "money_left_behind": {"fn": _h_money_left_behind, "confirm": False,
                          "desc": "Show the dollar total of revenue opportunities on the table "
                                  "now (reviews, follow-ups, win-backs). CALL THIS for 'money "
                                  "left behind' or 'what am I leaving on the table'.",
                          "params": []},
    "list_leads":        {"fn": _h_list_leads, "confirm": False,
                          "desc": "List the leads that need attention, triage order.", "params": []},
    "list_appointments": {"fn": _h_list_appointments, "confirm": False,
                          "desc": "Show booked estimates.", "params": []},
    "add_contact":       {"fn": _h_add_contact, "confirm": False,
                          "desc": "Save a phone number to the directory (customer, prospect, vendor, personal, blocked).",
                          "params": ["name", "phone", "category"]},
    "connect":           {"fn": _h_connect, "confirm": False,
                          "desc": "Connect Google Calendar, contacts, or texting (Twilio).",
                          "params": ["provider"]},
    "import_contacts":   {"fn": _h_import_contacts, "confirm": False,
                          "desc": "Import an address book from the caller inbox.", "params": []},
    "text_lead":         {"fn": _h_text_lead, "confirm": True,
                          "desc": "Text a lead a short message. CALL THIS whenever the owner "
                                  "wants to text, message, reply to, or follow up with a lead. "
                                  "Do not just say you will text them -- call the tool.",
                          "params": ["name", "message"]},
    "list_slots":        {"fn": _h_list_slots, "confirm": False,
                          "desc": "Show the next open estimate windows the owner can book.",
                          "params": []},
    "book_estimate":     {"fn": _h_book_estimate, "confirm": True,
                          "desc": "Book an estimate for a lead at an open window (name + when).",
                          "params": ["name", "when"]},
    "cancel_estimate":   {"fn": _h_cancel_estimate, "confirm": True,
                          "desc": "Cancel a lead's booked estimate.", "params": ["name"]},
    "flag_urgent":       {"fn": _h_flag_urgent, "confirm": False,
                          "desc": "Flag a lead as urgent so it sorts to the top.",
                          "params": ["name"]},
    "find_lead":         {"fn": _h_find_lead, "confirm": False,
                          "desc": "Find a lead by name or phone number.", "params": ["query"]},
    "scheduling":        {"fn": _h_scheduling, "confirm": False,
                          "desc": "Show the owner's scheduling: estimate windows, working days, buffer between estimates.",
                          "params": []},
    "set_scheduling":    {"fn": _h_set_scheduling, "confirm": True,
                          "desc": "Change scheduling: buffer_minutes (min gap between estimates), times (estimate windows), working_days (weekday ints 0=Mon..6=Sun).",
                          "params": ["buffer_minutes", "times", "working_days"]},
}


def _confirm_summary(tool, args):
    """One human sentence describing a gated action before it runs."""
    if tool == "set_scheduling":
        parts = []
        if args.get("buffer_minutes") is not None:
            parts.append(f"keep estimates at least {_buf_label(int(args['buffer_minutes']))} apart")
        if args.get("times"):
            parts.append("offer estimates at " + ", ".join(args["times"]))
        if args.get("working_days"):
            parts.append("only book on " + _days_label({int(d) for d in args["working_days"]}))
        return ("Update your scheduling to " + "; ".join(parts) + ".") if parts \
            else "Update your scheduling."
    return {
        "text_lead": "Send this text to the lead, through the gated messaging seam.",
    }.get(tool, "Run this action.")


# --------------------------------------------------------------------------
# MATCHERS
# --------------------------------------------------------------------------
def _match_provider(text):
    t = (text or "").lower()
    for prov, c in _CONNECT.items():
        if any(a in t for a in c["aliases"]):
            return prov
    return None


# ---- Referent resolution: "text her back", "the second one" -> a concrete record ----
_ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
            "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}
_PRONOUN = ("her", "him", "them", "they")
_DEMONSTRATIVE = ("that one", "that lead", "that caller", "this one", "the one")
_LASTISH = ("the last one", "last one", "most recent", "the latest", "latest one")


def _is_referential(message):
    """True when the owner is pointing at something already shown rather than naming it."""
    t = " " + (message or "").lower() + " "
    if any(" " + w + " " in t for w in _PRONOUN):
        return True
    return any(k in t for k in _DEMONSTRATIVE + _LASTISH + tuple(_ORDINAL))


def _resolve_referent(message, entities):
    """The record the owner means ("her", "the second one", "the last one") from what was
    just shown. None when there's nothing to resolve. A bare pronoun with several records
    resolves to the one the list led with -- and the honest confirm shows who, so a wrong
    guess is caught before anything sends."""
    if not entities:
        return None
    t = " " + (message or "").lower() + " "
    by_ord = {e.get("ordinal"): e for e in entities if e.get("ordinal")}
    for word, n in _ORDINAL.items():
        if " " + word + " " in t and n in by_ord:
            return by_ord[n]
    if any(k in t for k in _LASTISH):
        return max(entities, key=lambda e: e.get("ordinal") or 0)
    if any(" " + w + " " in t for w in _PRONOUN) or any(k in t for k in _DEMONSTRATIVE):
        return entities[0] if len(entities) == 1 else (by_ord.get(1) or entities[0])
    return None


def _apply_referent(message, args, entities):
    """When a text_lead message points at a shown record, pin the concrete recipient onto
    args so the send and the confirm preview both target the right person."""
    ent = _resolve_referent(message, entities)
    if not ent:
        return
    if ent.get("kind") == "lead" and ent.get("id"):
        args["_lead_id"] = ent["id"]
    if ent.get("name") or ent.get("phone"):
        args["name"] = ent.get("name") or ent.get("phone")


_PHONE_RE = re.compile(r"(\+?\d[\d\-\.\s\(\)]{6,}\d)")

_DAYNAME = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
            "wed": 2, "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4,
            "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_TIME_RE = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?", re.I)


def _parse_buffer(t):
    """Minutes of buffer from natural language, or None. '2 hours'->120, '90 min'->90."""
    m = re.search(r"(\d+)\s*(?:hour|hr|h)\b", t)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*(?:minute|min|m)\b", t)
    if m:
        return int(m.group(1))
    return None


def _parse_times(t):
    """Estimate windows mentioned, as canonical labels ('10:00 AM'), or None."""
    out = []
    for m in _TIME_RE.finditer(t):
        h, mm = m.group(1), (m.group(2) or "00")
        out.append(f"{int(h)}:{mm} {'AM' if m.group(3).lower() == 'a' else 'PM'}")
    return out or None


def _parse_working_days(t):
    """A weekday-int list from natural language, or None."""
    t = t.lower()
    if "weekday" in t or "mon-fri" in t or "monday to friday" in t or "monday through friday" in t \
            or "no weekend" in t or "without weekend" in t:
        return [0, 1, 2, 3, 4]
    if "every day" in t or "all week" in t or "seven day" in t or "7 day" in t:
        return [0, 1, 2, 3, 4, 5, 6]
    found = sorted({v for k, v in _DAYNAME.items() if re.search(r"\b" + k + r"\b", t)})
    return found or None


# --------------------------------------------------------------------------
# THE BRAIN  (LLM tool-routing with a deterministic floor)
# --------------------------------------------------------------------------
def _tool_catalog():
    lines = []
    for name, spec in TOOLS.items():
        p = (" params: " + ", ".join(spec["params"])) if spec["params"] else ""
        lines.append(f"- {name}: {spec['desc']}{p}")
    return "\n".join(lines)


def _learning_examples(business):
    """Few-shot lines from this tenant's confirmed corrections (hook wired by the app)."""
    fn = globals().get("_learning_examples_hook")
    try:
        return fn(business["id"]) if (fn and business) else ""
    except Exception:
        return ""


_MAX_TOOL_STEPS = 4


def _tool_schemas():
    """The TOOLS registry as provider-neutral tool-use schemas (params typed as strings;
    the handlers coerce). Built from the same dict the keyword floor and confirm gate use."""
    return [{"name": name, "description": spec["desc"],
             "input_schema": {"type": "object",
                              "properties": {p: {"type": "string"} for p in spec["params"]},
                              "required": []}}
            for name, spec in TOOLS.items()]


# The Vic persona -- the stance the brain speaks from (BRAIN.md S2). A chatbot asks what
# you want; Vic tells you what to do and why. Woven into every LLM reply path so the voice
# is one voice. (Deterministic routing and the confirm gate are unchanged; this only shapes
# the words.)
_VIC_PERSONA = (
    "You are Vic, the AI marketing employee inside RingBack -- not a chatbot, a sharp foreman "
    "who knows marketing cold. Voice: blue-collar, short sentences, plain words. Never use "
    "corporate words (no leverage, optimize, utilize, synergy). Lead with money and capacity: "
    "turn leads into dollars and open estimate slots, never funnel jargon. Own the "
    "recommendation -- when there is a right answer, say it, do not list five options. Never "
    "perform: no Great question, no hype, no exclamation marks, no emoji, no streaks. Never "
    "make up a customer detail; if you do not know a name, say the caller from that number. Be "
    "honest about what is working and what is not. No dashes; use periods and commas."
)


def _loop_system(business=None):
    taught = _learning_examples(business) if business else ""
    taught_block = ("\nThe owner has TAUGHT you these corrections; honor them:\n" + taught
                    + "\n") if taught else ""
    return (
        _VIC_PERSONA + " RingBack catches a home-services contractor's missed calls and books "
        "estimates by text. Help the owner by CALLING the available tools to pull their "
        "numbers, list leads or booked estimates, save a contact, connect an account, change "
        "scheduling, or text a lead. You can call more than one tool in sequence when a "
        "request needs it. Use tools rather than guessing, and never invent data. When the "
        "owner wants to text, book, cancel, flag, or change a setting, you MUST call the "
        "matching tool -- never just say you will do it without calling it. Scheduling: if the "
        "owner wants estimates spaced out or not back to back, call set_scheduling with a "
        "sensible buffer_minutes (90 if unsure). When you have what you need, reply in one or "
        "two short sentences." + taught_block)


def _tool_loop(business, message, history, entities=None, allow_llm=True):
    """The LLM brain as a multi-step tool-use loop (Claude/MiniMax): the model calls tools,
    we run the read ones and feed results back, and STOP at the first write tool (text_lead /
    set_scheduling) to return it as a pending_action for explicit confirm -- the gate is never
    bypassed. Returns a full run() result, or None to fall back to the keyword floor (no key,
    any error, or allow_llm=False when a tenant has spent its daily LLM budget)."""
    if not allow_llm:
        return None
    provider = ai._active_provider()
    if provider not in ("claude", "minimax"):
        return None
    msgs = []
    for turn in (history or [])[-6:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        msgs.append({"role": role, "content": str(turn.get("content", ""))})
    msgs.append({"role": "user", "content": message})
    tools = _tool_schemas()
    cards, ents_out, last_text = [], None, ""
    try:
        for _ in range(_MAX_TOOL_STEPS):
            res = llm.tool_complete(provider, _loop_system(business), msgs, tools)
            last_text = res.get("text") or last_text
            calls = res.get("tool_calls") or []
            if not calls:
                # Pure-chat turn: keep capability honesty -- route a known topic to its real
                # page rather than dead-ending it, exactly like the keyword path does.
                if not cards:
                    routed = _route_topic(message, business)
                    if routed:
                        return {"reply": routed["reply"], "cards": routed["cards"],
                                "pending_action": None,
                                "meta": {"tool": "route", "status": "capability_gap"}}
                break
            msgs.append({"role": "assistant", "content": res.get("text") or "",
                         "tool_calls": calls})
            for tc in calls:
                name, args = tc.get("name"), dict(tc.get("input") or {})
                spec = TOOLS.get(name)
                if not spec:
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": "No such tool."})
                    continue
                if spec["confirm"]:
                    if name in _LEAD_TOOLS and entities and _is_referential(message):
                        _apply_referent(message, args, entities)
                    g = _gated(business, name, args, message,
                               res.get("text") or _CONFIRM_PROMPT)
                    if cards:                       # keep any read-tool cards from this turn
                        g["cards"] = cards + g.get("cards", [])
                    if ents_out and not g.get("entities"):
                        g["entities"] = ents_out
                    return g
                out = spec["fn"](business, args)
                cards += out.get("cards", [])
                if out.get("entities"):
                    ents_out = out["entities"]
                msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "content": out.get("reply", "Done.")})
        reply = last_text or _chat_reply(message)
        return {"reply": reply, "cards": cards, "pending_action": None, "entities": ents_out,
                "meta": {"tool": "tools", "status": "ok" if cards else "chat"}}
    except Exception as e:
        print(f"[ringback] tool loop failed, using keyword floor: {e}", flush=True)
        return None


def suggest_tool_for(message):
    """Given a request the assistant fell back on (a recurring gap), ask the brain whether
    ONE existing tool would genuinely satisfy it. Returns a tool name only on high
    confidence, else None. Powers the proactive 'I think I can actually do that now' offer."""
    provider = ai._active_provider()
    if provider not in ("claude", "minimax"):
        return None
    system = (
        "An assistant could not handle a request and fell back to pointing the owner at a "
        "page. Decide if ONE of the existing tools below would ACTUALLY do what the owner "
        "asked. Be conservative: only name a tool if you are confident it FULLY satisfies "
        "the request; otherwise say none.\nTOOLS:\n" + _tool_catalog() + "\n"
        'Reply with ONLY a JSON object: {"tool":"<exact tool name or none>","confidence":"high|low"}.')
    user = f"OWNER REQUEST: {message}\n\nReturn the JSON now."
    try:
        raw = ai._strip_think(ai._llm_complete(provider, system, user))
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
        if d.get("tool") in TOOLS and d.get("confidence") == "high":
            return d["tool"]
    except Exception:
        return None
    return None


def _demo_route(message):
    t = message.lower().strip()
    args = {"raw": message}
    # The morning briefing -- "what should I do", "where do I start", "catch me up".
    if any(k in t for k in ("briefing", "my day", "what should i", "what do i do",
                            "where do i start", "where should i start", "catch me up",
                            "rundown", "run down", "the plan", "focus on", "what do i focus",
                            "good morning", "whats going on", "what's going on")):
        return "briefing", args
    # Growth engine (Phase 3): money left behind + the plays feed.
    if any(k in t for k in ("money left", "left on the table", "left behind",
                            "leaving on the table", "leaving money")):
        return "money_left_behind", args
    if any(k in t for k in ("plays", "grow my business", "grow the business", "how do i grow",
                            "what can i send", "win back", "winback",
                            "win-back", "reactivate", "follow ups", "follow-ups",
                            "review request", "get reviews", "ask for reviews",
                            "chase reviews", "drum up", "more business")):
        return "growth_plays", args
    if any(k in t for k in ("how many", "stats", "numbers", "how are we", "conversion",
                            "revenue", "this week", "booked how", "leads this")):
        if "list" in t and "lead" in t:
            return "list_leads", args
        return "get_stats", args

    times_found = _parse_times(message)

    # --- Booking the core verb (must beat the scheduling/appointments branches below) ---
    if any(k in t for k in ("open slot", "open window", "openings", "what's open", "whats open",
                            "what's available", "whats available", "available to book", "to book",
                            "free window", "free slot", "when can i book", "what do i have open",
                            "show me slots", "show slots", "my openings")):
        return "list_slots", args
    if "cancel" in t and any(k in t for k in ("estimate", "appointment", "appt", "booking")):
        nm = re.search(r"cancel\s+(?:the\s+)?([a-z]+)", t)
        if nm and nm.group(1) not in ("the", "a", "my", "this", "that", "their", "estimate",
                                      "appointment", "appt", "booking"):
            args["name"] = nm.group(1)
        return "cancel_estimate", args
    _sched_set = ("weekday" in t or "only" in t or "to friday" in t or "through friday" in t
                  or "i work" in t or "work " in t or "no weekend" in t)
    if (re.search(r"\bbook\b", t) or ("schedule" in t and " for " in t)) and not _sched_set:
        nm = re.search(r"(?:book|schedule)\s+(?:in\s+)?([a-z]+)", t)
        if nm and nm.group(1) not in ("the", "a", "an", "my", "this", "that", "them", "for",
                                      "me", "us", "in", "her", "him"):
            args["name"] = nm.group(1)
        return "book_estimate", args
    if any(k in t for k in ("urgent", "asap", "rush", "priority", "important")) and \
            any(k in t for k in ("lead", "them", "him", "her", "this", "mark", "flag", "make")):
        nm = re.search(r"(?:flag|mark)\s+([a-z]+)", t)
        if nm and nm.group(1) not in ("the", "a", "my", "this", "that", "them", "as", "it"):
            args["name"] = nm.group(1)
        return "flag_urgent", args

    # --- Search / lookup (guard against "number OF leads", which is a stats-ish phrasing) ---
    if (any(k in t for k in ("look up", "lookup", "search for", "pull up", "whose number",
                             "number for")) or re.match(r"find\s+\w", t)
            or re.search(r"[a-z]+['’]s (number|phone)", t)):
        args["query"] = _search_query_from(message)
        return "find_lead", args

    # --- Scheduling (must run before the appointments branch: "estimate"/"buffer" overlap) ---
    buffer_intent = (any(k in t for k in (
        "buffer", "back to back", "back-to-back", "too close", "that close",
        "close together", "spread out", "gap between", "space between", "space out"))
        or (times_found and len(times_found) >= 2
            and any(k in t for k in ("both", "never", "make", "close", "don't want", "dont want"))))
    if buffer_intent:
        args["buffer_minutes"] = _parse_buffer(t) or 90
        return "set_scheduling", args
    wd = _parse_working_days(t)
    if wd and any(k in t for k in ("book", "schedul", "estimate", "work", "available",
                                   "only", "day")):
        args["working_days"] = wd
        return "set_scheduling", args
    if times_found and any(k in t for k in ("offer", "estimate", "available", "book at",
                                            "window", "slot", "my times", "set my")):
        args["times"] = times_found
        return "set_scheduling", args
    if any(k in t for k in ("schedul", "availability", "my hours", "my windows",
                            "what times", "what days", "customize my")):
        return "scheduling", args

    if any(k in t for k in ("appointment", "estimate", "booked", "calendar today",
                            "what's booked", "schedule")) and "connect" not in t:
        return "list_appointments", args
    if any(k in t for k in ("connect", "link", "hook up", "sync", "integrat")):
        args["provider"] = _match_provider(t) or ""
        return "connect", args
    if "import" in t and "contact" in t:
        return "import_contacts", args
    if any(k in t for k in ("text", "message", "reply to", "follow up", "follow-up")) and (
            "lead" in t or "them" in t or "him" in t or "her" in t or "back" in t):
        m = re.search(r"(?:saying|say|that|:)\s+(.*)$", message, flags=re.I)
        if m:
            args["message"] = m.group(1).strip()
        nm = re.search(r"text\s+([a-z]+)", t)
        if nm and nm.group(1) not in ("my", "the", "a", "this", "last", "them", "back"):
            args["name"] = nm.group(1)
        return "text_lead", args
    if any(k in t for k in ("save", "store", "add")) and (_PHONE_RE.search(message) or "contact" in t or "number" in t):
        ph = _PHONE_RE.search(message)
        if ph:
            args["phone"] = ph.group(1).strip()
        head = re.split(r"(\+?\d)", message)[0]
        args["name"] = re.sub(r"^(save|store|add|new)\s+(contact\s+|number\s+)?", "", head,
                              flags=re.I).strip(" ,")
        for cat in ("customer", "prospect", "vendor", "personal", "blocked"):
            if cat in t:
                args["category"] = cat
        return "add_contact", args
    if "lead" in t and ("list" in t or "show" in t or "who" in t):
        return "list_leads", args
    return "chat", args


_GOLIVE_REPLY = {
    "not_live": "Let's get you live. Here's where you are. The Go Live page walks you through "
                "your number, carrier registration, and call forwarding, one step at a time.",
    "setup_complete": "You're set up. Make a test call to your RingBack number to confirm "
                      "forwarding works, then you're fully live.",
    "live": "You're live. RingBack is catching your missed calls and texting them back.",
}


def _golive_card(business):
    """A go-live status card for the command center: a condensed stepper + the top blocker,
    deep-linking to the wizard. Reuses connections.golive_summary (single source of truth) so
    it can never claim 'live' before the wizard would."""
    g = connections.golive_summary(business)
    card = {"type": "golive", "status": g["status"], "done": g["done"], "total": g["total"],
            "steps": g["steps"], "blocker": g["blocker"],
            "href": "/setup", "label": "Open Go Live"}
    return {"reply": _GOLIVE_REPLY[g["status"]], "cards": [card]}


def _route_topic(message, business=None):
    """Capability honesty: for a request with no direct tool, route to the nearest real
    page instead of dead-ending it as a 'feature request'. Returns {reply, cards} or None.
    When `business` is given, the go-live route returns a live status card; otherwise it
    falls back to a plain link card (e.g. direct unit calls without a tenant)."""
    t = message.lower()
    if any(k in t for k in ("go live", "make it live", "turn it on", "get connected",
                            "connect my number", "set up my number", "set up ringback",
                            "get a number", "get a phone number", "a2p", "10dlc", "carrier",
                            "call forwarding", "forward my calls", "deliverability",
                            "start texting", "not sending text", "texts aren't",
                            "isn't texting", "not texting", "won't text", "can't text")):
        if business is not None:
            return _golive_card(business)
        return {"reply": "Let's get you live. The Go Live page walks you through your number, "
                         "carrier registration, and call forwarding, one step at a time.",
                "cards": [_link("Open Go Live", "/setup", "Open Go Live")]}
    if any(k in t for k in ("password", "my account", "profile", "ai instruction", "what it says",
                            "my hours", "service area", "business name", "change my", "alert",
                            "reminder")):
        return {"reply": "You can change that in Settings, including your AI instructions, hours, "
                         "alerts, and reminders.",
                "cards": [_link("Open settings", "/settings", "Open settings")]}
    if any(k in t for k in ("demo", "simulator", "try it", "test it", "see it work", "show me how")):
        return {"reply": "Run a live demo: fire a missed call and watch RingBack text the caller "
                         "and book the estimate.",
                "cards": [_link("Open the simulator", "/simulator", "Open the demo")]}
    return None


def _chat_reply(message):
    provider = ai._active_provider()
    if provider in ("claude", "minimax"):
        try:
            sys = (_VIC_PERSONA + " RingBack catches a contractor's missed calls and books "
                   "estimates. Answer in 1 to 3 sentences. If they seem to want an action you "
                   "can take (show stats, list leads or estimates, save a contact, connect an "
                   "account, text a lead, change scheduling), offer it. Never call something a "
                   "'feature request' or say it is 'for future development'. If you cannot do it "
                   "directly, point them to the right place honestly (Settings for their profile, "
                   "hours, alerts and reminders; the simulator to see it work).")
            out = ai._strip_think(ai._llm_complete(provider, sys, message))
            if out:
                return out
        except Exception:
            pass
    return ("I run your desk. Tell me what you need: \"what should I focus on,\" \"how many "
            "leads this week,\" \"show my booked estimates,\" or \"put a 90 minute buffer "
            "between estimates.\"")


def _chat_or_route(business, message, llm_reply=""):
    """Chat answer, but first route known topics to a real page (capability honesty). A
    routed reply is a capability_gap (no native tool); both are logged so we can learn.
    `business` lets the go-live route return a live status card for this tenant."""
    routed = _route_topic(message, business)
    if routed:
        return {"reply": routed["reply"], "cards": routed["cards"], "pending_action": None,
                "meta": {"tool": "route", "status": "capability_gap"}}
    return {"reply": llm_reply or _chat_reply(message), "cards": [], "pending_action": None,
            "meta": {"tool": "chat", "status": "chat"}}


# --------------------------------------------------------------------------
# GATED ACTIONS: build the confirm (or an early ask), shared by both brain paths
# --------------------------------------------------------------------------
_LEAD_TOOLS = {"text_lead", "book_estimate", "cancel_estimate", "flag_urgent"}
_CONFIRM_PROMPT = "Ready when you are. Confirm below and I will take care of it."


def _say(text):
    return {"reply": text, "cards": [], "pending_action": None,
            "meta": {"tool": None, "status": "chat"}}


def _result(out, tool, status):
    cards = out.get("cards", [])
    return {"reply": out.get("reply", ""), "cards": cards, "pending_action": None,
            "entities": out.get("entities"), "meta": {"tool": tool, "status": status}}


def _gated(business, tool, args, message, fallback_text=_CONFIRM_PROMPT):
    """Result for a confirm-gated tool: an early non-gated reply (ask for a missing detail,
    or show open windows) OR a pending_action carrying an honest summary + preview. Shared by
    the keyword path and the tool-calling loop so the gate behaves identically on both."""
    if tool == "text_lead" and not (args.get("message") or "").strip():
        return _result(_h_text_lead(business, args), tool, "ok")     # ask what to say
    if tool == "set_scheduling" and not (args.get("buffer_minutes") is not None
                                         or args.get("times") or args.get("working_days")):
        return _result(_h_scheduling(business, args), "scheduling", "ok")
    if tool == "book_estimate":
        lead = _resolve_lead_target(business, args)
        if lead is None:
            return _say("Which lead should I book? Tell me a name, or list your leads and say "
                        "\"book the first one.\"")
        args["_lead_id"] = lead["id"]
        slot = _resolve_slot(business, args.get("when") or args.get("raw") or message)
        if not slot:
            out = _h_list_slots(business, args)
            return {"reply": ("I could not find an open window that matches. Here is what is "
                              "open." if out.get("cards") else out["reply"]),
                    "cards": out.get("cards", []), "pending_action": None,
                    "meta": {"tool": "list_slots",
                             "status": "ok" if out.get("cards") else "empty"}}
        _pin_slot(args, slot)
        summary = f"Book {_who(lead)} for {slot['label']}."
    elif tool == "cancel_estimate":
        lead = _resolve_lead_target(business, args)
        if lead is None:
            return _say("Whose estimate should I cancel? Tell me a name.")
        appts = db.lead_booked_appointments(business["id"], lead["id"])
        if not appts:
            return _say(f"{_who(lead)} has no booked estimate to cancel.")
        args["_lead_id"], args["_appt_id"] = lead["id"], appts[0]["id"]
        summary = f"Cancel {_who(lead)}'s estimate ({appts[0].get('scheduled_for') or 'booked'})."
    else:
        summary = _confirm_summary(tool, args)
    pending = {"tool": tool, "args": args, "summary": summary}
    if tool == "text_lead":
        pending["preview"] = _text_preview(business, args)
    return {"reply": fallback_text, "cards": [], "pending_action": pending,
            "meta": {"tool": tool, "status": "pending"}}


# --------------------------------------------------------------------------
# PUBLIC ENTRY POINTS
# --------------------------------------------------------------------------
def run(business, message, history=None, entities=None, allow_llm=True):
    """One natural-language turn. `entities` are the records most recently shown in this
    conversation (from db.recent_entities), used to resolve referents like "text her back".
    `allow_llm=False` skips the LLM loop and answers from the deterministic keyword floor --
    the graceful degrade when a tenant has spent its daily LLM budget (the gate, booking, and
    lists all still work; only the fuzzy/chat LLM path is withheld)."""
    message = (message or "").strip()
    if not message:
        return {"reply": "What can I do for you?", "cards": [], "pending_action": None,
                "meta": {"tool": None, "status": "empty"}}

    taught = _apply_learning(business, message)   # a confirmed correction beats the brain
    if taught is not None:
        return taught

    tool, args = _demo_route(message)

    # Clear, confirm-gated WRITE intents (text / book / cancel / scheduling change) go
    # through the deterministic router even when an LLM is keyed: it reliably invokes the
    # gated tool and surfaces the confirm, where an LLM (MiniMax especially) often just
    # *talks about* doing it without calling the tool. Safety + reliability. (The gate is
    # never bypassed either way -- a write only runs after an explicit confirm.)
    spec = TOOLS.get(tool)
    if spec and spec["confirm"]:
        if tool in _LEAD_TOOLS and entities and _is_referential(message):
            _apply_referent(message, args, entities)
        return _gated(business, tool, args, message)

    # Reads, chat, and fuzzy phrasing: the multi-step tool-calling loop when a provider is
    # keyed (it can chain read tools and answer conversationally), else the keyword floor.
    looped = _tool_loop(business, message, history, entities, allow_llm=allow_llm)
    if looped is not None:
        return looped

    if tool == "chat":
        return _chat_or_route(business, message)
    if tool in _LEAD_TOOLS and entities and _is_referential(message):
        _apply_referent(message, args, entities)
    spec = TOOLS[tool]
    out = spec["fn"](business, args)
    cards = out.get("cards", [])
    return {"reply": out.get("reply") or "", "cards": cards, "pending_action": None,
            "entities": out.get("entities"),
            "meta": {"tool": tool, "status": "ok" if cards else "empty"}}


# --------------------------------------------------------------------------
# STREAMING: live token streaming over the SSE channel, same result contract
# --------------------------------------------------------------------------
def _chunk_reply(text, size=24):
    """Yield a computed reply in small word-respecting slices so the deterministic paths
    (demo brain, keyword-routed confirms) stream over SSE exactly like the live model path --
    the transport is identical; only the source of the text differs."""
    text = text or ""
    buf = ""
    for w in re.findall(r"\S+\s*", text):
        buf += w
        if len(buf) >= size:
            yield buf
            buf = ""
    if buf:
        yield buf


def _stream_static(result):
    """Stream a fully-computed run() result: the reply as ('delta', ...) slices, then one
    ('done', result)."""
    for chunk in _chunk_reply(result.get("reply")):
        yield ("delta", chunk)
    yield ("done", result)


def _tool_loop_stream(business, message, history, entities=None):
    """Live-streaming sibling of _tool_loop for Claude: yields ('delta', text) as the model
    generates, runs read tools between rounds, and STOPS at the first gated write (returned as
    a pending_action via ('done', ...)). Yields ('done', result) on success, or ('fallback',
    None) to drop to the deterministic floor (non-claude / any error). The confirm gate is
    never bypassed -- identical contract to _tool_loop."""
    if ai._active_provider() != "claude":
        yield ("fallback", None)
        return
    msgs = []
    for turn in (history or [])[-6:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        msgs.append({"role": role, "content": str(turn.get("content", ""))})
    msgs.append({"role": "user", "content": message})
    tools = _tool_schemas()
    cards, ents_out, last_text = [], None, ""
    try:
        for _ in range(_MAX_TOOL_STEPS):
            res = None
            for kind, payload in llm.tool_complete_stream(
                    "claude", _loop_system(business), msgs, tools):
                if kind == "text":
                    yield ("delta", payload)
                else:
                    res = payload
            res = res or {"text": "", "tool_calls": []}
            last_text = res.get("text") or last_text
            calls = res.get("tool_calls") or []
            if not calls:
                if not cards:
                    routed = _route_topic(message, business)
                    if routed:
                        yield ("done", {"reply": routed["reply"], "cards": routed["cards"],
                                        "pending_action": None,
                                        "meta": {"tool": "route", "status": "capability_gap"}})
                        return
                break
            msgs.append({"role": "assistant", "content": res.get("text") or "",
                         "tool_calls": calls})
            for tc in calls:
                name, args = tc.get("name"), dict(tc.get("input") or {})
                spec = TOOLS.get(name)
                if not spec:
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": "No such tool."})
                    continue
                if spec["confirm"]:
                    if name in _LEAD_TOOLS and entities and _is_referential(message):
                        _apply_referent(message, args, entities)
                    g = _gated(business, name, args, message,
                               res.get("text") or _CONFIRM_PROMPT)
                    if cards:
                        g["cards"] = cards + g.get("cards", [])
                    if ents_out and not g.get("entities"):
                        g["entities"] = ents_out
                    yield ("done", g)
                    return
                out = spec["fn"](business, args)
                cards += out.get("cards", [])
                if out.get("entities"):
                    ents_out = out["entities"]
                msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "content": out.get("reply", "Done.")})
        reply = last_text or _chat_reply(message)
        yield ("done", {"reply": reply, "cards": cards, "pending_action": None,
                        "entities": ents_out,
                        "meta": {"tool": "tools", "status": "ok" if cards else "chat"}})
    except Exception as e:
        print(f"[ringback] stream tool loop failed, using keyword floor: {e}", flush=True)
        yield ("fallback", None)


def run_stream(business, message, history=None, entities=None, allow_llm=True):
    """Streaming sibling of run(): a generator yielding ('delta', text) for the reply as it is
    produced, then exactly one ('done', result) whose dict matches run()'s shape (so the same
    cards / pending_action / coach renderers apply). Live token streaming for the Claude chat /
    read path; the deterministic paths (empty, learned correction, gated write via the keyword
    router, demo / MiniMax / budget-exhausted floor) compute the reply and stream it chunked
    over the same SSE channel. The confirm gate is never bypassed on either path."""
    message = (message or "").strip()
    if not message:
        yield ("done", {"reply": "What can I do for you?", "cards": [],
                        "pending_action": None, "meta": {"tool": None, "status": "empty"}})
        return

    # Live streaming engages only for the Claude chat/read path. Everything run() would
    # short-circuit before the LLM loop (a confirmed correction, a gated WRITE routed through
    # the deterministic router) is computed and streamed chunked -- there are no model tokens
    # to stream there, and the gate must stay on the reliable keyword path.
    if allow_llm and ai._active_provider() == "claude":
        taught = _apply_learning(business, message)
        if taught is not None:
            yield from _stream_static(taught)
            return
        tool, args = _demo_route(message)
        spec = TOOLS.get(tool)
        if spec and spec["confirm"]:
            if tool in _LEAD_TOOLS and entities and _is_referential(message):
                _apply_referent(message, args, entities)
            yield from _stream_static(_gated(business, tool, args, message))
            return
        result = None
        streamed = False
        for kind, payload in _tool_loop_stream(business, message, history, entities):
            if kind == "delta":
                streamed = True
                yield ("delta", payload)
            elif kind == "done":
                result = payload
            # 'fallback' leaves result None -> drop to the deterministic compute below.
        if result is not None:
            yield ("done", result)
            return
        if streamed:
            # The live loop emitted partial text then bailed (rare mid-stream error). Don't
            # re-stream the reply over it (avoids a flash of duplicate text); compute once and
            # let the done frame set the authoritative final text.
            yield ("done", run(business, message, history, entities, allow_llm=allow_llm))
            return

    # Deterministic path (demo / MiniMax / no key / budget exhausted / live loop bailed before
    # any output): run() owns all routing + contracts; we just stream its reply chunked.
    yield from _stream_static(run(business, message, history, entities, allow_llm=allow_llm))


def _apply_learning(business, message):
    """Honor a tenant's confirmed correction matching this message (deterministic, before
    the brain). The lookup hook is wired by the app to convos.lookup; a {'_run_tool': name}
    directive is executed here so tool execution stays in this module (no import cycle)."""
    fn = globals().get("_learning_lookup")
    if not fn:
        return None
    hit = fn(business, message)
    if not hit:
        return None
    if "_run_tool" in hit:
        tool = hit["_run_tool"]
        spec = TOOLS.get(tool)
        if not spec:
            return None
        if spec["confirm"]:
            summary = _confirm_summary(tool, {"raw": message})
            pending = {"tool": tool, "args": {"raw": message}, "summary": summary}
            if tool == "text_lead":
                pending["preview"] = _text_preview(business, {"raw": message})
            return {"reply": "Ready when you are. Confirm below and I will take care of it.",
                    "cards": [], "pending_action": pending,
                    "meta": {"tool": tool, "status": "pending"}}
        out = spec["fn"](business, {"raw": message})
        cards = out.get("cards", [])
        return {"reply": out.get("reply", ""), "cards": cards, "pending_action": None,
                "meta": {"tool": tool, "status": "learned" if cards else "empty"}}
    return hit


def _clean_args(tool, args):
    """Keep only the args a tool declares (plus the internal raw/_lead_id), so a crafted
    /assistant/confirm payload can't smuggle unexpected keys into a handler."""
    spec = TOOLS.get(tool)
    if not spec:
        return {}
    allow = set(spec["params"]) | {"raw", "_lead_id", "_slot_id", "_slot_label",
                                   "_slot_day", "_slot_time", "_appt_id"}
    return {k: v for k, v in (args or {}).items() if k in allow}


def execute(business, tool, args):
    spec = TOOLS.get(tool)
    if not spec:
        return {"reply": "That action is no longer available.", "cards": [],
                "pending_action": None, "meta": {"tool": tool, "status": "error"}}
    out = spec["fn"](business, _clean_args(tool, args or {}))
    cards = out.get("cards", [])
    return {"reply": out.get("reply", "Done."), "cards": cards, "pending_action": None,
            "meta": {"tool": tool, "status": "ok" if cards else "empty"}}


def suggestions():
    return [
        "How many leads came in this week?",
        "Show my booked estimates",
        "Connect my Google calendar",
        "Save a number as a customer",
        "Who do I still need to chase?",
    ]
