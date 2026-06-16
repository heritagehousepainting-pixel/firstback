"""Shared data-core for the trades_core kernel (vendored into each product's db.py).

The byte-identical DB helpers both products had: timestamp, the users/auth CRUD, and the
assistant-subsystem reads/writes (all on the 5 shared tables — users + assistant_*). The
product-specific tables (businesses/contacts/leads/messages — "false friends" per the
audit) and each app's own `get_conn`/migrations stay in the app's db.py.

CONNECTION INJECTION: each app's db.py sets `db_core.get_conn = get_conn` after defining
its own connection factory, so these helpers use the product's own DB path/pragmas without
db_core importing the app (no circular import). Then db.py re-exports the names so every
existing `db.get_user(...)` call site is unchanged.

Edit trades_core/db_core.py, then run `python3 trades_core/sync.py`.
"""
import sqlite3
from datetime import datetime, timezone

# Injected by the host app's db.py (db_core.get_conn = get_conn). Calling these before
# injection is a programming error (None is not callable) — db.py wires it at import.
get_conn = None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---- Auth / users (shared `users` table) ----
def get_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_email(email):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email=?",
                       (email.strip().lower(),)).fetchone()
    conn.close()
    return dict(row) if row else None


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


# ---- Assistant subsystem (shared assistant_* tables) ----
def log_turn(convo_id, business_id, role, content, tool=None, status=None):
    conn = get_conn()
    tid = conn.execute(
        "INSERT INTO assistant_turns (convo_id, business_id, role, content, tool, status, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (convo_id, business_id, role, content, tool, status, now_iso())).lastrowid
    conn.commit()
    conn.close()
    return tid


def get_convo_turns(convo_id, business_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM assistant_turns WHERE convo_id=? AND business_id=? ORDER BY id",
        (convo_id, business_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def flag_counts(business_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM assistant_flags WHERE business_id=? AND resolved=0 "
        "GROUP BY kind", (business_id,)).fetchall()
    conn.close()
    return {r["kind"]: r["n"] for r in rows}
