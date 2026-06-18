# Phase 5a — SF-6 Server-Bound Confirm Token (BUILD SPEC, LOCKED)

**Date:** 2026-06-18 · Opus orchestrator · Base: `staging` @ 3dcb502 (46/46 green).
**Source of truth:** F11-FINAL §4.1–4.2, PREBUILD-SYNTHESIS gate [P0] SF-6.
**Size:** S (one new table, ~50 lines across db.py / assistant.py / app.py / assistant.js + a test).
**Owner-ops:** NONE. **Unblocks:** F11 (5b) + F13 (5d) proactive one-tap pushes.

## The gap (verified in code)
`/assistant/confirm` (app.py:864–884) executes `assistant.execute(biz, tool, args)` where
`tool` + `args` come straight from the client POST body. `_clean_args` (assistant.py:2201)
strips unknown keys but does NOT verify the action was ever proposed, nor that it matches the
preview the owner saw. A same-origin client with a valid CSRF token could POST `text_lead`
with **any recipient** and **any action**. "You approve exactly what you saw" is enforced only
in the UI, not at the server.

## The fix: proposal → stored → redeemed (DB-backed, idempotent, expiring)
1. When `_gated()` (assistant.py:1951) or `_apply_learning()` (assistant.py:2188) build a
   confirm-gated `pending_action`, call `_issue_token(business, tool, args)` → persist the
   EXACT (tool, args) → return `token_id`; attach `token_id` to `pending_action`.
2. The client POSTs ONLY `confirm_token` (+ optional edited `message`, see below) — never
   `tool`/`args`.
3. `/assistant/confirm` redeems by `token_id`: verify it belongs to `current_business()`,
   not expired, not consumed → execute the **STORED** args → mark consumed + store
   `result_json`. Second tap returns the stored result (no re-execution, no double-send).

## Shared seams / contracts (no collisions — single coherent change)
- **db.py** — new table in `init_db()` executescript (CREATE TABLE IF NOT EXISTS → existing
  DBs pick it up at next boot, no separate migration):
  ```sql
  CREATE TABLE IF NOT EXISTS pending_confirms (
      token_id TEXT PRIMARY KEY, business_id INTEGER NOT NULL, tool TEXT NOT NULL,
      args_json TEXT, preview_hash TEXT, expires_at REAL,
      consumed INTEGER DEFAULT 0, result_json TEXT, created_at TEXT );
  CREATE INDEX IF NOT EXISTS idx_pending_confirms_biz ON pending_confirms(business_id, token_id);
  ```
  + functions: `issue_confirm_token(bid, token_id, tool, args, preview_hash, ttl_seconds)`,
  `get_confirm_token(bid, token_id)`, `claim_confirm_token(bid, token_id)` (atomic
  `UPDATE … SET consumed=1 WHERE token_id=? AND business_id=? AND consumed=0` → returns
  `rowcount==1`; the race guard), `set_confirm_result(token_id, result_json)`. Add `import time`.
- **assistant.py** — add `import secrets`; new `_CONFIRM_TTL_SECONDS = 600`; new
  `_issue_token(business, tool, args)` (token_hex(16) + sha256 preview_hash + db.issue);
  attach `pending["token_id"]` at the two pending-construction sites. NO other behavior change.
- **app.py** — add `import time`; rewrite `assistant_confirm()` to redeem by token (below).
- **static/assistant.js** — `runAction(pending)` posts `{confirm_token: pending.token_id}`
  (+ `message` for text_lead edits). `tool`/`args` no longer sent.

## Redeem-path order (app.py assistant_confirm)
1. bad CSRF → 403 `bad_csrf` (unchanged).
2. missing `confirm_token` → **400** + honest reply.
3. token not found for this biz → **200** honest reply ("couldn't find that — it may have
   already run"). *(Deviation from F11's "400": a 4xx triggers the client's generic
   "something went wrong"; a 200 reply renders as a readable agent message. Dave-test win.
   Cross-tenant lookup still fails closed — `get_confirm_token` is scoped by business_id.)*
4. `consumed==1` → return stored `result_json` (idempotent replay; NO re-execute).
5. `expires_at < now` → 200 honest "expired — ask me again" (recompute, never act on stale).
6. `claim_confirm_token` atomic → if it loses the race, return the winner's stored result (or
   a benign "already running"). If it wins → load STORED args, execute, `set_confirm_result`,
   `add_audit("confirm:<tool>", "token=<8> <msg[:100]>")`, `record_exchange`, return result.

## Editable-body reconciliation (preserve the shipped feature, keep the boundary honest)
The confirm card's textarea lets the owner edit a `text_lead` body before sending
(assistant.js:462). Server-binding must NOT silently drop that edit. Resolution: the redeem
path accepts an **optional `message` override for `text_lead` ONLY**, applied on top of the
STORED args; the **recipient (`_lead_id`/phone), the tool, and every booking target stay
server-bound** and cannot be client-overridden. The body was always the owner's own free text
(authored to the server-bound recipient, as that tenant, on that tenant's own number) — so
accepting an edit introduces no recipient-swap, action-swap, or cross-tenant escalation. What
SF-6 closes — swap the action, swap who gets texted, replay/double-send — stays closed.

## Idempotency (F11 §4.2)
Atomic claim (`UPDATE … WHERE consumed=0`) is the single source of truth: exactly one
redemption executes. `text_lead` never double-sends; `book_estimate` never double-books.
PRIMARY KEY on `token_id` + the conditional UPDATE prevent any race even multi-worker.

## Honesty / consent gates carried in (non-negotiable)
- "You approve exactly what you saw" is now true at the SERVER for tool + recipient + target.
- One-tap stays one-tap. The token is a *proposal*; nothing sends until the owner taps.
- No new owner-facing claims; pricing/voice copy untouched.

## Tests — `test_confirm_token.py` (standalone, real DB, un-stubbed)
1. issue → redeem executes STORED args (recipient/tool bound).
2. replay (second redeem, same token) → returns stored result, action runs ONCE
   (assert exactly one outbound send / one booking).
3. expired token → honest reply, no execution.
4. cross-tenant: biz B cannot redeem biz A's token (not found, no execution).
5. unknown/missing token → honest reply / 400, no execution.
6. text_lead body edit override → edited body sent to the STORED recipient; a forged
   recipient in the POST is ignored.
7. token absent from pending for read/info tools (no token issued when no action gated).
Re-run the FULL suite (must stay green) + the existing assistant/confirm tests.

## Out of scope for 5a (later sub-phases)
P0-2 Sonnet switch / P0-3 dollar cap (separate), enforce_ack two-stage (5b/F11 P1-6),
pre-issued briefing-poll tokens (5b), Redis overlay (P2-6).
