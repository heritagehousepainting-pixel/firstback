"""Reliability: error handlers (Phase 2). Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_reliability.py

Covers:
  - 404 on a normal path returns HTML
  - 404 on /api/* returns JSON
  - 404 on /webhooks/* returns JSON
  - 500 returns HTML on normal paths and JSON on /api paths
  - HTML 404 renders (status 404, contains page content)
  - JSON 404 has correct structure
"""
import os
import sys
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"

import config as _config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
_config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import app as _app

# Register test routes BEFORE the first request (Flask requires this).
@_app.app.route("/test-500-trigger")
def _trigger_500():
    raise RuntimeError("test 500 trigger")

@_app.app.route("/api/test-500-trigger")
def _trigger_api_500():
    raise RuntimeError("api test 500 trigger")

client = _app.app.test_client()

_pass = _fail = 0
import json


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- 404 tests ----
r = client.get("/this-path-definitely-does-not-exist-xyz")
check("404 on normal path returns status 404", r.status_code == 404)
check("404 on normal path returns HTML content-type",
      "text/html" in r.content_type)
check("404 HTML contains '404' text", b"404" in r.data)

r_api = client.get("/api/this-does-not-exist-xyz")
check("404 on /api/ path returns status 404", r_api.status_code == 404)
check("404 on /api/ path returns JSON content-type",
      "application/json" in r_api.content_type)
data_api = json.loads(r_api.data)
check("404 /api/ JSON has 'error' key", "error" in data_api)

r_webhook = client.get("/webhooks/this-does-not-exist-xyz")
check("404 on /webhooks/ path returns status 404", r_webhook.status_code == 404)
check("404 on /webhooks/ path returns JSON",
      "application/json" in r_webhook.content_type)

# ---- 500 tests ----
r_500 = client.get("/test-500-trigger")
check("500 on normal path returns status 500", r_500.status_code == 500)
check("500 on normal path returns HTML", "text/html" in r_500.content_type)
check("500 HTML contains '500' text", b"500" in r_500.data)

r_api_500 = client.get("/api/test-500-trigger")
check("500 on /api/ path returns status 500", r_api_500.status_code == 500)
check("500 on /api/ path returns JSON", "application/json" in r_api_500.content_type)
data_500 = json.loads(r_api_500.data)
check("500 /api/ JSON has 'error' key", "error" in data_500)

# ---- Verify the 404 template actually renders (not just an error page fallback) ----
check("404 HTML body is non-empty", len(r.data) > 100)

print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
