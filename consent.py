"""Shared consent + opt-out core for the trades_core kernel (vendored into each product).

The compliance moat both products market (TCPA / CAN-SPAM / DNC), unified. Two concerns:

  1. OPT-OUT DETECTION (`opt_out_nlu` / `opt_in_nlu`) — pure functions, no DB. They
     extend the carrier exact-keyword convention (STOP/START) with plain-language
     intent ("please stop texting me", "take me off your list"). They only ADD
     detections over the exact-keyword baseline, so wiring them in never regresses
     existing STOP handling — it just closes the natural-language opt-out hole. A
     false positive is safe by design: we stop texting someone who can re-START.

  2. THE APPEND-ONLY CONSENT LEDGER (`ensure_ledger` / `record` / `current_status` /
     `is_suppressed`) — one auditable table both apps can adopt, keyed on the phone
     NUMBER (the identity both products share), per-channel, business_id-scoped. This
     is the migration target; RingBack's mutable `contacts_consent` backfills into it.
     The ledger functions take a sqlite3 connection so each app reuses them against its
     own DB with no new dependency.

Edit trades_core/consent.py, then run `python3 trades_core/sync.py`.
"""
import re

# Carrier-convention exact keywords (the existing baseline in both apps).
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit",
              "optout", "opt-out", "revoke", "remove"}
START_WORDS = {"start", "unstop", "yes", "optin", "opt-in", "resume"}

# Plain-language opt-out intent. Deliberately phrase-anchored (not bare "stop", which
# would catch "stop by tomorrow") so we add real opt-outs without nuking engaged leads.
_OPT_OUT_PHRASES = re.compile(
    r"\b("
    r"stop (texting|messaging|contacting|emailing|calling|sending)|"
    r"(take|get) me off|remove me|unsubscribe me|opt me out|"
    r"leave me alone|lose my number|"
    r"(no|don'?t want|do ?not want) (any )?(more )?(texts?|messages?|emails?|calls?)|"
    r"(don'?t|do ?not|stop|quit|never) (text|messag|contact|email|call)\w*( me)?|"
    r"unsubscribe"
    r")\b",
    re.IGNORECASE,
)
_OPT_IN_PHRASES = re.compile(
    r"\b(resume (texts?|messages?)|start (texts?|messaging)|opt me (back )?in|"
    r"(yes|sure),? (text|message) me)\b",
    re.IGNORECASE,
)


def _first_word(text):
    parts = (text or "").strip().lower().split()
    return parts[0].strip(".,!?;:") if parts else ""


def opt_out_nlu(text):
    """True if an inbound message expresses opt-out intent. A bare carrier keyword
    counts only when the message essentially IS that keyword (<=2 words) — so "STOP"
    and "stop please" opt out, but "stop by tomorrow" and "cancel my appointment" do
    not. Longer messages must match an explicit opt-out phrase. Strict superset of a
    correct exact-keyword check; only ADDS plain-language detections."""
    words = (text or "").strip().split()
    if words and _first_word(text) in STOP_WORDS and len(words) <= 2:
        return True
    return bool(_OPT_OUT_PHRASES.search(text or ""))


def opt_in_nlu(text):
    """True if an inbound message expresses opt-IN / resume intent. Exact keyword
    first-word match takes precedence; opt-out always wins over opt-in elsewhere."""
    if _first_word(text) in START_WORDS:
        return True
    return bool(_OPT_IN_PHRASES.search(text or ""))


def classify_inbound(text):
    """One call for inbound webhooks: 'opt_out' | 'opt_in' | None. Opt-out wins ties
    (always honor a stop)."""
    if opt_out_nlu(text):
        return "opt_out"
    if opt_in_nlu(text):
        return "opt_in"
    return None


def normalize_number(raw):
    """Loose E.164-ish normalization so the same phone keys one ledger identity
    regardless of formatting. Keeps a leading +, strips spaces/punctuation, and
    defaults a bare 10-digit US number to +1. Non-numeric input returns ''. """
    s = (raw or "").strip()
    if not s:
        return ""
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


# --------------------------------------------------------------------------
# Append-only ledger (migration target). Takes a sqlite3 connection.
# --------------------------------------------------------------------------
CHANNELS = ("sms", "email", "voice")


def ensure_ledger(conn):
    """Create the shared append-only consent ledger if absent. Additive and safe to
    run at boot in either app — creates an empty table, never touches existing data."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consent_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            number TEXT NOT NULL,          -- normalized E.164 identity (shared by both apps)
            channel TEXT NOT NULL,         -- sms | email | voice
            event TEXT NOT NULL,           -- granted | opted_out
            source TEXT DEFAULT '',        -- audit trail: 'inbound STOP', 'backfill', ...
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_consent_ledger_lookup "
        "ON consent_ledger (business_id, number, channel, id)"
    )


def record(conn, business_id, number, channel, event, source="", created_at=None):
    """Append a consent event. Idempotent on the (business, number, channel, event)
    state: if the latest event for that key already equals `event`, no row is added
    (so a retry/restart never double-records). Returns True if a row was written."""
    from datetime import datetime
    num = normalize_number(number)
    if not num or channel not in CHANNELS or event not in ("granted", "opted_out"):
        return False
    if current_status(conn, business_id, num, channel) == event:
        return False
    conn.execute(
        "INSERT INTO consent_ledger (business_id, number, channel, event, source, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (business_id, num, channel, event, source or "",
         created_at or datetime.now().astimezone().isoformat()),
    )
    conn.commit()
    return True


def current_status(conn, business_id, number, channel="sms"):
    """Latest event for (business, number, channel): 'granted' | 'opted_out' | None."""
    num = normalize_number(number)
    row = conn.execute(
        "SELECT event FROM consent_ledger WHERE business_id=? AND number=? AND channel=? "
        "ORDER BY id DESC LIMIT 1",
        (business_id, num, channel),
    ).fetchone()
    if not row:
        return None
    return row["event"] if hasattr(row, "keys") else row[0]


def is_suppressed(conn, business_id, number, channel="sms"):
    """True if the most recent ledger event for this number+channel is an opt-out."""
    return current_status(conn, business_id, number, channel) == "opted_out"
