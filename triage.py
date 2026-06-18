"""Caller triage: the "phone screen" -- decide whether a missed caller gets the
automated text-back, modeled on how Apple screens calls.

FirstBack's promise is "never lose a job to a missed call," which makes the costs
ASYMMETRIC: texting a non-prospect by mistake is cheap (a cent and a dead thread),
but NOT texting a real customer is the one failure the product exists to prevent.
So this layer is PRECISION-FIRST -- it only stays silent for callers we're nearly
certain about, and treats every ambiguous unknown as a prospect (engaged, just
flagged for review). It also fails OPEN: a missing or slow signal never silences a
caller.

Two callers should NOT get the cold bot text:
  * KNOWN / saved people -- the owner handles them personally (Apple passes anyone
    in your Contacts straight through). FirstBack's "known" set is AUTO-DERIVED with
    zero setup (db.is_known_caller: a booked estimate or any directory entry), so
    the owner never has to import or hand-categorize an address book.
  * SPAM / robocallers -- scored from layered signals (below).

The verdict is a single dict:
    {"engage": bool, "status": str, "score": int(0-100), "category": str,
     "reasons": [str], "reason": str}   # "reason" kept for back-compat
where status is one of:
    opted_out         -> replied STOP (engage False)
    screened_contact  -> a known non-prospect: personal/vendor/blocked (engage False)
    trusted           -> a known/saved caller; owner handles them, no bot (engage False)
    screened_spam     -> score >= HARD: a near-certain robocaller (engage False)
    review            -> MID <= score < HARD: engaged, but FLAGGED for the owner
    prospect          -> a clean unknown (engage True)

Tiers feeding the spam score (cheapest first; computed by the pure spam_score()):
    Tier 1 (free, hot path): STIR/SHAKEN attestation, neighbor-spoof, repeat-call
                             behavior.
    Tier 2 (paid, gated):    number reputation (reputation.lookup -> line type +
                             robocall score).
    Tier 2.5 (free):         crowdsourced cross-tenant spam flags (db.global_spam_count).
The AI CONTENT screen (Tier 3) lives on the conversation path (ai.classify_intent),
not here -- it can only run once the caller actually replies.
"""
import db
from config import SCREEN_SCORE_HARD, SCREEN_SCORE_MID, SCREEN_CROWD_MIN

# Directory categories that are positively NOT a prospect -> never proactively
# texted. Everything else (prospect, customer, or an unknown number) engages.
NON_PROSPECT = {"personal", "vendor", "blocked"}


def should_engage(contact):
    """Pure: True unless the caller is a known non-prospect. A None contact means
    we have never seen this number, which we treat as a potential customer."""
    if not contact:
        return True
    return (contact.get("category") or "prospect") not in NON_PROSPECT


def _attestation_level(raw):
    """Normalize a Twilio StirVerstat value to an attestation letter A/B/C, or None
    when the call carried no SHAKEN identity (unverified). Values look like
    'TN-Validation-Passed-A', 'TN-Validation-Failed-C', or 'No-TN-Validation'."""
    s = str(raw or "").strip().upper()
    if not s or "NO-TN-VALIDATION" in s:
        return None
    if "FAILED" in s:
        return "C"
    for letter in ("A", "B", "C"):
        if s.endswith("-" + letter) or s == letter:
            return letter
    return None


def spam_score(signals):
    """PURE (no I/O): fold screening signals into a 0-100 spam score + the list of
    human-readable reasons that drove it. Higher = more spam-like. Tuned precision-
    first: NO single weak signal reaches the HARD threshold, so a real homeowner is
    never hard-screened on one fuzzy hint -- it takes corroboration (or one
    authoritative provider verdict).

    `signals` keys (all optional):
        attestation     str  -- STIR/SHAKEN letter A/B/C (A lowers the score)
        neighbor_spoof  bool -- caller shares the business's area code + prefix
        line_type       str  -- e.g. 'nonFixedVoip' (a robocaller signal)
        reputation_score int -- 0-100 from a paid provider (authoritative)
        crowd_count     int  -- distinct OTHER businesses that flagged this number
        burst_count     int  -- distinct businesses that flagged this number in the
                               last 24h (velocity burst; needs corroboration to reach
                               HARD -- +35 alone doesn't hit the 80 default threshold)
        behavior        dict -- {missed_calls, inbound_msgs, booked}
    """
    score, reasons = 0, []

    att = _attestation_level(signals.get("attestation"))
    if att == "A":
        score -= 15  # carrier vouches the caller owns the number -> trust signal
    elif att == "C":
        score += 30
        reasons.append("failed caller-ID verification (attestation C)")
    elif att is None and "attestation" in signals:
        score += 10
        reasons.append("caller ID not verified")

    if signals.get("neighbor_spoof"):
        score += 25
        reasons.append("number mimics your local area code + prefix (neighbor spoofing)")

    lt = str(signals.get("line_type") or "")
    if lt and lt.lower() == "nonfixedvoip":
        score += 25
        reasons.append("non-fixed VoIP line (common for robocallers)")

    rep = signals.get("reputation_score")
    if rep is not None:
        try:
            rep = int(rep)
            score += round(rep * 0.8)   # 100 alone reaches HARD; an authoritative verdict
            if rep >= 70:
                reasons.append("flagged as spam by number-reputation lookup")
            elif rep >= 40:
                reasons.append("mixed reputation from number-reputation lookup")
        except (TypeError, ValueError):
            pass

    crowd = int(signals.get("crowd_count") or 0)
    if crowd >= SCREEN_CROWD_MIN:
        contribution = min(40 + (crowd - SCREEN_CROWD_MIN) * 15, 75)
        score += contribution
        reasons.append(f"flagged as spam by {crowd} other businesses")

    # Burst signal: this number has been flagged by 3+ distinct businesses in the
    # last 24 hours (velocity). +35 alone does NOT reach the default HARD threshold
    # of 80 -- corroboration (another signal) is still required. Precision-first.
    burst = int(signals.get("burst_count") or 0)
    if burst >= 3:
        score += 35
        reasons.append("calling dozens of businesses in the past hour")

    beh = signals.get("behavior") or {}
    missed = int(beh.get("missed_calls") or 0)
    inbound = int(beh.get("inbound_msgs") or 0)
    booked = int(beh.get("booked") or 0)
    if booked == 0 and inbound == 0 and missed >= 3:
        score += 30
        reasons.append(f"called {missed} times and never replied to a text")

    return max(0, min(100, score)), reasons


def screen_caller(business_id, number, *, attestation=None, neighbor_spoof=False,
                  reputation=None, behavior=None, hard=None, mid=None):
    """Verdict for a missed caller, BEFORE we text back (see module docstring for the
    shape). Identity tiers first (free, decisive), then the spam score for unknowns.

    The signal kwargs are optional, so a bare screen_caller(biz, number) still works
    (identity-only, as the original v1 did) -- the hot path passes the richer signals.

    `hard`/`mid`: per-tenant band overrides (from biz['screen_hard'/'screen_mid']).
    Default to SCREEN_SCORE_HARD/MID when None. The UI slice resolves these from the
    business row and passes them in; CORE reads the config defaults so callers that
    don't pass them get existing behavior."""
    hard_threshold = hard if hard is not None else SCREEN_SCORE_HARD
    mid_threshold = mid if mid is not None else SCREEN_SCORE_MID

    if db.is_suppressed(business_id, number):
        return {"engage": False, "status": "opted_out", "score": 0,
                "category": "opted_out", "reasons": ["recipient opted out"],
                "reason": "recipient opted out"}

    contact = db.get_contact(business_id, number)
    category = (contact or {}).get("category") or "prospect"
    if not should_engage(contact):   # known personal / vendor / blocked
        return {"engage": False, "status": "screened_contact", "score": 0,
                "category": category, "reasons": [f"known {category}, not a prospect"],
                "reason": f"known {category}, not a prospect"}

    # Faithful-Apple: a caller we already KNOW (booked estimate or any directory
    # entry, e.g. a returning customer) is the owner's to handle -- never cold-pitched.
    if db.is_known_caller(business_id, number):
        return {"engage": False, "status": "trusted", "score": 0,
                "category": category if contact else "customer",
                "reasons": ["known caller -- handled by you, not the bot"],
                "reason": "known caller"}

    # Unknown caller: score the spam signals (crowd count + burst count read here;
    # the rest passed in). Crowd = all-time distinct flagging businesses (persistent
    # reputation). Burst = how many distinct businesses flagged in the last 24h
    # (velocity signal for a fresh robocall blitz; +35 alone does NOT reach HARD=80).
    # Burst is only passed when crowd hasn't fired: if crowd already reflects the same
    # flags, burst would double-count the same evidence. When crowd fires (>= CROWD_MIN),
    # the persistent reputation already captures the signal; burst adds nothing new.
    crowd_count = db.global_spam_count(number, exclude_business_id=business_id)
    burst_count = 0
    if crowd_count < SCREEN_CROWD_MIN:
        burst_count = db.global_spam_count(number, exclude_business_id=business_id,
                                           within_hours=24)
    signals = {"neighbor_spoof": neighbor_spoof, "behavior": behavior or {},
               "crowd_count": crowd_count, "burst_count": burst_count}
    if attestation is not None:
        signals["attestation"] = attestation
    if reputation:
        signals["line_type"] = reputation.get("line_type")
        signals["reputation_score"] = reputation.get("spam_score")

    score, reasons = spam_score(signals)
    if score >= hard_threshold:
        return {"engage": False, "status": "screened_spam", "score": score,
                "category": "spam", "reasons": reasons,
                "reason": "looks like spam/robocall"}
    if score >= mid_threshold:
        return {"engage": True, "status": "review", "score": score,
                "category": category, "reasons": reasons,
                "reason": "engaged but flagged for review"}
    return {"engage": True, "status": "prospect", "score": score,
            "category": category, "reasons": reasons or ["clean unknown caller"],
            "reason": "prospect"}


def neighbor_spoof(business_number, caller_number):
    """Pure: True if the caller shares the business's area code AND next-3 prefix --
    the classic 'neighbor spoofing' pattern (a robocaller faking a local number to
    look like someone nearby). Needs both numbers to be 10-digit-resolvable."""
    b = db._digits10(business_number)
    c = db._digits10(caller_number)
    if len(b) < 6 or len(c) < 6 or b == c:
        return False
    return b[:6] == c[:6]


# --------------------------------------------------------------------------
# SUGGESTIONS  (QuickBooks-style: observe a caller, RECOMMEND a bucket, the
# owner confirms with one tap. Suggestions never auto-apply.)
# --------------------------------------------------------------------------
# Deliberately conservative thresholds -- a recommendation, not a verdict.
SPAM_MIN_CALLS = 3        # repeat missed calls with zero replies -> "looks like spam?"
CLIENT_MIN_BOOKINGS = 2   # multiple booked estimates -> "add to your clients?"


def suggest_category(signals):
    """Pure: from a caller's behavioral aggregates, recommend (category, reason),
    or None to leave them an engaged prospect. `signals`: {missed_calls,
    inbound_msgs, booked}. Booking is the strongest signal, so it wins."""
    booked = signals.get("booked") or 0
    missed = signals.get("missed_calls") or 0
    inbound = signals.get("inbound_msgs") or 0
    if booked >= CLIENT_MIN_BOOKINGS:
        return ("customer", f"Booked {booked} estimates with you.")
    if missed >= SPAM_MIN_CALLS and inbound == 0:
        return ("blocked", f"Called {missed} times and never replied to a text.")
    return None


def scan_suggestions(business_id):
    """Generate/refresh pending classification suggestions from observed behavior.
    Idempotent and off the hot path (run from the ticker). Never touches a number the
    owner already classified, nor a suggestion they dismissed. Returns the pending
    count."""
    classified = {c["number"] for c in db.list_contacts(business_id)}
    for s in db.caller_signals(business_id):
        if s["number"] in classified:
            continue  # already in the directory -> nothing to suggest
        rec = suggest_category(s)
        if rec:
            db.upsert_suggestion(business_id, s["number"], s.get("name") or None,
                                 rec[0], rec[1], "behavior")
    return db.count_pending_suggestions(business_id)


def scan_all_suggestions():
    """Scan every business (called from the reminders ticker, off the hot path)."""
    for biz in db.list_businesses():
        try:
            scan_suggestions(biz["id"])
        except Exception as e:
            import sys
            print(f"[firstback] suggestion scan failed (biz {biz.get('id')}): {e}",
                  file=sys.stderr, flush=True)
