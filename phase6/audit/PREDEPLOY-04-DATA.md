# PREDEPLOY-04-DATA — Data Integrity / DB / Migrations Audit

**Branch:** staging @ 55d2601  
**Auditor:** DATA lane (auditor 4 of 10)  
**Scope:** db.py init_db/migrations, indexes, transactions, WAL/backup daemon, DB_PATH, FK integrity, parameterized queries, int casts.

---

## VERDICT: DEPLOY-SAFE (0 P0, 1 P1, 3 P2)

---

## P0 — Data-loss / deploy blockers

**None found.**

---

## P1 — Should fix before or immediately after deploy

### P1-1 · `consume_password_reset_token` TOCTOU race — db.py:3522–3538

The SELECT-then-UPDATE pattern is not atomic. Two concurrent requests with the same token can both pass the `row["used"]` check, then both `UPDATE SET used=1` and both return a valid `user_id`. Under SQLite's single-writer model with 8 threads the race window is narrow, but it exists.

**Compare:** `claim_confirm_token` (db.py:3199–3205) does it correctly: `UPDATE … WHERE consumed=0` + checks `rowcount == 1` — only one winner. `consume_password_reset_token` should match that pattern.

**Impact:** A simultaneous double-submit of the reset form with the same token could allow one extra password change. Not a data-loss risk, but a security invariant violation.

**Fix:**
```python
# Replace SELECT+check+UPDATE with:
cur = conn.execute(
    "UPDATE password_reset_tokens SET used=1 "
    "WHERE token=? AND used=0 AND expires_at > ?", (token, now))
conn.commit()
if cur.rowcount != 1:
    conn.close()
    return None
row = conn.execute("SELECT user_id FROM password_reset_tokens WHERE token=?", (token,)).fetchone()
uid = row["user_id"] if row else None
conn.close()
return uid
```

---

## P2 — Minor / low-risk findings

### P2-1 · `growth_touch_index` exclusion list narrower than `uniq_growth_touch_per_lead` — db.py:2613–2616

The DB partial index (line 790–792) excludes `('reminder','followup','sms_retry','morning_reminder')`.  
The `growth_touch_index()` query (line 2616) only excludes `('reminder','followup')`.

**Impact:** `sms_retry` and `morning_reminder` rows can appear spuriously in the `touched` set consulted by `growth.plays()`. However, `plays()` only checks for kinds `review_request`, `quote_followup`, `reactivation`, `winback`, `referral`, `membership` — none of which are `sms_retry` or `morning_reminder`. So no false positives or false negatives result in practice. The exclusion lists should be kept in sync via a shared constant to prevent future divergence.

**Severity:** Cosmetic inconsistency, not a correctness bug today.

### P2-2 · `usage_grants.period_start` schema mismatch — db.py:383 vs 724

The initial `CREATE TABLE IF NOT EXISTS usage_grants` at line 380–390 defines `period_start TEXT, period_end TEXT`. A later migration at line 720–732 re-declares them as `INTEGER`. Because `CREATE TABLE IF NOT EXISTS` is a no-op when the table exists, the first schema (TEXT) always wins. Stripe invoices write Unix integer timestamps into this TEXT column; SQLite stores them as the string `'1700000000'`.

**Impact:** `conversations_remaining()` (line 3463) ignores `period_start`/`period_end` entirely (uses current calendar month instead). No functional breakage. The column type mismatch is harmless in SQLite's type-affinity system.

**Fix:** Remove the duplicate `usage_grants` CREATE from the migration block (line 720–732) since it is dead code; or consolidate to a single definition.

### P2-3 · `release_growth_batch` calls `consent_basis_for_lead` on a separate connection mid-transaction — db.py:2694

`release_growth_batch` holds an open, uncommitted transaction on `conn`. Inside its loop it calls `consent_basis_for_lead(business_id, row[1])` which opens a *new* connection to read `appointments`/`messages`. Under SQLite DELETE journal mode with a single writer this works but: (a) the consent_basis reads are outside the batch release transaction (if the loop crashes mid-way, some `growth_approvals` rows are missing even if the UPDATE committed), and (b) in WAL mode this would be fine; in DELETE mode with 8 threads, the second connection's SELECT will wait if another write is in progress.

**Impact:** If a crash occurs after `UPDATE scheduled_messages SET status='pending'` but before `conn.commit()`, the UPDATE is rolled back. The `growth_approvals` INSERTs in the loop are also part of `conn` and also rolled back — so the batch and its audit log remain consistent. The missing-approval edge case only occurs if the loop raises an exception AFTER the UPDATE (e.g., on a malformed lead row), which leaves the batch partially committed. This is a minor audit-log gap, not a data-loss issue.

---

## Confirmed CLEAN areas

### init_db idempotency ✓
- **Verified on real data:** double-call of `init_db()` on a copy of the staging DB leaves all 7 businesses, 26 leads, 2 appointments, 20 scheduled_messages unchanged.
- All ALTER TABLE migrations are column-existence-guarded via `PRAGMA table_info`.
- All DELETE collapse operations are gated: `if "uniq_*" not in existing_idx` — they run exactly once per index lifecycle.
- The growth index rebuild (lines 767–792) re-checks the stored SQL for `sms_retry`/`failed` presence before dropping and recreating — correct.

### One-time DELETE guards: no real-data risk ✓
- **Double-book collapse** (lines 465–470): `UPDATE SET status='canceled' WHERE status='booked' … AND id NOT IN (SELECT MIN(id) … GROUP BY day, slot_time)` — runs only when `uniq_booked_slot` doesn't exist. Keeps the earliest booking per slot. **Cannot cancel a genuinely-unique booked slot.**
- **Followup dedup DELETEs** (lines 741–742, 749–750): `DELETE WHERE kind='followup'/'followup_2' AND id NOT IN (SELECT MIN(id) … GROUP BY lead_id)` — runs only when index absent. Keeps earliest per lead. No real sends are lost (these are pending future sends).
- **Growth touch dedup DELETE** (lines 783–788): runs only when `_needs_growth_idx_rebuild=True`. Keeps earliest active per (lead, kind). The condition excludes `'canceled','failed'` so completed touches are untouched.
- **All verified:** No path exists where these DELETEs run on a DB that already has the corresponding index.

### Partial UNIQUE indexes ✓
- `uniq_booked_slot ON appointments(business_id, day, slot_time) WHERE status='booked' AND day IS NOT NULL AND slot_time IS NOT NULL` — correctly scoped by business_id. Verified in staging DB.
- `uniq_followup_per_lead ON scheduled_messages(lead_id) WHERE kind='followup'` — correct.
- `uniq_followup_2 ON scheduled_messages(lead_id) WHERE kind='followup_2'` — correct.
- `uniq_growth_touch_per_lead ON scheduled_messages(lead_id, kind) WHERE kind NOT IN ('reminder','followup','sms_retry','morning_reminder') AND status NOT IN ('canceled','failed')` — Phase 6c fix confirmed in index SQL.
- No double-booked slots exist in current staging DB.

### Transactions/atomicity ✓
- **`book_appointment`** (db.py:1472–1495): INSERT + UPDATE leads on same `conn`, COMMIT once, `IntegrityError` → rollback. Atomic. ✓
- **`cancel_appointment`** (db.py:2902–2931, Phase 6c W7): appointment status flip + reminder cancellation on same `conn`, single COMMIT. The comment explicitly calls this out. ✓
- **`claim_confirm_token`** (db.py:3194–3205): `UPDATE WHERE consumed=0`, checks `rowcount==1`. Atomic race-safe. ✓
- **`claim_scheduled_message`** (db.py:2559–2568): `UPDATE WHERE status='pending'`, checks `rowcount==1`. Atomic. ✓
- **`record_screening_rescue`** (db.py:2096–2124): contact upsert + business counter increment on same `conn`. Atomic. ✓

### WAL/backup daemon ✓
- Durable local-disk mode: SQLite runs on `/tmp/firstback.db` (local, no hang). `backup_to_durable` uses `VACUUM INTO` (consistent snapshot) → plain `os.replace` to `/var/data/firstback.db`. Anti-clobber guard: refuses to overwrite populated backup with empty live DB (`_business_count` check at line 106).
- `restore_from_backup_if_needed`: plain `shutil.copy2` from backup → live; only if live doesn't exist. Safe on warm restart.
- `render.yaml` correctly sets `FIRSTBACK_DB_PATH=/var/data/firstback.db` and `FIRSTBACK_DB_LOCAL_MIRROR=1`.
- No path in code that silently initializes with an empty DB from a misconfigured path (code defaults to `BASE_DIR/firstback.db`; Render's render.yaml always sets the env var).

### Parameterized queries / SQL injection ✓
- All user-controlled values go through `?` parameters.
- f-string SQL appears only in column-name selection (from `_BUSINESS_COLS`, `cols` filtered against known-good constants) and dynamic `WHERE` clauses built by appending `AND col=?` with values as params. The `_GROWTH_EXCLUSION` / `_GROWTH_ACTIVE` f-strings embed fixed module-level string constants, not user input.
- No raw string interpolation of external values found.

### int casts on external IDs ✓
- Route IDs from URL paths use Flask's `<int:appt_id>` converter (line 2079) — cast at the framework level.
- `business_id` is derived from `current_business()["id"]` (from the session/DB, not user input).
- `int()` called on token/duration fields where numeric external values enter (e.g., `int(request.form.get("CallDuration") or 0)` at app.py:3081).

### FK integrity (no ON DELETE CASCADE, no FK PRAGMA) ✓
- SQLite FK enforcement is OFF (no `PRAGMA foreign_keys = ON` call). No `REFERENCES` constraints in schema.
- **No business-delete path exists** in any route or db.py function — `DELETE FROM businesses` never appears. Tenant data cannot be orphaned by a business deletion.
- Orphan check on staging DB: 0 orphaned appointments, messages, scheduled_messages, users. Clean.

---

## Staging DB state (verified)
- 7 businesses, 26 leads, 2 booked appointments, 20 scheduled_messages (sent/simulated)
- 0 double-booked slots, 0 orphaned rows
- All required indexes present including both `uniq_growth_touch_per_lead` (with 6c fix) and `uniq_booked_slot` (business_id-scoped)
- `subscription_status='active'` backfill confirmed in schema (DEFAULT 'active', plus UPDATE WHERE NULL)

---

*Audit completed: 2026-06-19*
