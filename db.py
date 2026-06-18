"""SQLite storage for FirstBack. File-based, zero-config.

Multi-tenant: every business (tenant) owns its own leads, appointments, busy
days, and integrations, all scoped by `business_id`. `users` log in and map to
one business. "Client zero" (Heritage House Painting) is business id 1.
"""
import re
import sys
import sqlite3
from datetime import datetime, timezone, date, timedelta
import token_crypto
from config import (DB_PATH, DB_BACKUP_PATH, DEFAULT_BUSINESS, ESTIMATE_TIMES,
                    BOOKING_HORIZON_DAYS, DEFAULT_WORKING_DAYS, DEFAULT_BUFFER_MINUTES, app_tz)

# `available_slots` is intentionally omitted: availability comes from the
# in-house calendar now, so we no longer seed or update that legacy column.
_BUSINESS_COLS = ["name", "trade", "service_area", "hours", "owner_name",
                  "phone", "ai_instructions",
                  # Scheduling preferences (Phase 1): the owner shapes their own
                  # availability instead of the global ESTIMATE_TIMES default.
                  "estimate_times", "working_days", "buffer_minutes",
                  # Phase 3: the owner's public Google review link (review-request copy).
                  "review_link"]


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    # Never let a lock hang the process forever (e.g. during a deploy while the prior
    # instance is still releasing the DB on the shared disk). Wait briefly, then error.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# Shared data-core (trades_core kernel): timestamp + users/auth CRUD + assistant-subsystem
# helpers, byte-identical with JobMagnet. Inject our connection factory, then re-export the
# names so every existing db.now_iso()/db.get_user()/… call site is unchanged.
import db_core as _core
_core.get_conn = get_conn
from db_core import (now_iso, get_user, get_user_by_email, create_user,  # noqa: E402,F401
                     log_turn, get_convo_turns, flag_counts)


# ---- Durable local-disk mode (the fix for the network-FS boot hang) ----
# When DB_BACKUP_PATH is set, SQLite runs on DB_PATH (a fast LOCAL disk where it never
# hangs) and we keep a durable single-file snapshot at DB_BACKUP_PATH (e.g. Render's
# /var/data). Only PLAIN file copies ever touch the network disk -- never a SQLite open --
# so the lock/mmap hang can't happen. See [[reference-firstback-wal-boot-hazard]].
def _backup_enabled():
    import os
    return bool(DB_BACKUP_PATH) and os.fspath(DB_BACKUP_PATH) != os.fspath(DB_PATH)


def restore_from_backup_if_needed(live=None, backup=None):
    """Seed the live (local) DB from the durable backup when the local file is missing
    (a fresh container). A plain file copy of the at-rest single-file backup -- no SQLite
    open of the network file, so it can't hang. No-op when backup mode is off or the live
    DB already exists (a warm restart keeps its fresher local copy)."""
    import os, shutil
    live = os.fspath(live or DB_PATH)
    backup = os.fspath(backup if backup is not None else (DB_BACKUP_PATH or ""))
    if not backup or backup == live or os.path.exists(live) or not os.path.exists(backup):
        return
    try:
        d = os.path.dirname(live)
        if d:
            os.makedirs(d, exist_ok=True)
        shutil.copy2(backup, live)
        print(f"[firstback] restored DB from durable backup: {backup} -> {live}",
              file=sys.stderr, flush=True)
    except OSError as e:
        print(f"[firstback] DB restore failed ({e}); starting from a fresh local DB",
              file=sys.stderr, flush=True)


def _business_count(path):
    """Best-effort COUNT(*) of `businesses` in a SQLite file; -1 if unreadable/absent.
    Used purely as an anti-clobber guard for backups."""
    import os
    if not os.path.exists(path):
        return -1
    try:
        c = sqlite3.connect(path, timeout=10)
        try:
            return c.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return -1


def backup_to_durable(live=None, backup=None):
    """Snapshot the live DB to the durable backup path. Consistent snapshot via
    VACUUM INTO (to a LOCAL temp), then plain copy + atomic os.replace onto the backup
    disk -- no SQLite open of the network file. Anti-clobber: refuses to overwrite a
    populated backup with an empty live DB (so a failed restore can never wipe data).
    Best-effort; never raises."""
    import os, shutil, tempfile
    live = os.fspath(live or DB_PATH)
    backup = os.fspath(backup if backup is not None else (DB_BACKUP_PATH or ""))
    if not backup or backup == live or not os.path.exists(live):
        return
    if _business_count(live) <= 0 and _business_count(backup) > 0:
        print("[firstback] backup skipped: live DB empty but durable backup is populated",
              file=sys.stderr, flush=True)
        return
    tmpdir = tempfile.mkdtemp()
    try:
        snap = os.path.join(tmpdir, "snapshot.db")
        src = sqlite3.connect(live, timeout=30)
        try:
            src.execute("VACUUM INTO ?", (snap,))   # consistent single-file snapshot, local
        finally:
            src.close()
        d = os.path.dirname(backup)
        if d:
            os.makedirs(d, exist_ok=True)
        staged = backup + ".staging"
        shutil.copy2(snap, staged)                   # plain write to the network disk
        os.replace(staged, backup)                   # atomic within the backup filesystem
    except (OSError, sqlite3.Error) as e:
        print(f"[firstback] durable backup failed ({e})", file=sys.stderr, flush=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


_backup_thread_started = False


def start_backup_daemon(interval=60):
    """Start the periodic durable-backup loop once, plus an atexit final snapshot so a
    graceful deploy/shutdown loses nothing. No-op when backup mode is off."""
    global _backup_thread_started
    if not _backup_enabled() or _backup_thread_started:
        return
    _backup_thread_started = True
    import threading, atexit, time
    atexit.register(backup_to_durable)
    def _loop():
        while True:
            time.sleep(interval)
            backup_to_durable()
    threading.Thread(target=_loop, daemon=True).start()
    print(f"[firstback] durable DB backup started (every {interval}s -> {DB_BACKUP_PATH})",
          file=sys.stderr, flush=True)


def _recover_network_fs_db(db_path):
    """Heal a SQLite DB stuck in WAL mode on Render's network-attached /var/data disk.

    WAL's shared-memory (-shm / mmap) is unreliable over a network filesystem, so if
    stale `-wal`/`-shm` sidecars are present, *every* boot hangs the moment SQLite opens
    the file ("No open HTTP ports detected") -- with or without an overlapping deploy.
    We can't safely open it in place (that open is what hangs), so we recover on LOCAL
    disk, where the -shm mmap works: copy the file out, checkpoint the -wal back IN (so
    no committed data is lost), convert to DELETE, copy the clean single file back, and
    drop the sidecars. Backs the live file up first. No-op (and near-instant) when the
    DB is already healthy, so a normal boot pays nothing.
    """
    import os, shutil, tempfile
    db_path = os.fspath(db_path)  # accept a Path or str; below we build "-wal"/"-shm" siblings
    wal, shm = db_path + "-wal", db_path + "-shm"
    if not (os.path.exists(wal) or os.path.exists(shm)):
        return  # healthy: no WAL sidecars -> normal boot
    try:
        shutil.copy2(db_path, db_path + ".bak-wal-recover")
        if os.path.exists(wal):
            shutil.copy2(wal, db_path + ".bak-wal-recover-wal")
    except OSError:
        pass
    tmp = tempfile.mkdtemp()
    try:
        local = os.path.join(tmp, "recover.db")
        shutil.copy2(db_path, local)
        if os.path.exists(wal):
            shutil.copy2(wal, local + "-wal")  # carry committed-but-unflushed transactions
        rc = sqlite3.connect(local, timeout=60)
        try:
            rc.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # fold -wal into the main db
            rc.execute("PRAGMA journal_mode=DELETE")       # leave WAL behind for good
            rc.execute("PRAGMA integrity_check")
        finally:
            rc.close()
        shutil.copy2(local, db_path)                       # put back the clean DELETE-mode file
        for f in (wal, shm):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def init_db():
    # FIRST, before anything opens the DB: in durable local-disk mode, seed the local DB
    # from the network-disk backup if it's missing (plain copy, no hang). Then heal any
    # WAL-stuck file (a near-no-op on the local disk) before the first open.
    restore_from_backup_if_needed()
    import os as _os
    _live_dir = _os.path.dirname(_os.fspath(DB_PATH))
    if _live_dir:
        _os.makedirs(_live_dir, exist_ok=True)   # ensure the (local) DB dir exists on a fresh boot
    _recover_network_fs_db(DB_PATH)
    conn = get_conn()
    c = conn.cursor()
    # Keep the file in DELETE (rollback-journal) mode -- correct and safe on Render's
    # network-attached /var/data disk, where WAL's -shm mmap is unreliable. Recovery
    # above guarantees no live -wal remains, so this plain set never engages -shm.
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
        -- Call screening: a per-number reputation cache (Tier 2 paid lookup) and a
        -- cross-tenant spam-flag ledger (Tier 2.5 crowdsource). Both keyed by the
        -- last 10 digits so formatting never matters; reputation is GLOBAL (not
        -- per-business) since a robocaller's line type / spam score is the same for
        -- everyone, which also lets one tenant's cached lookup serve all tenants.
        CREATE TABLE IF NOT EXISTS number_reputation (
            number TEXT PRIMARY KEY, line_type TEXT, spam_score INTEGER,
            source TEXT, checked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS spam_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, number TEXT NOT NULL, created_at TEXT,
            UNIQUE(business_id, number)
        );
        CREATE INDEX IF NOT EXISTS idx_spam_flags_number ON spam_flags(number);

        -- Command-center memory: every conversation the owner has with the assistant, so
        -- we can replay it, call out the weak spots, and learn from confirmed corrections.
        CREATE TABLE IF NOT EXISTS assistant_convos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, session_key TEXT, browser_key TEXT,
            started_at TEXT, last_at TEXT
        );
        CREATE TABLE IF NOT EXISTS assistant_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            convo_id INTEGER NOT NULL, business_id INTEGER NOT NULL,
            role TEXT, content TEXT, tool TEXT, status TEXT, created_at TEXT
        );
        -- What a turn put on screen (leads/appointments), so a later turn can resolve
        -- "text her back" / "book the second one" to the right record. Tenant-scoped.
        CREATE TABLE IF NOT EXISTS assistant_turn_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, convo_id INTEGER NOT NULL, turn_id INTEGER NOT NULL,
            kind TEXT, entity_id INTEGER, name TEXT, phone TEXT, ordinal INTEGER, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS assistant_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, convo_id INTEGER, turn_id INTEGER,
            kind TEXT, detail TEXT, created_at TEXT, resolved INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS assistant_learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, pattern TEXT, action TEXT, answer TEXT,
            source_turn_id INTEGER, confirmed INTEGER DEFAULT 0, uses INTEGER DEFAULT 0,
            created_at TEXT
        );
        -- Per-tenant action counters (sliding window key) for rate limiting the assistant,
        -- and an audit trail of gated actions the owner confirmed.
        CREATE TABLE IF NOT EXISTS rate_limits (
            business_id INTEGER NOT NULL, k TEXT NOT NULL, n INTEGER DEFAULT 0,
            PRIMARY KEY (business_id, k)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL, action TEXT, detail TEXT, created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_biz ON audit_log(business_id, id);
        CREATE INDEX IF NOT EXISTS idx_aturns_convo ON assistant_turns(convo_id);
        CREATE INDEX IF NOT EXISTS idx_aturns_biz ON assistant_turns(business_id);
        CREATE INDEX IF NOT EXISTS idx_aents_convo ON assistant_turn_entities(convo_id);
        CREATE INDEX IF NOT EXISTS idx_aflags_biz ON assistant_flags(business_id);
        CREATE INDEX IF NOT EXISTS idx_alearn_biz ON assistant_learnings(business_id);
        -- Platform meta: a simple key-value store for internal signals (e.g. heartbeat).
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # Migration: add `urgent` to older databases that predate the column.
    cols = [r[1] for r in c.execute("PRAGMA table_info(leads)").fetchall()]
    if "urgent" not in cols:
        c.execute("ALTER TABLE leads ADD COLUMN urgent INTEGER DEFAULT 0")
    # Migration: durable browser_key on conversations (reload continuity for memory/anaphora).
    acols = [r[1] for r in c.execute("PRAGMA table_info(assistant_convos)").fetchall()]
    if "browser_key" not in acols:
        c.execute("ALTER TABLE assistant_convos ADD COLUMN browser_key TEXT")
    # The browser_key index is built HERE, after the column is guaranteed -- never inside the
    # CREATE-TABLE executescript above, where an older assistant_convos (table exists but predates
    # the column) makes the index reference a missing column and crashes init_db on boot.
    c.execute("CREATE INDEX IF NOT EXISTS idx_aconvos_browser "
              "ON assistant_convos(business_id, browser_key)")
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

    # businesses gain a `phone` (the FirstBack texting number, shown in the demo).
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
            ("voice_callback_enabled", "INTEGER DEFAULT 0"),
            # Go-Live wizard (connections.py): A2P registration intake the founder
            # submits to Twilio, the synced messaging-service sid, the submission
            # timestamp, and whether the owner has confirmed carrier call-forwarding
            # (which can't be verified server-side). Set by the wizard, NOT the
            # Settings form, so they stay out of _BUSINESS_COLS like the rest above.
            ("legal_business_name", "TEXT"), ("ein", "TEXT"),
            ("business_address", "TEXT"), ("website", "TEXT"),
            ("a2p_messaging_service_sid", "TEXT"), ("a2p_submitted_at", "TEXT"),
            ("forwarding_confirmed", "INTEGER DEFAULT 0"),
            # Whether the provisioned number actually has its Twilio Voice + SMS
            # webhooks wired (set by messaging.provision_number on success). A number
            # without webhooks can receive nothing, so it must NOT read as "live".
            ("webhooks_wired", "INTEGER DEFAULT 0"),
            # Phase 3 growth engine: the owner's public review link (used in review-request
            # copy) and the opt-in flag for auto-queued growth touches (default OFF -- the
            # feed + one-tap gated sends are always live; auto-texting is explicit opt-in).
            ("review_link", "TEXT"), ("growth_on", "INTEGER DEFAULT 0")):
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
    # `engaged` whether we texted back; `screen_status` the verdict label
    # (trusted/prospect/review/screened_spam/screened_contact/opted_out);
    # `spam_score` the 0-100 score; `screen_reasons` a "; "-joined explanation;
    # `screen_mode` whether the verdict was ENFORCED (acted on) or only observed in
    # MONITOR mode (logged but still texted) -- so the stat never claims a text was
    # saved when it wasn't.
    for col, ddl in (("category", "TEXT"), ("engaged", "INTEGER"),
                     ("screen_status", "TEXT"), ("spam_score", "INTEGER"),
                     ("screen_reasons", "TEXT"), ("screen_mode", "TEXT")):
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
                     ("avg_job_value", "REAL"),   # owner-set; powers the ROI revenue estimate
                     # When the owner paused the first-run setup chaperone (ISO ts, or NULL).
                     ("chaperone_dismissed_at", "TEXT"),
                     # Per-business call-screening mode (off|monitor|enforce). NULL ->
                     # inherit the app-wide config.SCREEN_MODE default, so existing
                     # tenants behave exactly as before until the owner chooses.
                     ("screen_mode", "TEXT")):
        if col not in biz_cols:
            c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")

    # businesses gain owner-set scheduling preferences (Phase 1). All NULL/blank ->
    # fall back to config defaults, so existing tenants behave exactly as before:
    #   estimate_times  CSV of time labels (e.g. "9:00 AM,2:00 PM"); blank -> ESTIMATE_TIMES
    #   working_days    CSV of weekday ints, Mon=0..Sun=6; blank -> DEFAULT_WORKING_DAYS
    #   buffer_minutes  minimum gap between estimates; 0 -> no buffer (old behavior)
    biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
    for col, ddl in (("estimate_times", "TEXT"), ("working_days", "TEXT"),
                     ("buffer_minutes", "INTEGER DEFAULT 0")):
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
    # One ACTIVE growth touch per (lead, kind) -- the same race-safe dedupe for the
    # Phase 3 growth engine. Partial on status!='canceled' so a touch can be re-queued
    # after it's canceled (e.g. a reschedule), but never double-queued while in flight.
    if "uniq_growth_touch_per_lead" not in sched_idx:
        # Collapse any pre-existing active duplicates (keep the earliest per lead+kind) so the
        # UNIQUE index can't fail to build on an already-populated DB.
        c.execute(
            "DELETE FROM scheduled_messages WHERE kind NOT IN ('reminder','followup') "
            "AND status!='canceled' AND id NOT IN ("
            "  SELECT MIN(id) FROM scheduled_messages "
            "  WHERE kind NOT IN ('reminder','followup') AND status!='canceled' "
            "  GROUP BY lead_id, kind)")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_growth_touch_per_lead "
            "ON scheduled_messages(lead_id, kind) "
            "WHERE kind NOT IN ('reminder','followup') AND status!='canceled'")
    # Speed the growth-engine per-lead aggregates (growth_candidates correlated subqueries).
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_lead "
              "ON messages(lead_id, direction, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_appts_lead "
              "ON appointments(lead_id, status, day)")

    # Seed "client zero" (business 1) if no business exists yet.
    if not c.execute("SELECT 1 FROM businesses WHERE id=1").fetchone():
        b = DEFAULT_BUSINESS
        cols = ",".join(_BUSINESS_COLS)
        marks = ",".join("?" for _ in _BUSINESS_COLS)
        c.execute(
            f"INSERT INTO businesses (id,{cols}) VALUES (1,{marks})",
            tuple(b.get(col) for col in _BUSINESS_COLS),  # new cols default to NULL -> config fallback
        )
    conn.commit()
    conn.close()


# ---- Platform meta (key-value) ----
def set_meta(key, value):
    """Upsert a platform-level key-value pair (e.g. 'last_tick_utc')."""
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()


def get_meta(key):
    """Return the stored string value for `key`, or None if absent."""
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


# ---- Users / auth ----  (create_user / get_user / get_user_by_email are in the
# trades_core db_core kernel; imported + re-exported near the top of this file.)
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


def set_chaperone_dismissed(business_id, when):
    """Pause (or clear) the first-run setup chaperone. `when` is an ISO timestamp string to
    pause it, or None to clear (so it can lead again). Kept out of _BUSINESS_COLS so a profile
    save never flips it by omission."""
    conn = get_conn()
    conn.execute("UPDATE businesses SET chaperone_dismissed_at=? WHERE id=?",
                 (when, business_id))
    conn.commit()
    conn.close()


def set_growth_on(business_id, on):
    """Opt a business in/out of auto-queued growth touches (Phase 3). Kept out of the
    Settings-form path (_BUSINESS_COLS) so a profile save never flips it by omission."""
    conn = get_conn()
    conn.execute("UPDATE businesses SET growth_on=? WHERE id=?",
                 (1 if on else 0, business_id))
    conn.commit()
    conn.close()


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


def set_screen_mode(business_id, mode):
    """Set (or clear) this business's call-screening mode. A valid mode
    (off|monitor|enforce) is stored; anything else clears it to NULL so the business
    inherits the app-wide config.SCREEN_MODE default."""
    m = mode if mode in ("off", "monitor", "enforce") else None
    conn = get_conn()
    conn.execute("UPDATE businesses SET screen_mode=? WHERE id=?", (m, business_id))
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


# ---- Go-Live wizard (connections.py) setters ----
# Kept separate from update_business/_BUSINESS_COLS (which is also the seed INSERT
# list) so these wizard-only columns are never blanked by a profile save.
_A2P_PROFILE_COLS = ("legal_business_name", "ein", "business_address", "website")


def update_a2p_profile(business_id, fields):
    """Persist the A2P 10DLC registration intake (legal name, EIN, address, website).
    Only writes the columns actually present in `fields`."""
    cols = [c for c in _A2P_PROFILE_COLS if c in fields]
    if not cols:
        return
    conn = get_conn()
    sets = ", ".join(f"{c}=?" for c in cols)
    conn.execute(f"UPDATE businesses SET {sets} WHERE id=?",
                 tuple(fields[c] for c in cols) + (business_id,))
    conn.commit()
    conn.close()


def set_a2p_status(business_id, status):
    """Update just the A2P status (unregistered|pending|approved|failed). Used by the
    Twilio status sync."""
    conn = get_conn()
    conn.execute("UPDATE businesses SET a2p_status=? WHERE id=?", (status, business_id))
    conn.commit()
    conn.close()


def set_a2p_registration(business_id, brand_sid=None, campaign_sid=None,
                         messaging_service_sid=None, status=None, submitted_at=None):
    """Record the A2P registration on a business (concierge submit + SID capture).
    Only the provided fields are written, so recording SIDs later never clobbers an
    earlier status."""
    sets, vals = [], []
    for col, val in (("a2p_brand_sid", brand_sid), ("a2p_campaign_sid", campaign_sid),
                     ("a2p_messaging_service_sid", messaging_service_sid),
                     ("a2p_status", status), ("a2p_submitted_at", submitted_at)):
        if val is not None:
            sets.append(f"{col}=?")
            vals.append(val)
    if not sets:
        return
    conn = get_conn()
    conn.execute(f"UPDATE businesses SET {', '.join(sets)} WHERE id=?",
                 tuple(vals) + (business_id,))
    conn.commit()
    conn.close()


def set_forwarding_confirmed(business_id, confirmed):
    """Record whether the owner has confirmed they set carrier call-forwarding to the
    FirstBack number (the one go-live step that happens on their physical phone and so
    can't be verified server-side)."""
    conn = get_conn()
    conn.execute("UPDATE businesses SET forwarding_confirmed=? WHERE id=?",
                 (1 if confirmed else 0, business_id))
    conn.commit()
    conn.close()


def get_business_by_twilio_number(number):
    """The tenant that owns a given FirstBack/Twilio number (a webhook's `To`).
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


def set_business_twilio(business_id, twilio_number, twilio_number_sid="",
                        forward_to=None, webhooks_wired=None):
    """Store a business's provisioned Twilio number (and optional forward-to cell).
    Used by messaging.provision_number; not exposed on the Settings form.

    `webhooks_wired` records whether the number's Twilio Voice + SMS webhooks were
    actually configured -- only written when given, so a caller that just rebinds a
    number never clears the flag. A number whose webhooks aren't wired must not read
    as 'live' (see compliance.launch_blockers)."""
    conn = get_conn()
    sets = ["twilio_number=?", "twilio_number_sid=?"]
    vals = [twilio_number, twilio_number_sid]
    if forward_to is not None:
        sets.append("forward_to=?"); vals.append(forward_to)
    if webhooks_wired is not None:
        sets.append("webhooks_wired=?"); vals.append(1 if webhooks_wired else 0)
    vals.append(business_id)
    conn.execute(f"UPDATE businesses SET {', '.join(sets)} WHERE id=?", tuple(vals))
    conn.commit(); conn.close()


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


def search_leads(business_id, query, limit=8):
    """Free-text lead lookup (name / phone / project / summary), tenant-scoped, newest first.
    A digit run in the query also matches the stored E.164 phone (so "5550111" finds
    "+15125550111"). Returns [] for an empty query."""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    digits = re.sub(r"\D", "", q)
    clauses = ["name LIKE ? COLLATE NOCASE", "phone LIKE ?",
               "IFNULL(project_type,'') LIKE ? COLLATE NOCASE",
               "IFNULL(summary,'') LIKE ? COLLATE NOCASE"]
    params = [business_id, like, like, like, like]
    if len(digits) >= 4:
        clauses.append("phone LIKE ?")
        params.append(f"%{digits}%")
    params.append(limit)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM leads WHERE business_id=? AND ({' OR '.join(clauses)}) "
        "ORDER BY id DESC LIMIT ?", tuple(params)).fetchall()
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


def mark_lead_urgent(lead_id, business_id=None):
    # business_id is optional for back-compat, but production callers pass it: defense-in-depth
    # tenant scoping so a stray lead_id can never flip another tenant's lead.
    conn = get_conn()
    if business_id is None:
        conn.execute("UPDATE leads SET urgent=1 WHERE id=?", (lead_id,))
    else:
        conn.execute("UPDATE leads SET urgent=1 WHERE id=? AND business_id=?",
                     (lead_id, business_id))
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


def _hm_to_min(hhmm):
    """'HH:MM' -> minutes past midnight, or None."""
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


def scheduling_prefs(business_id):
    """The owner's effective scheduling preferences, with config fallbacks so a tenant
    who never customized behaves exactly as before:
      times          [labels]            the estimate windows offered each open day
      working_days   set of weekday ints (Mon=0..Sun=6) the shop takes estimates
      buffer_minutes int                 minimum gap between two booked estimates"""
    biz = get_business(business_id) or {}
    times = [t.strip() for t in (biz.get("estimate_times") or "").split(",") if t.strip()]
    if not times:
        times = list(ESTIMATE_TIMES)
    wd_raw = (biz.get("working_days") or "").strip()
    if wd_raw:
        working = {int(x) for x in wd_raw.split(",")
                   if x.strip().isdigit() and 0 <= int(x) <= 6}
    else:
        working = set(DEFAULT_WORKING_DAYS)
    if not working:                       # never strand a tenant with zero open days
        working = set(DEFAULT_WORKING_DAYS)
    try:
        buf = int(biz.get("buffer_minutes") or 0)
    except (TypeError, ValueError):
        buf = DEFAULT_BUFFER_MINUTES
    return {"times": times, "working_days": working, "buffer_minutes": max(0, buf)}


def set_scheduling_prefs(business_id, times=None, working_days=None, buffer_minutes=None):
    """Update any provided scheduling preference. Blank times/days fall back to defaults
    (we never store a tenant into zero availability)."""
    fields = {}
    if times is not None:
        clean = [t.strip() for t in times if t and time_key(t)]
        fields["estimate_times"] = ",".join(clean)
    if working_days is not None:
        days = sorted({int(d) for d in working_days
                       if str(d).strip().isdigit() and 0 <= int(d) <= 6})
        fields["working_days"] = ",".join(str(d) for d in days)
    if buffer_minutes is not None:
        try:
            fields["buffer_minutes"] = max(0, int(buffer_minutes))
        except (TypeError, ValueError):
            pass
    if fields:
        update_business(business_id, fields)


def upcoming_slots(business_id, limit=8, exclude_ids=None):
    """The soonest OPEN estimate windows for a business, honoring the owner's scheduling
    preferences (db.scheduling_prefs): their estimate times, their working days, and a
    minimum buffer between bookings. Skips busy days, taken times, and -- when a buffer
    is set -- any window within `buffer_minutes` of an existing booking that day, so the
    AI never books two estimates too close to make both.

    `exclude_ids` is an optional set of slot ids ('YYYY-MM-DD@HH:MM') to also skip
    (used to fold in a connected Google calendar's conflicts at slot granularity).
    Each slot carries a canonical `time_key` and that stable `id`."""
    prefs = scheduling_prefs(business_id)
    times, working, buf = prefs["times"], prefs["working_days"], prefs["buffer_minutes"]
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
        if cur.weekday() not in working:          # the shop is closed this day
            continue
        iso = cur.isoformat()
        if iso in busy:
            continue
        taken_times = taken.get(iso, set())
        taken_mins = [m for m in (_hm_to_min(k) for k in taken_times) if m is not None]
        for t in times:
            tk = time_key(t)
            if not tk:
                continue
            sid = f"{iso}@{tk}"
            if tk in taken_times or sid in exclude_ids:
                continue
            cand = _hm_to_min(tk)
            if buf and cand is not None and any(abs(cand - bm) < buf for bm in taken_mins):
                continue                          # too close to an existing booking
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
    if not row:
        return None
    intg = dict(row)
    # Decrypt tokens at rest. Dual-read: encrypted blobs are decrypted, legacy
    # plaintext passes through unchanged, so businesses connected before encryption
    # was switched on keep working (token_crypto.decrypt handles both).
    for col in ("access_token", "refresh_token"):
        if col in intg:
            intg[col] = token_crypto.decrypt(intg[col])
    return intg


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
        # Encrypt tokens at rest (no-op when no key is configured). encrypt(None)
        # stays None so the COALESCE still keeps an existing refresh token.
        access_token = token_crypto.encrypt(access_token)
        refresh_token = token_crypto.encrypt(refresh_token)
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
    """Store (or clear) a business's Google OAuth tokens and mark it connected.

    On connect/refresh (access_token given): upsert, encrypting tokens at rest, and
    KEEP an existing refresh token when this response omitted one. On disconnect
    (access_token None): a real forget -- clear the tokens outright (not just
    connected=0), matching set_oauth_tokens so no refresh token lingers at rest."""
    conn = get_conn()
    if access_token is None:
        conn.execute(
            "UPDATE integrations SET connected=0, connected_at=NULL, access_token=NULL, "
            "refresh_token=NULL, token_expiry=NULL WHERE business_id=? AND provider='google'",
            (business_id,))
        conn.commit()
        conn.close()
        return
    # Encrypt tokens at rest (no-op when no key is configured).
    enc_access = token_crypto.encrypt(access_token)
    enc_refresh = token_crypto.encrypt(refresh_token)
    conn.execute(
        "INSERT INTO integrations (business_id, provider, connected, connected_at, "
        "access_token, refresh_token, token_expiry, calendar_id) "
        "VALUES (?, 'google', 1, ?, ?, ?, ?, ?) "
        "ON CONFLICT(business_id, provider) DO UPDATE SET "
        "connected=excluded.connected, connected_at=excluded.connected_at, "
        "access_token=excluded.access_token, "
        "refresh_token=COALESCE(excluded.refresh_token, integrations.refresh_token), "
        "token_expiry=excluded.token_expiry, calendar_id=excluded.calendar_id",
        (business_id, now_iso(), enc_access, enc_refresh, token_expiry, calendar_id),
    )
    conn.commit()
    conn.close()


# ---- Callback system: inbound call log + consent/suppression ----
def log_call(business_id, call_sid, from_number="", to_number="", dial_status="",
             answered_by="", missed=0, lead_id=None, category=None, engaged=None,
             screen_status=None, spam_score=None, screen_reasons=None, screen_mode=None):
    """Record (or update) an inbound call. Idempotent on call_sid because Twilio
    retries webhooks and fires several events per call; later events update the
    outcome fields rather than inserting duplicates. `category`/`engaged` carry the
    triage verdict, and `screen_status`/`spam_score`/`screen_reasons`/`screen_mode`
    the richer screening outcome (all COALESCEd so a later event never nulls them)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO calls (business_id, lead_id, call_sid, from_number, to_number,"
        " dial_status, answered_by, missed, category, engaged, screen_status,"
        " spam_score, screen_reasons, screen_mode, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(call_sid) DO UPDATE SET "
        "  dial_status=excluded.dial_status, answered_by=excluded.answered_by, "
        "  missed=excluded.missed, lead_id=COALESCE(excluded.lead_id, calls.lead_id), "
        "  category=COALESCE(excluded.category, calls.category), "
        "  engaged=COALESCE(excluded.engaged, calls.engaged), "
        "  screen_status=COALESCE(excluded.screen_status, calls.screen_status), "
        "  spam_score=COALESCE(excluded.spam_score, calls.spam_score), "
        "  screen_reasons=COALESCE(excluded.screen_reasons, calls.screen_reasons), "
        "  screen_mode=COALESCE(excluded.screen_mode, calls.screen_mode)",
        (business_id, lead_id, call_sid, from_number, to_number, dial_status,
         answered_by, 1 if missed else 0, category,
         None if engaged is None else (1 if engaged else 0),
         screen_status, spam_score, screen_reasons, screen_mode, now_iso()))
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


def last_inbound_call(business_id):
    """The most recent inbound call logged for a business (any outcome), or None.
    Used by the Go-Live page to confirm a real test call came through and whether we
    engaged (texted the caller back)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, from_number, engaged, missed, created_at FROM calls "
        "WHERE business_id=? ORDER BY created_at DESC LIMIT 1", (business_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def recent_screened_calls(business_id, limit=8):
    """Recent missed callers the screen flagged -- either a known non-prospect (the
    owner's directory: personal/vendor/blocked) OR a suspected spam/robocaller --
    grouped to the most recent call per number, for the dashboard 'Screened calls'
    strip + one-tap override. Each row carries the spam score + reasons (so the owner
    sees WHY) plus `engaged`/`screen_mode` (so the UI can tell an enforced screen, which
    offers a 'text them back' override, from a MONITOR candidate, which was still texted
    and is shown for review only). Opt-outs are excluded -- re-texting a STOP is never
    offered."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, from_number, category, screen_status, spam_score, screen_reasons, "
        "  engaged, screen_mode, created_at, times FROM ("
        "  SELECT id, from_number, category, screen_status, spam_score, screen_reasons, "
        "    engaged, screen_mode, created_at, COUNT(*) OVER (PARTITION BY from_number) AS times, "
        "    ROW_NUMBER() OVER (PARTITION BY from_number ORDER BY created_at DESC) AS rn "
        "  FROM calls "
        "  WHERE business_id=? AND from_number IS NOT NULL AND from_number<>'' "
        # An ENFORCED screen (engaged=0) of a directory non-prospect or spam, OR a
        # MONITOR-mode would-have-screened candidate (still texted, shown for review).
        "    AND ((engaged=0 AND (category IN ('personal','vendor','blocked') "
        "                          OR screen_status='screened_spam')) "
        "         OR (screen_mode='monitor' "
        "             AND screen_status IN ('screened_spam','screened_contact')))"
        ") WHERE rn=1 ORDER BY created_at DESC LIMIT ?",
        (business_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def screening_stats(business_id, since=None):
    """Counts for the dashboard 'phone screen' stat: how many missed callers were
    assessed as spam vs known non-prospect, how many were engaged/flagged, and -- split
    by screen_mode -- how many were actually SUPPRESSED (enforce) vs how many merely
    WOULD have been (monitor). `since` is an ISO timestamp lower bound (None = all time).
    One row per call (not deduped) so the stat reflects texts actually saved."""
    conn = get_conn()
    where = "business_id=? AND missed=1"
    args = [business_id]
    if since:
        where += " AND created_at>=?"
        args.append(since)
    # A verdict that suppresses a text-back (vs. 'trusted' which is passed to the owner).
    suppress = "screen_status IN ('screened_spam','screened_contact')"
    row = conn.execute(
        f"SELECT "
        f"  SUM(CASE WHEN screen_status='screened_spam' THEN 1 ELSE 0 END) AS spam, "
        f"  SUM(CASE WHEN screen_status='screened_contact' THEN 1 ELSE 0 END) AS contact, "
        f"  SUM(CASE WHEN screen_status='trusted' THEN 1 ELSE 0 END) AS trusted, "
        f"  SUM(CASE WHEN screen_status='review' THEN 1 ELSE 0 END) AS review, "
        f"  SUM(CASE WHEN engaged=1 THEN 1 ELSE 0 END) AS engaged, "
        f"  SUM(CASE WHEN {suppress} AND screen_mode='enforce' THEN 1 ELSE 0 END) AS enforced, "
        f"  SUM(CASE WHEN {suppress} AND screen_mode='monitor' THEN 1 ELSE 0 END) AS would_screen, "
        f"  COUNT(*) AS total "
        f"FROM calls WHERE {where}", args).fetchone()
    conn.close()
    d = {k: (row[k] or 0) for k in row.keys()}
    d["screened"] = d["spam"] + d["contact"]   # assessed as spam/known (across modes)
    return d


def is_known_caller(business_id, number):
    """True if this business already KNOWS the caller, so the bot should NOT cold-pitch
    them (the owner handles them personally) -- the auto-derived 'allowlist', the way
    Apple builds one from Contacts + recent calls with ZERO setup. Known means either a
    directory entry (manual, synced, or auto-learned on a booking) OR a booked estimate
    on a lead with this number. No manual import required."""
    key = _digits10(number)
    if not key:
        return False
    conn = get_conn()
    hit = conn.execute(
        "SELECT 1 FROM contacts WHERE business_id=? AND number=? "
        "UNION ALL "
        "SELECT 1 FROM appointments a JOIN leads l ON l.id=a.lead_id "
        "  WHERE a.business_id=? AND a.status='booked' "
        "  AND substr(replace(replace(replace(replace(l.phone,' ',''),'-',''),'(',''),')',''), -10)=? "
        "LIMIT 1",
        (business_id, key, business_id, key)).fetchone()
    conn.close()
    return hit is not None


# ---- Number reputation cache (Tier 2 paid lookup) + crowdsource ledger ----
def get_reputation(number, max_age_hours=None):
    """A cached reputation row {line_type, spam_score, source, checked_at} for a
    number, or None if absent or staler than max_age_hours. Global (not per-business):
    a robocaller's reputation is the same for everyone, so one lookup serves all."""
    key = _digits10(number)
    if not key:
        return None
    conn = get_conn()
    row = conn.execute("SELECT * FROM number_reputation WHERE number=?", (key,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if max_age_hours is not None and d.get("checked_at"):
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(d["checked_at"])).total_seconds() / 3600.0
            if age > max_age_hours:
                return None
        except (ValueError, TypeError):
            return None
    return d


def set_reputation(number, line_type=None, spam_score=None, source=""):
    """Upsert a number's reputation into the global cache."""
    key = _digits10(number)
    if not key:
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO number_reputation (number, line_type, spam_score, source, checked_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(number) DO UPDATE SET "
        "  line_type=excluded.line_type, spam_score=excluded.spam_score, "
        "  source=excluded.source, checked_at=excluded.checked_at",
        (key, line_type, spam_score, source, now_iso()))
    conn.commit()
    conn.close()


def add_spam_flag(business_id, number):
    """Record that this business flagged a number as spam (the owner's 'Mark spam'
    tap). Idempotent per business+number. Feeds the cross-tenant crowdsource signal."""
    key = _digits10(number)
    if not key:
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO spam_flags (business_id, number, created_at) VALUES (?,?,?) "
        "ON CONFLICT(business_id, number) DO NOTHING",
        (business_id, key, now_iso()))
    conn.commit()
    conn.close()


def global_spam_count(number, exclude_business_id=None):
    """How many DISTINCT businesses have flagged this number as spam -- the privacy-safe
    crowdsource signal (a COUNT only; never who). Optionally exclude the asking business
    so a tenant's own flag doesn't double-count as 'the community'."""
    key = _digits10(number)
    if not key:
        return 0
    conn = get_conn()
    if exclude_business_id is not None:
        n = conn.execute(
            "SELECT COUNT(DISTINCT business_id) FROM spam_flags WHERE number=? AND business_id<>?",
            (key, exclude_business_id)).fetchone()[0]
    else:
        n = conn.execute(
            "SELECT COUNT(DISTINCT business_id) FROM spam_flags WHERE number=?", (key,)).fetchone()[0]
    conn.close()
    return n or 0


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
# FirstBack observes a caller and PROPOSES a category; the owner confirms with one
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


def set_suggestion_status(sug_id, status, business_id=None):
    # business_id optional for back-compat; production callers pass it for defense-in-depth
    # tenant scoping (the routes already 404-guard via get_suggestion, this is belt + braces).
    conn = get_conn()
    if business_id is None:
        conn.execute("UPDATE contact_suggestions SET status=?, updated_at=? WHERE id=?",
                     (status, now_iso(), sug_id))
    else:
        conn.execute("UPDATE contact_suggestions SET status=?, updated_at=? "
                     "WHERE id=? AND business_id=?", (status, now_iso(), sug_id, business_id))
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


# ---- Phase 3 growth engine: signal reads (one pass feeds every play) ----
def growth_candidates(business_id):
    """One row per lead with the aggregates every growth play needs: contact + status +
    last inbound/outbound message times + booked-appointment count + most recent booked
    appointment day. Powers growth.plays() in a single pass (no per-lead N+1)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT l.id, l.name, l.phone, l.status, l.created_at, l.address, "
        "(SELECT MAX(m.created_at) FROM messages m "
        "   WHERE m.lead_id=l.id AND m.direction='out') AS last_out_at, "
        "(SELECT MAX(m.created_at) FROM messages m "
        "   WHERE m.lead_id=l.id AND m.direction='in') AS last_in_at, "
        "(SELECT COUNT(*) FROM appointments a "
        "   WHERE a.lead_id=l.id AND a.status='booked') AS booked_count, "
        "(SELECT MAX(a.day) FROM appointments a "
        "   WHERE a.lead_id=l.id AND a.status='booked') AS last_appt_day "
        "FROM leads l WHERE l.business_id=?",
        (business_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def growth_touch_index(business_id):
    """Map lead_id -> set of growth `kind`s already in flight (status != canceled), so
    plays()/scan() never re-offer or re-queue a touch already pending or sent, in one query."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT lead_id, kind FROM scheduled_messages "
        "WHERE business_id=? AND status!='canceled' "
        "AND kind NOT IN ('reminder','followup')", (business_id,)).fetchall()
    conn.close()
    idx = {}
    for r in rows:
        idx.setdefault(r["lead_id"], set()).add(r["kind"])
    return idx


def cancel_lead_growth_touches(lead_id, kinds):
    """Cancel a lead's PENDING growth touches of the given kinds -- e.g. pause the whole
    quote-followup sequence the moment the lead books. Mirrors cancel_lead_pending_reminders."""
    kinds = list(kinds or [])
    if not kinds:
        return
    conn = get_conn()
    qs = ",".join("?" for _ in kinds)
    conn.execute(f"UPDATE scheduled_messages SET status='canceled' "
                 f"WHERE lead_id=? AND kind IN ({qs}) AND status='pending'",
                 (lead_id, *kinds))
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


# ---- Command-center conversation memory (record / flag / learn) ----
def start_or_get_convo(business_id, session_key, browser_key=None):
    """Reuse this owner's current conversation or open a new one. A `browser_key` (a stable
    id the client keeps in localStorage) is preferred when present: it survives page reloads
    -- so "text her back" still resolves after a refresh -- with a 24h window. The ephemeral
    `session_key` (per page load) remains the fallback for older clients, with a 2h window."""
    conn = get_conn()
    cid = None
    if browser_key:
        row = conn.execute(
            "SELECT id, last_at FROM assistant_convos WHERE business_id=? AND browser_key=? "
            "ORDER BY id DESC LIMIT 1", (business_id, browser_key)).fetchone()
        cid = _reuse_convo(row, 86400)        # 24h: reload-durable
    if cid is None:
        row = conn.execute(
            "SELECT id, last_at FROM assistant_convos WHERE business_id=? AND session_key=? "
            "ORDER BY id DESC LIMIT 1", (business_id, session_key or "")).fetchone()
        cid = _reuse_convo(row, 7200)         # 2h: per-page-load fallback
    now = now_iso()
    if cid is None:
        cid = conn.execute(
            "INSERT INTO assistant_convos (business_id, session_key, browser_key, started_at, "
            "last_at) VALUES (?,?,?,?,?)",
            (business_id, session_key or "", browser_key or "", now, now)).lastrowid
    else:
        conn.execute("UPDATE assistant_convos SET last_at=? WHERE id=?", (now, cid))
    conn.commit()
    conn.close()
    return cid


def _reuse_convo(row, window_secs):
    """The convo id to reuse from a lookup row if it's within `window_secs`, else None."""
    if not row:
        return None
    try:
        gap = (datetime.now(timezone.utc) - datetime.fromisoformat(row["last_at"])).total_seconds()
        return row["id"] if gap < window_secs else None
    except (ValueError, TypeError):
        return row["id"]


def record_turn_entities(business_id, convo_id, turn_id, entities):
    """Remember the records a turn showed (leads/appointments), so a later turn can resolve
    a referent ("her", "the second one") to a concrete id. Safe no-op when empty."""
    if not entities:
        return
    conn = get_conn()
    now = now_iso()
    conn.executemany(
        "INSERT INTO assistant_turn_entities (business_id, convo_id, turn_id, kind, entity_id, "
        "name, phone, ordinal, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(business_id, convo_id, turn_id, e.get("kind"), e.get("id"), e.get("name") or "",
          e.get("phone") or "", e.get("ordinal"), now) for e in entities])
    conn.commit()
    conn.close()


def incr_rate(business_id, bucket, window_secs=60):
    """Increment and return this tenant's action count in the current time window -- a cheap
    per-tenant sliding-window rate counter. Stale windows for this bucket are pruned on every
    call, so the table never accumulates more than one row per (tenant, bucket)."""
    import time
    key = f"{bucket}:{int(time.time() // window_secs)}"
    conn = get_conn()
    conn.execute("INSERT INTO rate_limits (business_id, k, n) VALUES (?,?,1) "
                 "ON CONFLICT(business_id, k) DO UPDATE SET n = n + 1", (business_id, key))
    # Drop every other window for this bucket -- only the current one is ever consulted.
    conn.execute("DELETE FROM rate_limits WHERE business_id=? AND k LIKE ? AND k<>?",
                 (business_id, f"{bucket}:%", key))
    n = conn.execute("SELECT n FROM rate_limits WHERE business_id=? AND k=?",
                     (business_id, key)).fetchone()["n"]
    conn.commit()
    conn.close()
    return n


def add_audit(business_id, action, detail=""):
    """Record a gated/confirmed action for this tenant -- the abuse + reconciliation trail."""
    conn = get_conn()
    conn.execute("INSERT INTO audit_log (business_id, action, detail, created_at) "
                 "VALUES (?,?,?,?)", (business_id, action, (detail or "")[:500], now_iso()))
    conn.commit()
    conn.close()


def list_audit(business_id, limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT action, detail, created_at FROM audit_log WHERE business_id=? "
        "ORDER BY id DESC LIMIT ?", (business_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recent_entities(business_id, convo_id):
    """The entities most recently shown in this conversation (the latest turn that showed
    any), ordered by the position they appeared in -- the context for resolving "her" /
    "the second one". [] when nothing has been shown yet."""
    if not convo_id:
        return []
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(turn_id) AS t FROM assistant_turn_entities WHERE business_id=? AND convo_id=?",
        (business_id, convo_id)).fetchone()
    if not row or row["t"] is None:
        conn.close()
        return []
    rows = conn.execute(
        "SELECT kind, entity_id, name, phone, ordinal FROM assistant_turn_entities "
        "WHERE business_id=? AND turn_id=? ORDER BY ordinal", (business_id, row["t"])).fetchall()
    conn.close()
    return [{"kind": r["kind"], "id": r["entity_id"], "name": r["name"],
             "phone": r["phone"], "ordinal": r["ordinal"]} for r in rows]


def recent_user_turns(convo_id, business_id, limit=6):
    conn = get_conn()
    rows = conn.execute(
        "SELECT content FROM assistant_turns WHERE convo_id=? AND business_id=? AND role='user' "
        "ORDER BY id DESC LIMIT ?", (convo_id, business_id, limit)).fetchall()
    conn.close()
    return [r["content"] for r in rows]


def add_flag(business_id, convo_id, turn_id, kind, detail=""):
    conn = get_conn()
    fid = conn.execute(
        "INSERT INTO assistant_flags (business_id, convo_id, turn_id, kind, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (business_id, convo_id, turn_id, kind, detail, now_iso())).lastrowid
    conn.commit()
    conn.close()
    return fid


def list_convos(business_id, limit=30):
    conn = get_conn()
    rows = conn.execute(
        "SELECT c.id, c.started_at, c.last_at, "
        " (SELECT COUNT(*) FROM assistant_turns t WHERE t.convo_id=c.id) AS turns, "
        " (SELECT COUNT(*) FROM assistant_flags f WHERE f.convo_id=c.id AND f.resolved=0) AS flags "
        "FROM assistant_convos c WHERE c.business_id=? ORDER BY c.id DESC LIMIT ?",
        (business_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_flags(business_id, resolved=0, limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT f.*, t.content AS turn_content FROM assistant_flags f "
        "LEFT JOIN assistant_turns t ON t.id=f.turn_id "
        "WHERE f.business_id=? AND f.resolved=? ORDER BY f.id DESC LIMIT ?",
        (business_id, resolved, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_flag(business_id, flag_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM assistant_flags WHERE id=? AND business_id=?",
                       (flag_id, business_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def resolve_flag(business_id, flag_id):
    conn = get_conn()
    conn.execute("UPDATE assistant_flags SET resolved=1 WHERE id=? AND business_id=?",
                 (flag_id, business_id))
    conn.commit()
    conn.close()


def add_learning(business_id, pattern, action, answer="", source_turn_id=None, confirmed=1):
    conn = get_conn()
    lid = conn.execute(
        "INSERT INTO assistant_learnings (business_id, pattern, action, answer, source_turn_id, "
        "confirmed, created_at) VALUES (?,?,?,?,?,?,?)",
        (business_id, (pattern or "").strip().lower(), action, answer, source_turn_id,
         1 if confirmed else 0, now_iso())).lastrowid
    conn.commit()
    conn.close()
    return lid


def list_learnings(business_id, confirmed_only=True):
    conn = get_conn()
    q = "SELECT * FROM assistant_learnings WHERE business_id=?"
    if confirmed_only:
        q += " AND confirmed=1"
    rows = conn.execute(q + " ORDER BY id DESC", (business_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def bump_learning(business_id, learning_id):
    conn = get_conn()
    conn.execute("UPDATE assistant_learnings SET uses=uses+1 WHERE id=? AND business_id=?",
                 (learning_id, business_id))
    conn.commit()
    conn.close()


def memory_digest(business_id, since_iso):
    """Counts since `since_iso` for the command-center digest: flags by kind, learnings
    added, and conversations touched."""
    conn = get_conn()
    flags = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM assistant_flags WHERE business_id=? AND created_at>=? "
        "GROUP BY kind", (business_id, since_iso)).fetchall()
    learns = conn.execute(
        "SELECT COUNT(*) FROM assistant_learnings WHERE business_id=? AND created_at>=?",
        (business_id, since_iso)).fetchone()[0]
    convs = conn.execute(
        "SELECT COUNT(*) FROM assistant_convos WHERE business_id=? "
        "AND COALESCE(last_at, started_at)>=?", (business_id, since_iso)).fetchone()[0]
    conn.close()
    d = {r["kind"]: r["n"] for r in flags}
    return {"gaps": d.get("capability_gap", 0) + d.get("unhelpful", 0),
            "repeats": d.get("repeat", 0), "negatives": d.get("negative", 0),
            "learnings": learns, "convos": convs}


def unmet_flag_contents(business_id):
    """The owner messages behind every still-open capability-gap / unhelpful flag, so we
    can rank which missing capability recurs the most (what to build next)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT t.content AS c FROM assistant_flags f JOIN assistant_turns t ON t.id=f.turn_id "
        "WHERE f.business_id=? AND f.resolved=0 AND f.kind IN ('capability_gap','unhelpful') "
        "AND t.content IS NOT NULL AND t.content != ''", (business_id,)).fetchall()
    conn.close()
    return [r["c"] for r in rows]


def coach_candidates(business_id):
    """(message, route) for every open capability-gap whose flag recorded the page the
    assistant pointed to -- the raw material for a proactive 'remember this' offer."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT t.content AS c, f.detail AS d FROM assistant_flags f "
        "JOIN assistant_turns t ON t.id=f.turn_id "
        "WHERE f.business_id=? AND f.resolved=0 AND f.kind='capability_gap' "
        "AND f.detail LIKE 'route:%' AND t.content IS NOT NULL AND t.content != ''",
        (business_id,)).fetchall()
    conn.close()
    return [(r["c"], r["d"][6:]) for r in rows]


def convo_user_turn_count(business_id, convo_id):
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM assistant_turns WHERE business_id=? AND convo_id=? "
                     "AND role='user'", (business_id, convo_id)).fetchone()[0]
    conn.close()
    return n


def has_coach_offer(business_id, convo_id):
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM assistant_flags WHERE business_id=? AND convo_id=? "
                     "AND kind='coach_offered'", (business_id, convo_id)).fetchone()[0]
    conn.close()
    return n > 0


def mark_coach_offered(business_id, convo_id):
    conn = get_conn()
    conn.execute(
        "INSERT INTO assistant_flags (business_id, convo_id, turn_id, kind, detail, "
        "created_at, resolved) VALUES (?,?,?,?,?,?,1)",
        (business_id, convo_id, None, "coach_offered", "", now_iso()))
    conn.commit()
    conn.close()


def all_owner_recipients():
    """(business_id, owner_email) for every tenant -- the weekly-digest cron's mailing list.
    FirstBack stores the owner's contact email in alert_email; falls back to the login email."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT u.business_id AS bid, COALESCE(NULLIF(b.alert_email,''), u.email) AS email "
        "FROM users u JOIN businesses b ON b.id = u.business_id").fetchall()
    conn.close()
    return [(r["bid"], r["email"]) for r in rows if r["email"]]
