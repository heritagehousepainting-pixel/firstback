"""Phase 1 B — Cost spine tests.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_cost_spine.py
Prints ok/FAIL per check; exits 0 only when all pass.
"""
import os
import sys
import tempfile

# ---- Isolation: temp DB, demo provider, no real keys ----
os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("FIRSTBACK_DAILY_COST_CAP", "1.00")

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

import ai
import llm

_pass = _fail = 0


def ok(label):
    global _pass
    _pass += 1
    print(f"  ok  {label}")


def fail(label, detail=""):
    global _fail
    _fail += 1
    print(f"  FAIL  {label}" + (f": {detail}" if detail else ""))


# ---- 1. CLAUDE_MODEL defaults to Sonnet, not Opus ----
sonnet_model = config.CLAUDE_MODEL
if "sonnet" in sonnet_model.lower():
    ok("CLAUDE_MODEL is Sonnet")
else:
    fail("CLAUDE_MODEL should be Sonnet", sonnet_model)

# ---- 2. CLAUDE_MODEL_VOICE defaults to Haiku ----
haiku_model = config.CLAUDE_MODEL_VOICE
if "haiku" in haiku_model.lower():
    ok("CLAUDE_MODEL_VOICE is Haiku")
else:
    fail("CLAUDE_MODEL_VOICE should be Haiku", haiku_model)

# ---- 3. Env override works ----
os.environ["CLAUDE_MODEL"] = "claude-test-override"
import importlib
importlib.reload(config)
if config.CLAUDE_MODEL == "claude-test-override":
    ok("CLAUDE_MODEL env override works")
else:
    fail("CLAUDE_MODEL env override did not apply", config.CLAUDE_MODEL)
# Restore
del os.environ["CLAUDE_MODEL"]
importlib.reload(config)
config.DB_PATH = _TMP.name

# ---- 4. log_llm_usage writes a row ----
db.log_llm_usage(1, "sms", "claude-sonnet-4-6", 100, 50, 0.001_25, lead_id=99)
conn = db.get_conn()
row = conn.execute(
    "SELECT * FROM llm_usage WHERE business_id=1 AND path='sms' AND lead_id=99"
).fetchone()
conn.close()
if row and row["input_tokens"] == 100 and row["output_tokens"] == 50:
    ok("log_llm_usage writes input/output tokens")
else:
    fail("log_llm_usage row missing or wrong tokens", dict(row) if row else None)

if row and abs(row["cost_usd"] - 0.001_25) < 1e-9:
    ok("log_llm_usage writes cost_usd")
else:
    fail("log_llm_usage cost_usd wrong", row["cost_usd"] if row else None)

if row and row["model"] == "claude-sonnet-4-6":
    ok("log_llm_usage writes model")
else:
    fail("log_llm_usage model wrong", row["model"] if row else None)

# ---- 5. get_llm_spend_today accumulates correctly ----
db.log_llm_usage(1, "sms", "claude-sonnet-4-6", 200, 100, 0.005, lead_id=100)
spend = db.get_llm_spend_today(1)
# Two rows: 0.00125 + 0.005 = 0.00625
if abs(spend - 0.00625) < 1e-6:
    ok("get_llm_spend_today sums rows")
else:
    fail("get_llm_spend_today wrong total", spend)

# Different business is isolated
spend2 = db.get_llm_spend_today(2)
if spend2 == 0.0:
    ok("get_llm_spend_today is tenant-scoped")
else:
    fail("get_llm_spend_today leaked across tenants", spend2)

# ---- 6. Dollar cap blocks past budget ----
# Set cap to 0.001 (below current spend of 0.00625)
config.CLAUDE_DAILY_COST_CAP_USD = 0.001
ai.CLAUDE_DAILY_COST_CAP_USD = 0.001
# Patch ai's imported name too
import importlib
importlib.reload(ai)
import types
# Directly test is_over_daily_cap
if ai.is_over_daily_cap(1):
    ok("is_over_daily_cap returns True past cap")
else:
    fail("is_over_daily_cap should be True past cap")

# ---- 7. generate_reply returns cap message when over cap ----
biz = db.get_business(1)
history = [{"direction": "in", "body": "hello"}]
reply, booking = ai.generate_reply(biz, history, lead_id=99)
if reply == ai._CAP_SMS_REPLY:
    ok("generate_reply returns cap message when over daily cap")
else:
    fail("generate_reply should return cap message when over cap", reply[:80])

if booking is None:
    ok("generate_reply returns None booking when capped")
else:
    fail("generate_reply should return None booking when capped", booking)

# ---- 8. Cap of 0 means no cap ----
config.CLAUDE_DAILY_COST_CAP_USD = 0
ai.CLAUDE_DAILY_COST_CAP_USD = 0
importlib.reload(ai)
if not ai.is_over_daily_cap(1):
    ok("cap=0 disables the cap")
else:
    fail("cap=0 should disable the cap")

# ---- 9. _claude_cost pricing ----
# Sonnet: $3/1M input, $15/1M output
cost = llm._claude_cost("claude-sonnet-4-6", 1_000_000, 0)
if abs(cost - 3.00) < 0.01:
    ok("_claude_cost Sonnet input rate correct")
else:
    fail("_claude_cost Sonnet input rate wrong", cost)

cost2 = llm._claude_cost("claude-haiku-4-5", 0, 1_000_000)
if abs(cost2 - 4.00) < 0.01:
    ok("_claude_cost Haiku output rate correct")
else:
    fail("_claude_cost Haiku output rate wrong", cost2)

# ---- 10. cache_control: complete() passes ephemeral block (check signature) ----
import inspect
src = inspect.getsource(llm.complete)
if "cache_control" in src and "ephemeral" in src:
    ok("complete() includes cache_control ephemeral on system block")
else:
    fail("complete() missing cache_control ephemeral")

# ---- 11. return_usage param exists on complete() ----
sig = inspect.signature(llm.complete)
if "return_usage" in sig.parameters:
    ok("complete() has return_usage parameter")
else:
    fail("complete() missing return_usage parameter")

# ---- Summary ----
print(f"\n{'='*40}")
print(f"test_cost_spine: {_pass} passed, {_fail} failed")
if _fail:
    sys.exit(1)
sys.exit(0)
