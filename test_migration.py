"""Schema migration safety. Run: python3 test_migration.py

Regression for the boot crash that failed the staging deploy: on a DB where
assistant_convos already existed from an OLDER schema (no browser_key column), init_db
crashed because the browser_key index was built INSIDE the CREATE-TABLE executescript,
before the ALTER that adds the column ("sqlite3.OperationalError: no such column:
browser_key"). The index must be built AFTER the column-adding migration. No network.
"""
import os
import sqlite3
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.DB_BACKUP_PATH = ""   # no durable-mode restore in the test

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# Simulate a pre-existing OLD-schema DB: assistant_convos created before browser_key existed.
conn = sqlite3.connect(_TMP.name)
conn.execute("CREATE TABLE assistant_convos (id INTEGER PRIMARY KEY AUTOINCREMENT, "
             "business_id INTEGER NOT NULL, session_key TEXT, started_at TEXT, last_at TEXT)")
conn.commit(); conn.close()

err = None
try:
    db.init_db()
except Exception as e:
    err = e
check("init_db survives an old assistant_convos (no browser_key) without crashing", err is None)

_q = sqlite3.connect(_TMP.name)
cols = [r[1] for r in _q.execute("PRAGMA table_info(assistant_convos)").fetchall()]
check("init_db healed the table: browser_key column now exists", "browser_key" in cols)
idx = [r[0] for r in _q.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                "AND name='idx_aconvos_browser'").fetchall()]
check("init_db built the browser_key index after the column was added",
      "idx_aconvos_browser" in idx)
_q.close()

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
