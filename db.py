"""SQLite storage for RingBack. File-based, zero-config.

Multi-tenant: every business (tenant) owns its own leads, appointments, busy
days, and integrations, all scoped by `business_id`. `users` log in and map to
one business. "Client zero" (Heritage House Painting) is business id 1.
"""
import re
import sqlite3
from datetime import datetime, timezone, date, timedelta
from config import (DB_PATH, DEFAULT_BUSINESS, ESTIMATE_TIMES, BOOKING_HORIZON_DAYS,
                    app_tz)

# `available_slots` is intentionally omitted: availability comes from the
# in-house calendar now, so we no longer seed or update that legacy column.
_BUSINESS_COLS = ["name", "trade", "service_area", "hours", "owner_name",
                  "phone", "ai_instructions"]


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    # Never let a lock hang the process forever (e.g. during a deploy while the prior
    # instance is still releasing the DB on the shared disk). Wait briefly, then error.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = get_conn()
    c = conn.cursor()
    # NOT WAL. The DB lives on Render's network-attached /var/data disk, where SQLite
    # WAL's shared-memory (-shm / mmap) is unreliable and can hang the worker on boot
    # ("No open HTTP ports detected"). The voice service relays over HTTP
    # (/internal/voice/turn) and never opens this file directly, so a single-writer
    # rollback journal is correct here and safe on a network filesystem.
    c.execute("PRAGMA journal_mode=DELETE")
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY,
            name TEXT, trade TEXT, service_area TEXT, hours TEXT,
            owner_name TEXT, ai_instructions TEXT, available_slots TEXT, phone TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            business_id INTEGER NOT NULL, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, phone TEXT, source TEXT DEFAULT 'missed_call',
            status TEXT DEFAULT 'new', urgent INTEGER DEFAULT 0, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER, direction TEXT, body TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER, scheduled_for TEXT, notes TEXT,
            status TEXT DEFAULT 'booked', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS busy_days (
            day TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS integrations (
            provider TEXT PRIMARY KEY,
            connected INTEGER DEFAULT 0,
            connected_at TEXT
        );
        CREATE TABLE IF NOT EXISTS contact_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, email TEXT, message TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, lead_id INTEGER,
            call_sid TEXT UNIQUE, from_number TEXT, to_number TEXT,
            dial_status TEXT, answered_by TEXT, missed INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS contacts_consent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, consumer_number TEXT NOT NULL,
            sms_ok INTEGER DEFAULT 1, voice_ok INTEGER DEFAULT 0,
            opted_out INTEGER DEFAULT 0, opted_out_at TEXT,
            source TEXT, updated_at TEXT,
            UNIQUE(business_id, consumer_number)
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, kind TEXT, channel TEXT,
            target TEXT, status TEXT, dedupe_key TEXT, body TEXT, created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_dedupe
            ON alerts(business_id, dedupe_key, created_at);
        CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, lead_id INTEGER, appointment_id INTEGER,
            kind TEXT, send_at TEXT, body TEXT, status TEXT DEFAULT 'pending',
            created_at TEXT, sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sched_due
            ON scheduled_messages(status, send_at);
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, number TEXT NOT NULL,
            name TEXT, category TEXT DEFAULT 'prospect', note TEXT, source TEXT,
            created_at TEXT, updated_at TEXT,
            UNIQUE(business_id, number)
        );
        CREATE TABLE IF NOT EXISTS contact_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, number TEXT NOT NULL,
            name TEXT, suggested_category TEXT, reason TEXT, source TEXT,
            status TEXT DEFAULT 'pending', created_at TEXT, updated_at TEXT,
            UNIQUE(business_id, number)
        );
        """
    )
    # Migration: add `urgent` to older databases that predate the column.
    cols = [r[1] for r in c.execute("PRAGMA table_info(leads)").fetchall()]
    if "urgent" not in cols:
        c.execute("ALTER TABLE leads ADD COLUMN urgent INTEGER DEFAULT 0")
    # Migration: appointments gain a real `day` (YYYY-MM-DD) for the calendar.
    appt_cols = [r[1] for r in c.execute("PRAGMA table_info(appointments)").fetchall()]
    if "day" not in appt_cols:
        c.execute("ALTER TABLE appointments ADD COLUMN day TEXT")
        for row in c.execute(
                "SELECT id, scheduled_for FROM appointments WHERE day IS NULL").fetchall():
            d = parse_day(row[1])
            if d:
                c.execute("UPDATE appointments SET day=? WHERE id=?", (d, row[0]))
    # Migration: appointments gain a canonical `slot_time` (24h HH:MM) so every
    # spelling of a window collapses to one identity. This backs the DB-level
    # uniqueness that makes a slot un-double-bookable.
    if "slot_time" not in appt_cols:
        c.execute("ALTER TABLE appointments ADD COLUMN slot_time TEXT")
        for row in c.execute(
                "SELECT id, scheduled_for FROM appointments WHERE slot_time IS NULL").fetchall():
            tk = time_key(row[1])
            if tk:
                c.execute("UPDATE appointments SET slot_time=? WHERE id=?", (tk, row[0]))
    # One-time: collapse any pre-existing double-books (keep the earliest booking
    # per slot, cancel the rest), then enforce uniqueness at the DB layer.
    existing_idx = [r[1] for r in c.execute("PRAGMA index_list(appointments)").fetchall()]
    if "uniq_booked_slot" not in existing_idx:
        c.execute(
            "UPDATE appointments SET status='canceled' "
            "WHERE status='booked' AND day IS NOT NULL AND slot_time IS NOT NULL "
            "AND id NOT IN (SELECT MIN(id) FROM appointments "
            "  WHERE status='booked' AND day IS NOT NULL AND slot_time IS NOT NULL "
            "  GROUP BY day, slot_time)")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_booked_slot "
            "ON appointments(day, slot_time) "
            "WHERE status='booked' AND day IS NOT NULL AND slot_time IS NOT NULL")
    # Migration: leads gain compressed conversation notes.
    if "summary" not in cols:
        for col in ("address", "project_type", "summary", "stage"):
            c.execute(f"ALTER TABLE leads ADD COLUMN {col} TEXT")
        c.execute("ALTER TABLE leads ADD COLUMN notes_msgs INTEGER DEFAULT 0")

    # ---- Multi-tenant migration: scope everything by business_id ----
    lead_cols = [r[1] for r in c.execute("PRAGMA table_info(leads)").fetchall()]
    if "business_id" not in lead_cols:
        c.execute("ALTER TABLE leads ADD COLUMN business_id INTEGER DEFAULT 1")
    appt_cols = [r[1] for r in c.execute("PRAGMA table_info(appointments)").fetchall()]
    if "business_id" not in appt_cols:
        c.execute("ALTER TABLE appointments ADD COLUMN business_id INTEGER DEFAULT 1")
        # Uniqueness is now PER BUSINESS: two tenants may book the same wall-clock
        # slot, but one tenant can never double-book it.
        c.execute("DROP INDEX IF EXISTS uniq_booked_slot")
        c.execute(
            "CREATE UNIQUE INDEX uniq_booked_slot "
            "ON appointments(business_id, day, slot_time) "
            "WHERE status='booked' AND day IS NOT NULL AND slot_time IS NOT NULL")
    # busy_days: single-key (day) -> composite (business_id, day).
    busy_cols = [r[1] for r in c.execute("PRAGMA table_info(busy_days)").fetchall()]
    if "business_id" not in busy_cols:
        c.executescript(
            "CREATE TABLE busy_days_new (business_id INTEGER NOT NULL, day TEXT NOT NULL,"
            "  PRIMARY KEY (business_id, day));"
            "INSERT INTO busy_days_new (business_id, day) SELECT 1, day FROM busy_days;"
            "DROP TABLE busy_days;"
            "ALTER TABLE busy_days_new RENAME TO busy_days;")
    # integrations: single-key (provider) -> composite (business_id, provider),
    # plus OAuth token columns (used by the real Google Calendar sync).
    intg_cols = [r[1] for r in c.execute("PRAGMA table_info(integrations)").fetchall()]
    if "business_id" not in intg_cols:
        c.executescript(
            "CREATE TABLE integrations_new ("
            "  business_id INTEGER NOT NULL, provider TEXT NOT NULL,"
            "  connected INTEGER DEFAULT 0, connected_at TEXT,"
            "  access_token TEXT, refresh_token TEXT, token_expiry TEXT, calendar_id TEXT,"
            "  PRIMARY KEY (business_id, provider));"
            "INSERT INTO integrations_new (business_id, provider, connected, connected_at)"
            "  SELECT 1, provider, connected, connected_at FROM integrations;"
            "DROP TABLE integrations;"
            "ALTER TABLE integrations_new RENAME TO integrations;")

    # businesses gain a `phone` (the RingBack texting number, shown in the demo).
    biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
    if "phone" not in biz_cols:
        c.execute("ALTER TABLE businesses ADD COLUMN phone TEXT")
        c.execute("UPDATE businesses SET phone=? WHERE id=1 AND (phone IS NULL OR phone='')",
                  (DEFAULT_BUSINESS.get("phone", ""),))

    # ---- Callback system (Twilio) migration ----
    # businesses gain real-telephony + registration fields. These are set by number
    # provisioning / onboarding, NOT the Settings form, so they stay OUT of
    # _BUSINESS_COLS (which would otherwise blank them on a profile save).
    biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
    for col, ddl in (
            ("twilio_number", "TEXT"), ("twilio_number_sid", "TEXT"),
            ("forward_to", "TEXT"), ("timezone", "TEXT"),
            ("a2p_brand_sid", "TEXT"), ("a2p_campaign_sid", "TEXT"),
            ("a2p_status", "TEXT DEFAULT 'unregistered'"),
            ("voice_callback_enabled", "INTEGER DEFAULT 0")):
        if col not in biz_cols:
            c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")
    # messages gain delivery tracking for real (Twilio) SMS.
    msg_cols = [r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()]
    for col in ("provider_sid", "delivery_status"):
        if col not in msg_cols:
            c.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
    # calls gain the triage outcome (Caller triage v1): which directory category the
    # caller matched and whether we engaged (texted back) or screened them out.
    call_cols = [r[1] for r in c.execute("PRAGMA table_info(calls)").fetchall()]
    for col, ddl in (("category", "TEXT"), ("engaged", "INTEGER")):
        if col not in call_cols:
            c.execute(f"ALTER TABLE calls ADD COLUMN {col} {ddl}")

    # businesses gain owner-alert preferences (Feature 2: alert the owner when a
    # lead arrives / an estimate books / a lead is flagged urgent). These ARE set
    # by the Settings form (via update_alert_prefs, not _BUSINESS_COLS). alert_email
    # defaults at send time to the owner's login email; the toggles default ON.
    biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
    for col, ddl in (("alert_email", "TEXT"), ("alert_sms", "TEXT"),
                     ("alert_on_lead", "INTEGER DEFAULT 1"),
                     ("alert_on_booking", "INTEGER DEFAULT 1"),
                     ("alert_on_urgent", "INTEGER DEFAULT 1")):
        if col not in biz_cols:
            c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")

    # businesses gain reminder/follow-up preferences (Feature 1). Both default ON;
    # reminder_lead_hours NULL -> falls back to config.REMINDER_LEAD_HOURS.
    biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
    for col, ddl in (("reminders_enabled", "INTEGER DEFAULT 1"),
                     ("followups_enabled", "INTEGER DEFAULT 1"),
                     ("reminder_lead_hours", "REAL"),
                     ("avg_job_value", "REAL")):   # owner-set; powers the ROI revenue estimate
        if col not in biz_cols:
            c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")

    # One follow-up per lead, EVER -- enforced at the DB layer so two scheduler
    # drivers (the in-process ticker + an external /tasks/run-due cron) can't race
    # and double-queue a nudge. Collapse any pre-existing duplicates (keep the
    # earliest per lead) before creating the unique index so it can't fail to build.
    sched_idx = [r[1] for r in c.execute("PRAGMA index_list(scheduled_messages)").fetchall()]
    if "uniq_followup_per_lead" not in sched_idx:
        c.execute(
            "DELETE FROM scheduled_messages WHERE kind='followup' AND id NOT IN "
            "(SELECT MIN(id) FROM scheduled_messages WHERE kind='followup' GROUP BY lead_id)")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_followup_per_lead "
            "ON scheduled_messages(lead_id) WHERE kind='followup'")

    # Seed "client zero" (business 1) if no business exists yet.
    if not c.execute("SELECT 1 FROM businesses WHERE id=1").fetchone():
        b = DEFAULT_BUSINESS
        cols = ",".join(_BUSINESS_COLS)
        marks = ",".join("?" for _ in _BUSINESS_COLS)
        c.execute(
            f"INSERT INTO businesses (id,{cols}) VALUES (1,{marks})",
            tuple(b[col] for col in _BUSINESS_COLS),
        )
    conn.commit()
    conn.close()


# ---- Users / auth ----
def create_user(email, password_hash, business_id):
    """Create a login. Returns the new user id, or None if the email is taken."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, business_id, created_at) "
            "VALUES (?,?,?,?)",
            (email.strip().lower(), password_hash, business_id, now_iso()))
        conn.commit()
        uid = cur.lastrowid
    except sqlite3.IntegrityError:
        uid = None  # email already registered
    conn.close()
    return uid


def get_user_by_email(email):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email=?",
                       (email.strip().lower(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def count_users():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return n


def update_user_password(user_id, password_hash):
    conn = get_conn()
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
    conn.commit()
    conn.close()


# ---- Business profile ----
def create_business(fields):
    """Create a new tenant business (signup). Returns its id."""
    cols = [col for col in _BUSINESS_COLS if col in fields]
    conn = get_conn()
    collist = ",".join(cols)
    marks = ",".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO businesses ({collist}) VALUES ({marks})",
                       tuple(fields[col] for col in cols))
    conn.commit()
    bid = cur.lastrowid
    conn.close()
    return bid


def get_business(business_id=1):
    conn = get_conn()
    row = conn.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
    conn.close()
    return dict(row) if row else dict(DEFAULT_BUSINESS, id=business_id)


def update_business(business_id, fields):
    # Only update the columns actually provided, so a form that omits a field
    # (e.g. the retired free-text slots) never blanks it.
    cols = [col for col in _BUSINESS_COLS if col in fields]
    if not cols:
        return
    conn = get_conn()
    sets = ", ".join(f"{col}=?" for col in cols)
    conn.execute(f"UPDATE businesses SET {sets} WHERE id=?",
                 tuple(fields[col] for col in cols) + (business_id,))
    conn.commit()
    conn.close()


def update_phone_voice(business_id, forward_to=None, voice_callback_enabled=None):
    """Persist the phone-forwarding + AI-voice-callback settings. Kept separate from
    update_business because _BUSINESS_COLS is also used for the seed INSERT (keyed to
    DEFAULT_BUSINESS), so these columns must not be added to that list."""
    sets, vals = [], []
    if forward_to is not None:
        sets.append("forward_to=?")
        vals.append(forward_to)
    if voice_callback_enabled is not None:
        sets.append("voice_callback_enabled=?")
        vals.append(1 if voice_callback_enabled else 0)
    if not sets:
        return
    conn = get_conn()
    conn.execute(f"UPDATE businesses SET {', '.join(sets)} WHERE id=?",
                 tuple(vals) + (business_id,))
    conn.commit()
    conn.close()


def get_business_by_twilio_number(number):
    """The tenant that owns a given RingBack/Twilio number (a webhook's `To`).
    Matches on the last 10 digits so +1 / formatting differences never matter."""
    key = re.sub(r"\D", "", str(number or ""))[-10:]
    if not key:
        return None
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM businesses WHERE twilio_number IS NOT NULL AND twilio_number<>''"
    ).fetchall()
    conn.close()
    for r in rows:
        if re.sub(r"\D", "", r["twilio_number"] or "")[-10:] == key:
            return dict(r)
    return None


def set_business_twilio(business_id, twilio_number, twilio_number_sid="", forward_to=None):
    """Store a business's provisioned Twilio number (and optional forward-to cell).
    Used by messaging.provision_number; not exposed on the Settings form."""
    conn = get_conn()
    if forward_to is None:
        conn.execute(
            "UPDATE businesses SET twilio_number=?, twilio_number_sid=? WHERE id=?",
            (twilio_number, twilio_number_sid, business_id))
    else:
        conn.execute(
            "UPDATE businesses SET twilio_number=?, twilio_number_sid=?, forward_to=? "
            "WHERE id=?", (twilio_number, twilio_number_sid, forward_to, business_id))
    conn.commit()
    conn.close()


# ---- Leads ----
def create_lead(business_id, name, phone, source="missed_call"):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO leads (business_id,name,phone,source,status,created_at) "
        "VALUES (?,?,?,?, 'new', ?)",
        (business_id, name, phone, source, now_iso()),
    )
    conn.commit()
    lead_id = cur.lastrowid
    conn.close()
    return lead_id


def get_lead(lead_id, business_id=None):
    """Fetch a lead. When business_id is given, only returns it if it belongs to
    that business (authorization guard), else None."""
    conn = get_conn()
    if business_id is None:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM leads WHERE id=? AND business_id=?",
                           (lead_id, business_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_lead_by_phone(business_id, phone):
    """The most recent lead for a (business, phone), matched on the last 10 digits
    so +1 / formatting differences never matter; None if none. Lets an inbound text
    or a repeat missed call attach to the caller's existing conversation."""
    key = re.sub(r"\D", "", str(phone or ""))[-10:]
    if not key:
        return None
    conn = get_conn()
    rows = conn.execute("SELECT * FROM leads WHERE business_id=? ORDER BY id DESC",
                        (business_id,)).fetchall()
    conn.close()
    for r in rows:
        if re.sub(r"\D", "", r["phone"] or "")[-10:] == key:
            return dict(r)
    return None


def list_leads(business_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM leads WHERE business_id=? ORDER BY id DESC",
                        (business_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def leads_with_stage(business_id):
    """Leads (newest first) with a triage stage available for every lead without
    opening it: scheduled (booked) / warm (has replied) / new (no reply yet)."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM leads WHERE business_id=? ORDER BY id DESC",
                        (business_id,)).fetchall()
    replied = {r[0] for r in conn.execute(
        "SELECT DISTINCT m.lead_id FROM messages m JOIN leads l ON l.id=m.lead_id "
        "WHERE m.direction='in' AND l.business_id=?", (business_id,)).fetchall()}
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["stage"] = ("scheduled" if d.get("status") == "booked"
                      else "warm" if d["id"] in replied else "new")
        out.append(d)
    # Triage order: urgent first, then the leads that need chasing (warm), then
    # new, then already-scheduled; newest first within each group.
    rank = {"warm": 0, "new": 1, "scheduled": 2}
    out.sort(key=lambda d: (0 if d.get("urgent") else 1, rank.get(d["stage"], 3), -d["id"]))
    return out


def mark_lead_urgent(lead_id):
    conn = get_conn()
    conn.execute("UPDATE leads SET urgent=1 WHERE id=?", (lead_id,))
    conn.commit()
    conn.close()


def set_lead_notes(lead_id, name="", address="", project_type="", stage="",
                   summary="", notes_msgs=0):
    """Store compressed conversation notes on a lead. Updates the name only if we
    extracted a real one (not a placeholder)."""
    conn = get_conn()
    if name and name.strip().lower() not in (
            "", "homeowner", "new caller", "unknown", "the caller", "caller"):
        conn.execute("UPDATE leads SET name=? WHERE id=?", (name.strip(), lead_id))
    conn.execute(
        "UPDATE leads SET address=?, project_type=?, stage=?, summary=?, notes_msgs=? "
        "WHERE id=?",
        (address or "", project_type or "", stage or "", summary or "", notes_msgs, lead_id),
    )
    conn.commit()
    conn.close()


# ---- Messages ----
def add_message(lead_id, direction, body, provider_sid=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO messages (lead_id,direction,body,provider_sid,created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, direction, body, provider_sid, now_iso()),
    )
    conn.commit()
    conn.close()


def set_message_delivery(provider_sid, status):
    """Update a sent SMS's delivery status from a Twilio status callback (no-op if
    we have no provider id for it)."""
    if not provider_sid:
        return
    conn = get_conn()
    conn.execute("UPDATE messages SET delivery_status=? WHERE provider_sid=?",
                 (status, provider_sid))
    conn.commit()
    conn.close()


def get_messages(lead_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE lead_id=? ORDER BY id", (lead_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Appointments ----
def book_appointment(business_id, lead_id, scheduled_for, notes="", day=None,
                     slot_time=None):
    """Create an appointment for a business. Returns True if booked, False if that
    (business, day, time) was already taken. The DB UNIQUE constraint is the
    source of truth, so a slot can never be double-booked even under a race."""
    if day is None:
        day = parse_day(scheduled_for)
    if slot_time is None:
        slot_time = time_key(scheduled_for)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO appointments (business_id,lead_id,scheduled_for,day,slot_time,"
            "notes,status,created_at) VALUES (?,?,?,?,?,?, 'booked', ?)",
            (business_id, lead_id, scheduled_for, day, slot_time, notes, now_iso()),
        )
        conn.execute("UPDATE leads SET status='booked' WHERE id=?", (lead_id,))
        conn.commit()
        booked = True
    except sqlite3.IntegrityError:
        conn.rollback()
        booked = False
    conn.close()
    return booked


def list_appointments(business_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT a.*, l.name AS lead_name, l.phone AS lead_phone "
        "FROM appointments a JOIN leads l ON l.id = a.lead_id "
        "WHERE a.business_id=? AND a.status='booked' ORDER BY a.id DESC",
        (business_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Scheduling: dates, busy days, the in-house calendar ----
def _today():
    # The app's "today" in the configured timezone (matches how times render),
    # so slot dates never drift from what the user sees.
    return datetime.now(app_tz()).date()


_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
             "wed": 2, "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4,
             "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


_TIME_12_RE = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?", re.I)
_TIME_24_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def time_key(label):
    """Canonical 24h 'HH:MM' for the time inside a window string, or None.

    Handles '9:00 AM', '2:00 PM', the compact '2pm'/'9 am', a bare '14:00', and
    full labels like 'Thu Jun 18 . 2:00 PM'. This is the single source of slot
    identity, used for offer ids, slot consumption, and the DB uniqueness
    constraint, so every spelling of the same time collapses to one value."""
    s = (label or "").strip().lower()
    if not s:
        return None
    m = _TIME_12_RE.search(s)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3).lower() == "p":
            hour += 12
        return f"{hour:02d}:{int(m.group(2) or 0):02d}"
    m = _TIME_24_RE.search(s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def parse_day(label):
    """Best-effort: turn a human window label into an ISO date or None. Handles
    'today', 'tomorrow', weekday names, and 'Mon Jun 16' / 'June 16' forms."""
    s = (label or "").strip().lower()
    if not s:
        return None
    today = _today()
    if "today" in s:
        return today.isoformat()
    if "tomorrow" in s:
        return (today + timedelta(days=1)).isoformat()
    # 'Mon DD' month-name + day (check before weekday so 'Jun 16' wins)
    m = re.search(r"\b([a-z]{3,9})\s+(\d{1,2})\b", s)
    if m and m.group(1)[:3] in _MONTHS:
        mon, dd, yr = _MONTHS[m.group(1)[:3]], int(m.group(2)), today.year
        try:
            d = date(yr, mon, dd)
        except ValueError:
            return None
        if d < today:
            try:
                d = date(yr + 1, mon, dd)
            except ValueError:
                return None
        return d.isoformat()
    # weekday name -> the next such weekday (never today)
    for name, idx in _WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", s):
            delta = (idx - today.weekday()) % 7 or 7
            return (today + timedelta(days=delta)).isoformat()
    return None


def list_busy_days(business_id):
    conn = get_conn()
    rows = conn.execute("SELECT day FROM busy_days WHERE business_id=?",
                        (business_id,)).fetchall()
    conn.close()
    return {r["day"] for r in rows}


def set_day_busy(business_id, day, busy):
    conn = get_conn()
    if busy:
        conn.execute("INSERT OR IGNORE INTO busy_days (business_id, day) VALUES (?,?)",
                     (business_id, day))
    else:
        conn.execute("DELETE FROM busy_days WHERE business_id=? AND day=?",
                     (business_id, day))
    conn.commit()
    conn.close()


def appointments_by_day(business_id):
    """{ 'YYYY-MM-DD': [ {label, slot_time, name, phone}, ... ] } for one business."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT a.day, a.scheduled_for, a.slot_time, l.name AS lead_name, "
        "l.phone AS lead_phone FROM appointments a JOIN leads l ON l.id = a.lead_id "
        "WHERE a.business_id=? AND a.day IS NOT NULL AND a.status='booked' "
        "ORDER BY a.day, a.id",
        (business_id,)
    ).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["day"], []).append(
            {"label": r["scheduled_for"], "slot_time": r["slot_time"],
             "name": r["lead_name"], "phone": r["lead_phone"]})
    return out


def upcoming_slots(business_id, limit=8, exclude_ids=None):
    """The soonest OPEN estimate windows for a business: ESTIMATE_TIMES across the
    next BOOKING_HORIZON_DAYS days, skipping busy days and times already taken.

    `exclude_ids` is an optional set of slot ids ('YYYY-MM-DD@HH:MM') to also skip
    (used to fold in a connected Google calendar's conflicts at slot granularity).
    Each slot carries a canonical `time_key` and that stable `id`."""
    busy = list_busy_days(business_id)
    exclude_ids = exclude_ids or set()
    # Which (day -> set of 24h times) are already booked, matched on time_key so
    # every spelling of a window collapses to the same identity.
    taken = {}
    for day, appts in appointments_by_day(business_id).items():
        keys = {a.get("slot_time") or time_key(a["label"]) for a in appts}
        taken[day] = {k for k in keys if k}
    out = []
    today = _today()
    for i in range(1, BOOKING_HORIZON_DAYS + 1):
        cur = today + timedelta(days=i)
        iso = cur.isoformat()
        if iso in busy:
            continue
        taken_times = taken.get(iso, set())
        for t in ESTIMATE_TIMES:
            tk = time_key(t)
            sid = f"{iso}@{tk}"
            if tk in taken_times or sid in exclude_ids:
                continue
            out.append({"day": iso, "time": t, "time_key": tk, "id": sid,
                        "label": f"{cur.strftime('%a %b ')}{cur.day} · {t}"})
            if len(out) >= limit:
                return out
    return out


def calendar_month(business_id, year, month):
    """A 6-row week grid (Sunday-start) with busy + estimate state per day."""
    first = date(year, month, 1)
    grid_start = first - timedelta(days=(first.weekday() + 1) % 7)  # Mon=0 -> Sun-start
    busy = list_busy_days(business_id)
    appts = appointments_by_day(business_id)
    today = _today()
    weeks, cur = [], grid_start
    for _w in range(6):
        week = []
        for _d in range(7):
            iso = cur.isoformat()
            week.append({
                "date": iso, "day": cur.day,
                "inMonth": cur.month == month,
                "today": cur == today, "past": cur < today,
                "busy": iso in busy, "estimates": appts.get(iso, []),
            })
            cur += timedelta(days=1)
        weeks.append(week)
    prev_m = first - timedelta(days=1)
    next_m = date(year + (month // 12), (month % 12) + 1, 1)
    return {
        "year": year, "month": month, "label": first.strftime("%B %Y"),
        "prev": f"{prev_m.year:04d}-{prev_m.month:02d}",
        "next": f"{next_m.year:04d}-{next_m.month:02d}",
        "today": today.isoformat(), "weeks": weeks,
    }


# ---- Integrations (calendar provider connections) ----
def list_integrations(business_id):
    conn = get_conn()
    rows = conn.execute("SELECT provider, connected FROM integrations WHERE business_id=?",
                        (business_id,)).fetchall()
    conn.close()
    return {r["provider"]: bool(r["connected"]) for r in rows}


def get_integration(business_id, provider):
    conn = get_conn()
    row = conn.execute("SELECT * FROM integrations WHERE business_id=? AND provider=?",
                       (business_id, provider)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_integration(business_id, provider, connected):
    conn = get_conn()
    conn.execute(
        "INSERT INTO integrations (business_id, provider, connected, connected_at) "
        "VALUES (?,?,?,?) ON CONFLICT(business_id, provider) DO UPDATE SET "
        "connected=excluded.connected, connected_at=excluded.connected_at",
        (business_id, provider, 1 if connected else 0, now_iso() if connected else None),
    )
    conn.commit()
    conn.close()
    return bool(connected)


def set_oauth_tokens(business_id, provider, access_token, refresh_token, token_expiry):
    """Store (or clear) OAuth tokens for any provider, keyed by (business_id,
    provider). Provider-agnostic sibling of set_google_tokens (no calendar_id),
    used by the Google Contacts import connection.

    On connect/refresh (access_token given): upsert and KEEP an existing refresh
    token when this response omitted one. On disconnect (access_token None): clear
    the tokens outright and mark disconnected -- a real forget, not just inactive."""
    conn = get_conn()
    if access_token is None:
        conn.execute(
            "UPDATE integrations SET connected=0, connected_at=NULL, access_token=NULL, "
            "refresh_token=NULL, token_expiry=NULL WHERE business_id=? AND provider=?",
            (business_id, provider))
    else:
        conn.execute(
            "INSERT INTO integrations (business_id, provider, connected, connected_at, "
            "access_token, refresh_token, token_expiry) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(business_id, provider) DO UPDATE SET "
            "connected=excluded.connected, connected_at=excluded.connected_at, "
            "access_token=excluded.access_token, "
            "refresh_token=COALESCE(excluded.refresh_token, integrations.refresh_token), "
            "token_expiry=excluded.token_expiry",
            (business_id, provider, 1, now_iso(), access_token, refresh_token, token_expiry))
    conn.commit()
    conn.close()


def set_google_tokens(business_id, access_token, refresh_token, token_expiry,
                      calendar_id="primary"):
    """Store (or clear) a business's Google OAuth tokens and mark it connected."""
    connected = 1 if access_token else 0
    conn = get_conn()
    conn.execute(
        "INSERT INTO integrations (business_id, provider, connected, connected_at, "
        "access_token, refresh_token, token_expiry, calendar_id) "
        "VALUES (?, 'google', ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(business_id, provider) DO UPDATE SET "
        "connected=excluded.connected, connected_at=excluded.connected_at, "
        "access_token=excluded.access_token, "
        "refresh_token=COALESCE(excluded.refresh_token, integrations.refresh_token), "
        "token_expiry=excluded.token_expiry, calendar_id=excluded.calendar_id",
        (business_id, connected, now_iso() if connected else None,
         access_token, refresh_token, token_expiry, calendar_id),
    )
    conn.commit()
    conn.close()


# ---- Callback system: inbound call log + consent/suppression ----
def log_call(business_id, call_sid, from_number="", to_number="", dial_status="",
             answered_by="", missed=0, lead_id=None, category=None, engaged=None):
    """Record (or update) an inbound call. Idempotent on call_sid because Twilio
    retries webhooks and fires several events per call; later events update the
    outcome fields rather than inserting duplicates. `category`/`engaged` carry the
    triage verdict (COALESCEd so a later event never nulls them)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO calls (business_id, lead_id, call_sid, from_number, to_number,"
        " dial_status, answered_by, missed, category, engaged, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(call_sid) DO UPDATE SET "
        "  dial_status=excluded.dial_status, answered_by=excluded.answered_by, "
        "  missed=excluded.missed, lead_id=COALESCE(excluded.lead_id, calls.lead_id), "
        "  category=COALESCE(excluded.category, calls.category), "
        "  engaged=COALESCE(excluded.engaged, calls.engaged)",
        (business_id, lead_id, call_sid, from_number, to_number, dial_status,
         answered_by, 1 if missed else 0, category,
         None if engaged is None else (1 if engaged else 0), now_iso()))
    conn.commit()
    conn.close()


def get_call(call_id, business_id):
    """A single call row, scoped to the business (so cross-tenant ids are rejected)."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM calls WHERE id=? AND business_id=?",
                       (call_id, business_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_call_engaged(call_id, lead_id=None):
    """Flip a previously-screened call to engaged (the owner's dashboard override)."""
    conn = get_conn()
    conn.execute("UPDATE calls SET engaged=1, lead_id=COALESCE(?, lead_id) WHERE id=?",
                 (lead_id, call_id))
    conn.commit()
    conn.close()


def recent_screened_calls(business_id, limit=8):
    """Recent missed callers we screened OUT for being non-prospects (the owner's
    directory: personal/vendor/blocked), grouped to the most recent call per number,
    for the dashboard 'Screened calls' strip + one-tap override. Opt-outs are
    intentionally excluded -- re-texting a STOP is never offered."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, from_number, category, MAX(created_at) AS created_at, "
        "COUNT(*) AS times FROM calls "
        "WHERE business_id=? AND engaged=0 AND category IN ('personal','vendor','blocked') "
        "AND from_number IS NOT NULL AND from_number<>'' "
        "GROUP BY from_number ORDER BY created_at DESC LIMIT ?",
        (business_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_voice_consent(business_id, number, ok=True):
    """Record affirmative (or revoked) consent to place an AI voice call to a
    consumer. The FCC treats AI voice as a robocall, so we only call after the
    customer asks; this is that opt-in. Keyed by the last 10 digits."""
    key = re.sub(r"\D", "", str(number or ""))[-10:]
    conn = get_conn()
    conn.execute(
        "INSERT INTO contacts_consent (business_id, consumer_number, voice_ok, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(business_id, consumer_number) DO UPDATE SET "
        "voice_ok=excluded.voice_ok, updated_at=excluded.updated_at",
        (business_id, key, 1 if ok else 0, now_iso()))
    conn.commit()
    conn.close()


def get_consent(business_id, number):
    """The consent/suppression row for a (business, consumer number), or None.
    The consumer number is keyed by its last 10 digits."""
    key = re.sub(r"\D", "", str(number or ""))[-10:]
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM contacts_consent WHERE business_id=? AND consumer_number=?",
        (business_id, key)).fetchone()
    conn.close()
    return dict(row) if row else None


def is_suppressed(business_id, number):
    """True if this consumer opted out for this business (blocks SMS and voice).
    Checked before every outbound message/call."""
    row = get_consent(business_id, number)
    return bool(row and row["opted_out"])


def set_opt_out(business_id, number, source="reply"):
    """Mark a consumer opted out (honoring STOP / any reasonable revocation)."""
    key = re.sub(r"\D", "", str(number or ""))[-10:]
    conn = get_conn()
    conn.execute(
        "INSERT INTO contacts_consent (business_id, consumer_number, opted_out, "
        " opted_out_at, source, updated_at) VALUES (?,?,1,?,?,?) "
        "ON CONFLICT(business_id, consumer_number) DO UPDATE SET "
        "  opted_out=1, opted_out_at=excluded.opted_out_at, source=excluded.source, "
        "  updated_at=excluded.updated_at",
        (business_id, key, now_iso(), source, now_iso()))
    conn.commit()
    conn.close()


# ---- Caller triage: the per-business contact directory ----
# Who a number IS to this business, so we never cold-pitch a non-prospect (the
# owner's mom, the power company, a known nuisance). Keyed by the last 10 digits,
# like the consent ledger, so +1 / formatting never matters.
CONTACT_CATEGORIES = ("prospect", "customer", "personal", "vendor", "blocked")


def _digits10(number):
    return re.sub(r"\D", "", str(number or ""))[-10:]


def get_contact(business_id, number):
    """The directory entry for a (business, number), or None if we have not
    classified this caller."""
    key = _digits10(number)
    if not key:
        return None
    conn = get_conn()
    row = conn.execute("SELECT * FROM contacts WHERE business_id=? AND number=?",
                       (business_id, key)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_contact(business_id, number, category, name=None, note=None, source="owner"):
    """Upsert a directory entry (the owner tagging a number, or an auto-learn).
    `category` is the source of truth for engage-vs-screen; an unknown category
    falls back to 'prospect'. name/note are preserved when passed None on update."""
    key = _digits10(number)
    if not key:
        return None
    if category not in CONTACT_CATEGORIES:
        category = "prospect"
    conn = get_conn()
    conn.execute(
        "INSERT INTO contacts (business_id, number, name, category, note, source, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(business_id, number) DO UPDATE SET "
        "  category=excluded.category, "
        "  name=COALESCE(excluded.name, contacts.name), "
        "  note=COALESCE(excluded.note, contacts.note), "
        "  source=excluded.source, updated_at=excluded.updated_at",
        (business_id, key, name, category, note, source, now_iso(), now_iso()))
    conn.commit()
    conn.close()
    return key


def list_contacts(business_id):
    """All directory entries for a business (non-prospects first), for the Settings
    'Caller screening' card."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM contacts WHERE business_id=? ORDER BY "
        "CASE category WHEN 'blocked' THEN 0 WHEN 'vendor' THEN 1 WHEN 'personal' "
        "THEN 2 WHEN 'customer' THEN 3 ELSE 4 END, id DESC", (business_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_contact(business_id, number):
    """Forget a directory entry (the number reverts to an unscreened prospect)."""
    conn = get_conn()
    conn.execute("DELETE FROM contacts WHERE business_id=? AND number=?",
                 (business_id, _digits10(number)))
    conn.commit()
    conn.close()


def learn_customer(business_id, number, name=None):
    """Auto-mark a number as a known customer after they book, so a future call from
    them is never mistaken for a cold lead. NEVER overrides an owner-set
    personal/vendor/blocked tag -- that intent wins."""
    key = _digits10(number)
    if not key:
        return
    existing = get_contact(business_id, number)
    if existing and existing.get("category") in NON_PROSPECT_CATEGORIES:
        return
    set_contact(business_id, number, "customer", name=name, source="auto-booking")


# Mirror of triage.NON_PROSPECT, kept here to avoid a db->triage import cycle.
NON_PROSPECT_CATEGORIES = ("personal", "vendor", "blocked")


# ---- Caller triage: the suggestion / "for review" queue (QuickBooks-style) ----
# RingBack observes a caller and PROPOSES a category; the owner confirms with one
# tap, recategorizes, or dismisses. Suggestions never auto-apply.
def caller_signals(business_id):
    """Per-caller-number behavioral aggregates that drive suggestions: how many
    times they were a missed call, how many texts they sent back, and how many
    estimates they booked. Keyed by the last 10 digits so formatting never matters."""
    conn = get_conn()
    calls = conn.execute("SELECT from_number, missed FROM calls WHERE business_id=?",
                         (business_id,)).fetchall()
    leads = conn.execute("SELECT id, phone, name FROM leads WHERE business_id=?",
                         (business_id,)).fetchall()
    inbound = dict(conn.execute(
        "SELECT m.lead_id, COUNT(*) FROM messages m JOIN leads l ON l.id=m.lead_id "
        "WHERE l.business_id=? AND m.direction='in' GROUP BY m.lead_id", (business_id,)).fetchall())
    booked = dict(conn.execute(
        "SELECT lead_id, COUNT(*) FROM appointments WHERE business_id=? AND status='booked' "
        "GROUP BY lead_id", (business_id,)).fetchall())
    conn.close()
    stats = {}

    def slot(k):
        return stats.setdefault(k, {"number": k, "name": "", "missed_calls": 0,
                                    "inbound_msgs": 0, "booked": 0})
    for r in calls:
        k = _digits10(r["from_number"])
        if k and r["missed"]:
            slot(k)["missed_calls"] += 1
    for l in leads:
        k = _digits10(l["phone"])
        if not k:
            continue
        s = slot(k)
        if l["name"] and not s["name"]:
            s["name"] = l["name"]
        s["inbound_msgs"] += inbound.get(l["id"], 0)
        s["booked"] += booked.get(l["id"], 0)
    return list(stats.values())


def upsert_suggestion(business_id, number, name, category, reason, source="behavior"):
    """Record (or refresh) a pending suggestion for a number. A suggestion the owner
    already accepted or dismissed is left untouched (the WHERE on the upsert), so we
    never nag about a number they've already decided."""
    key = _digits10(number)
    if not key:
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO contact_suggestions (business_id, number, name, suggested_category, "
        "reason, source, status, created_at, updated_at) VALUES (?,?,?,?,?,?, 'pending', ?, ?) "
        "ON CONFLICT(business_id, number) DO UPDATE SET "
        "  name=COALESCE(excluded.name, contact_suggestions.name), "
        "  suggested_category=excluded.suggested_category, reason=excluded.reason, "
        "  source=excluded.source, updated_at=excluded.updated_at "
        "WHERE contact_suggestions.status='pending'",
        (business_id, key, name, category, reason, source, now_iso(), now_iso()))
    conn.commit()
    conn.close()


def list_suggestions(business_id, status="pending"):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM contact_suggestions WHERE business_id=? AND status=? ORDER BY id DESC",
        (business_id, status)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_pending_suggestions(business_id):
    return count_suggestions(business_id, "pending")


def count_suggestions(business_id, status="pending"):
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM contact_suggestions WHERE business_id=? AND status=?",
                     (business_id, status)).fetchone()[0]
    conn.close()
    return n


def get_suggestion(sug_id, business_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM contact_suggestions WHERE id=? AND business_id=?",
                       (sug_id, business_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_suggestion_status(sug_id, status):
    conn = get_conn()
    conn.execute("UPDATE contact_suggestions SET status=?, updated_at=? WHERE id=?",
                 (status, now_iso(), sug_id))
    conn.commit()
    conn.close()


# ---- Contact form (marketing site) ----
def add_contact_message(name, email, message):
    conn = get_conn()
    conn.execute(
        "INSERT INTO contact_messages (name, email, message, created_at) VALUES (?,?,?,?)",
        (name, email, message, now_iso()),
    )
    conn.commit()
    conn.close()


# ---- Owner alerts (notifications) ----
def update_alert_prefs(business_id, fields):
    """Persist a business's owner-alert preferences (Settings 'Owner alerts' card).
    Only the alert columns are touched, so it never disturbs the profile fields."""
    cols = ["alert_email", "alert_sms", "alert_on_lead", "alert_on_booking",
            "alert_on_urgent"]
    present = [col for col in cols if col in fields]
    if not present:
        return
    conn = get_conn()
    sets = ", ".join(f"{col}=?" for col in present)
    conn.execute(f"UPDATE businesses SET {sets} WHERE id=?",
                 tuple(fields[col] for col in present) + (business_id,))
    conn.commit()
    conn.close()


def owner_email(business_id):
    """The login email of the business's owner (its earliest-registered user), or
    '' if none. The default destination for email alerts."""
    conn = get_conn()
    row = conn.execute(
        "SELECT email FROM users WHERE business_id=? ORDER BY id LIMIT 1",
        (business_id,)).fetchone()
    conn.close()
    return row["email"] if row else ""


def add_alert(business_id, kind, channel, target, status, dedupe_key, body):
    """Record one alert delivery attempt (audit trail + in-app feed + de-dupe)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO alerts (business_id, kind, channel, target, status, "
        "dedupe_key, body, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (business_id, kind, channel, target, status, dedupe_key, body, now_iso()))
    conn.commit()
    conn.close()


def alert_recent(business_id, dedupe_key, within_seconds):
    """True if an alert with this dedupe_key was already recorded for the business
    within the last `within_seconds` (collapses bursts / double-fires)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).isoformat()
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE business_id=? AND dedupe_key=? AND created_at>=? "
        "LIMIT 1", (business_id, dedupe_key, cutoff)).fetchone()
    conn.close()
    return bool(row)


def recent_alerts(business_id, limit=8):
    """The most recent in-app alert events for a business (one row per event, since
    every event records exactly one 'inapp' row). Newest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT kind, body, status, created_at FROM alerts "
        "WHERE business_id=? AND channel='inapp' ORDER BY id DESC LIMIT ?",
        (business_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Reminders & follow-ups (scheduled outbound queue) ----
def list_businesses():
    """All tenant businesses (with prefs), for the scheduler to iterate."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM businesses ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_reminder_prefs(business_id, fields):
    """Persist reminder/follow-up prefs (Settings card). Touches only those cols."""
    cols = ["reminders_enabled", "followups_enabled", "reminder_lead_hours"]
    present = [col for col in cols if col in fields]
    if not present:
        return
    conn = get_conn()
    sets = ", ".join(f"{col}=?" for col in present)
    conn.execute(f"UPDATE businesses SET {sets} WHERE id=?",
                 tuple(fields[col] for col in present) + (business_id,))
    conn.commit()
    conn.close()


def add_scheduled_message(business_id, lead_id, appointment_id, kind, send_at, body):
    """Queue an outbound message (reminder/followup). Returns its id, or None if a
    duplicate follow-up was blocked by the one-per-lead unique index (so a second
    scheduler driver racing the first can't double-queue a nudge). Reminders are
    unaffected by that index (it's WHERE kind='followup')."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO scheduled_messages (business_id, lead_id, appointment_id, kind, "
            "send_at, body, status, created_at) VALUES (?,?,?,?,?,?, 'pending', ?)",
            (business_id, lead_id, appointment_id, kind, send_at, body, now_iso()))
        conn.commit()
        sid = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        sid = None
    conn.close()
    return sid


def find_appointment(business_id, lead_id, day, slot_time):
    """The booked appointment for a (business, lead, day, slot), or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM appointments WHERE business_id=? AND lead_id=? AND day=? "
        "AND slot_time=? AND status='booked' ORDER BY id DESC LIMIT 1",
        (business_id, lead_id, day, slot_time)).fetchone()
    conn.close()
    return dict(row) if row else None


def cancel_lead_pending_reminders(lead_id):
    """Cancel a lead's pending reminders (a reschedule replaces, not duplicates)."""
    conn = get_conn()
    conn.execute("UPDATE scheduled_messages SET status='canceled' "
                 "WHERE lead_id=? AND kind='reminder' AND status='pending'", (lead_id,))
    conn.commit()
    conn.close()


def cancel_appointment_reminders(appointment_id):
    """Cancel pending reminders for an appointment (call when it's canceled)."""
    conn = get_conn()
    conn.execute("UPDATE scheduled_messages SET status='canceled' "
                 "WHERE appointment_id=? AND kind='reminder' AND status='pending'",
                 (appointment_id,))
    conn.commit()
    conn.close()


def due_scheduled_messages(now_iso_str):
    """Pending messages due now (all businesses), with the lead's phone/name and any
    linked appointment's day/slot/status."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT s.*, l.phone AS lead_phone, l.name AS lead_name, "
        "a.day AS appt_day, a.slot_time AS appt_slot, a.status AS appt_status "
        "FROM scheduled_messages s JOIN leads l ON l.id = s.lead_id "
        "LEFT JOIN appointments a ON a.id = s.appointment_id "
        "WHERE s.status='pending' AND s.send_at <= ? ORDER BY s.send_at",
        (now_iso_str,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def claim_scheduled_message(sched_id):
    """Atomically claim a pending message (mark 'sent') so a concurrent tick or a
    restart mid-send can't double-send. Returns True only for the claimer."""
    conn = get_conn()
    cur = conn.execute("UPDATE scheduled_messages SET status='sent', sent_at=? "
                       "WHERE id=? AND status='pending'", (now_iso(), sched_id))
    conn.commit()
    claimed = cur.rowcount == 1
    conn.close()
    return claimed


def mark_scheduled(sched_id, status):
    """Set a message's status. `sent_at` is recorded for a real OR simulated send
    (both mean 'processed at this time'); cleared for other states."""
    conn = get_conn()
    conn.execute("UPDATE scheduled_messages SET status=?, sent_at=? WHERE id=?",
                 (status, now_iso() if status in ("sent", "simulated") else None, sched_id))
    conn.commit()
    conn.close()


def followup_candidate_rows(business_id):
    """Warm leads (replied, not booked) with their last-message time and whether a
    follow-up was ever queued. The 'is it cold yet' time decision is left to the
    caller (pure + testable)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT l.id, l.name, l.phone, MAX(m.created_at) AS last_msg_at, "
        "EXISTS(SELECT 1 FROM scheduled_messages s WHERE s.lead_id=l.id "
        "       AND s.kind='followup') AS has_followup "
        "FROM leads l JOIN messages m ON m.lead_id = l.id "
        "WHERE l.business_id=? AND l.status != 'booked' "
        "AND EXISTS(SELECT 1 FROM messages mi WHERE mi.lead_id=l.id AND mi.direction='in') "
        "GROUP BY l.id", (business_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reminders_by_appointment(business_id):
    """{appointment_id: {'status', 'send_at'}} (newest row per appointment) for the
    dashboard's reminder-state column."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT appointment_id, status, send_at FROM scheduled_messages "
        "WHERE business_id=? AND kind='reminder' AND appointment_id IS NOT NULL "
        "ORDER BY id", (business_id,)).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out[r["appointment_id"]] = {"status": r["status"], "send_at": r["send_at"]}
    return out


# ---- Appointment cancellation / reschedule ----
def lead_booked_appointments(business_id, lead_id):
    """A lead's currently-booked appointments (oldest first), scoped to the business."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM appointments WHERE business_id=? AND lead_id=? AND status='booked' "
        "ORDER BY id", (business_id, lead_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cancel_appointment(business_id, appointment_id):
    """Cancel a booked appointment. The slot frees automatically (status->canceled
    drops it from upcoming_slots + the partial UNIQUE booked-slot index), its
    pending reminders are canceled, and the lead reverts to a non-booked stage if it
    has no other booked estimate. Returns the canceled appointment row, or None if it
    wasn't a booked appointment of this business (so cross-tenant ids are rejected)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM appointments WHERE id=? AND business_id=? AND status='booked'",
        (appointment_id, business_id)).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("UPDATE appointments SET status='canceled' WHERE id=?", (appointment_id,))
    lead_id = row["lead_id"]
    still_booked = conn.execute(
        "SELECT 1 FROM appointments WHERE business_id=? AND lead_id=? AND status='booked' "
        "LIMIT 1", (business_id, lead_id)).fetchone()
    if not still_booked:
        # leads_with_stage recomputes this as 'warm' (they replied) or 'new'.
        conn.execute("UPDATE leads SET status='new' WHERE id=?", (lead_id,))
    conn.commit()
    conn.close()
    cancel_appointment_reminders(appointment_id)  # pending reminders -> canceled
    return dict(row)


# ---- ROI / analytics (Feature 4: read-only aggregation) ----
def set_avg_job_value(business_id, value):
    """Owner-set average job value used to ESTIMATE recovered revenue (None clears)."""
    conn = get_conn()
    conn.execute("UPDATE businesses SET avg_job_value=? WHERE id=?", (value, business_id))
    conn.commit()
    conn.close()


def _roi_series(lead_days, booked_days, start, end):
    """Pure + testable: per-day counts of leads + booked estimates from `start` to
    `end` (inclusive). Inputs are lists of date objects."""
    from collections import Counter
    lc, bc = Counter(lead_days), Counter(booked_days)
    out, d = [], start
    while d <= end:
        out.append({"date": d.isoformat(), "leads": lc.get(d, 0), "booked": bc.get(d, 0)})
        d += timedelta(days=1)
    return out


def analytics(business_id, days=30):
    """ROI metrics for a business over the last `days` (None = all time), bucketed by
    business-local day. Read-only + tenant-scoped. 'revenue' is an ESTIMATE (booked x
    avg_job_value) or None when the owner hasn't set a value. Conversion is guarded
    against divide-by-zero."""
    biz = get_business(business_id)
    try:
        avg = float(biz.get("avg_job_value")) if biz.get("avg_job_value") not in (None, "") else None
    except (TypeError, ValueError):
        avg = None
    conn = get_conn()
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        lead_rows = conn.execute(
            "SELECT created_at FROM leads WHERE business_id=? AND created_at>=?",
            (business_id, cutoff)).fetchall()
        appt_rows = conn.execute(
            "SELECT created_at FROM appointments WHERE business_id=? AND status='booked' "
            "AND created_at>=?", (business_id, cutoff)).fetchall()
    else:
        lead_rows = conn.execute(
            "SELECT created_at FROM leads WHERE business_id=?", (business_id,)).fetchall()
        appt_rows = conn.execute(
            "SELECT created_at FROM appointments WHERE business_id=? AND status='booked'",
            (business_id,)).fetchall()
    conn.close()
    tz = app_tz()

    def localday(iso):
        try:
            return datetime.fromisoformat(iso).astimezone(tz).date()
        except (TypeError, ValueError):
            return None

    lead_days = [d for d in (localday(r[0]) for r in lead_rows) if d]
    booked_days = [d for d in (localday(r[0]) for r in appt_rows) if d]
    today = datetime.now(tz).date()
    if days:
        start = today - timedelta(days=int(days) - 1)
    else:
        start = min(lead_days + booked_days) if (lead_days or booked_days) else today
    series = _roi_series(lead_days, booked_days, start, today)
    leads_n, booked_n = len(lead_days), len(booked_days)
    conversion = round(booked_n / leads_n * 100) if leads_n else 0
    revenue = int(round(booked_n * avg)) if avg else None
    return {
        "totals": {"leads": leads_n, "booked": booked_n,
                   "conversion": conversion, "revenue": revenue},
        "series": series, "avg_job_value": avg, "days": days,
    }
