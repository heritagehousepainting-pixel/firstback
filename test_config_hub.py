"""Phase 6 Pillar A (talk-to-configure) -- money settings. Run: python3 test_config_hub.py

Vic configures the two settings that unlock its core value props, by talking:
  * set_avg_job_value -- "my average job is about $2,400" -> the dollar framing lights up on
    every lead + the briefing. Gated (the owner confirms), parses $ / commas / "k".
  * set_review_link -- the Google review URL -> the review-request plays stop showing a
    placeholder. Gated, validates it's a real URL (never saves "potato").

Both are pure-data writes mirroring the existing db setters; behind the same confirm gate as
everything else. Throwaway temp DB + the deterministic demo brain; no network.
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import assistant
import app  # noqa: F401  -- seeds business #1

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


biz = db.get_business(1)

# ---------------------------------------------------------------------------
# Average job value
# ---------------------------------------------------------------------------
out = assistant.run(biz, "my average job is about $2,400")
pa = out.get("pending_action")
check("avg: an average-job amount routes to a gated set_avg_job_value",
      pa is not None and pa["tool"] == "set_avg_job_value")
check("avg: the confirm names the dollar amount", pa and "2,400" in pa["summary"])
check("avg: parses $ + commas to a number", pa and float(pa["args"].get("value")) == 2400)

# "k" shorthand a foreman would actually say
out = assistant.run(biz, "my jobs run about 3k")
pa = out.get("pending_action")
check("avg: parses the 'k' shorthand (3k -> 3000)",
      pa and pa["tool"] == "set_avg_job_value" and float(pa["args"].get("value")) == 3000)

# The gate does not write on run().
_before = db.get_business(1).get("avg_job_value")
assistant.run(biz, "my average job is about $9,999")
check("avg: the gate does NOT write on run()",
      db.get_business(1).get("avg_job_value") == _before)

res = assistant.execute(biz, "set_avg_job_value", {"value": "2400"})
check("avg: execute writes avg_job_value", float(db.get_business(1).get("avg_job_value")) == 2400)
check("avg: execute confirms in the reply", bool(res["reply"]))

# ---------------------------------------------------------------------------
# Google review link
# ---------------------------------------------------------------------------
out = assistant.run(biz, "my google review link is https://g.page/r/abc123/review")
pa = out.get("pending_action")
check("review: a review URL routes to a gated set_review_link",
      pa is not None and pa["tool"] == "set_review_link")
check("review: the confirm shows the URL verbatim",
      pa and "https://g.page/r/abc123/review" in pa["summary"])

# Never save garbage: a non-URL asks instead of gating a bad write.
out = assistant.run(biz, "set my review link to potato")
check("review: a non-URL does not gate a garbage save",
      out.get("pending_action") is None)
check("review: instead it asks for a real link",
      "link" in out["reply"].lower())

res = assistant.execute(biz, "set_review_link",
                        {"url": "https://g.page/r/abc123/review"})
check("review: execute writes review_link",
      db.get_business(1).get("review_link") == "https://g.page/r/abc123/review")

# A crafted confirm payload can't smuggle a non-param column.
_status_before = db.get_business(1).get("a2p_status")
assistant.execute(biz, "set_avg_job_value", {"value": "100", "a2p_status": "approved"})
check("config: execute arg-cleans -- a smuggled a2p_status is ignored",
      db.get_business(1).get("a2p_status") == _status_before)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
