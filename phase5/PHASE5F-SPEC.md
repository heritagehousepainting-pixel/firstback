======================================================================
PHASE 5F -- F08 CONTACTS NIGHTLY SYNC + BULK-ACCEPT
Build-ready spec. LOCKED 2026-06-18. READ-ONLY reference; edit nothing here.
======================================================================

----------------------------------------------------------------------
1. STATE OF PLAY (what already exists -- code-cited)
----------------------------------------------------------------------

BUILT (ship-ready, no changes needed):
  - google_contacts.configured()          google_contacts.py:37-39
      True when GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET are set.
      Controls the "Connect Google Contacts" gate in the UI.

  - google_contacts.is_connected(biz_id)  google_contacts.py:42-45
      Checks integrations table for provider='google_contacts' + refresh_token.
      Two-condition gate: app credentials present AND this business connected.

  - google_contacts._access_token(biz_id) google_contacts.py:89-115
      Returns a valid access token, auto-refreshing via the People API.
      Swallows + logs any refresh error, returns None on failure (no raise).

  - google_contacts.sync(business_id)     google_contacts.py:160-164
      Full People API fetch -> contact_import.ingest() with source='import-google'.
      Returns ingest's summary dict {contacts, suggested, skipped, unclassified}.

  - google_contacts.fetch_contacts()      google_contacts.py:119-147
      Paginates People API (1000/page, 50-page cap = 50k contacts max).
      Swallows network errors per page (partial import on failure, no crash).
      Gap: on partial failure, no notice to Dave (F08-FINAL §5 / S2 partial-fix).

  - OAuth flow (connect/callback/disconnect) google_contacts.py:49-82
      State param CSRF guard, token exchange, db.set_oauth_tokens.

  - Callers page template + UI           templates/callers.html:22-33
      "Connect Google Contacts" button gated on gc_configured (app.py:1091).
      "Sync from Google" button shown when gc_connected (callers.html:22-25).
      "Coming soon" shown when not gc_configured (callers.html:29-31).

  - Manual sync endpoint                 app.py:2216-2227
      POST /api/contacts/google/sync -> google_contacts.sync(biz_id).
      Gated: is_connected() check, returns 400 if not connected.

  - Suggestion queue (the inbox)         db.py:288-293
      contact_suggestions table: business_id, number, name, suggested_category,
      reason, source, status (pending/accepted/dismissed), timestamps.
      UNIQUE(business_id, number): one pending suggestion per number per biz.

  - upsert_suggestion()                  db.py:2232-2250
      ON CONFLICT WHERE status='pending': never overwrites accepted/dismissed.
      Invariant: "dismiss once, never re-surfaced" is enforced here.

  - Bulk-accept endpoint (ALREADY EXISTS) app.py:2129-2158
      POST /api/suggestions/bulk {ids: [...], action: "accept"|"dismiss"|"reopen"}
      Per-suggestion: get_suggestion(id, biz_id) -> tenant scope check -> write.
      Calls db.set_contact() + db.set_suggestion_status("accepted").
      This IS the bulk-accept endpoint. It is complete.

  - Per-suggestion accept/dismiss/reopen  app.py:2085-2126
      Single-item routes. set_contact() writes to directory on accept.
      delete_contact() reverts on reopen of an accepted suggestion.

  - Bulk-accept UI (ALREADY EXISTS)      static/app.js:683-721, callers.html:48-52
      Checkboxes on "To review" tab. Bulk toolbar appears on selection.
      "Accept selected" -> POST /api/suggestions/bulk {action:"accept"}.
      "Dismiss selected" -> POST /api/suggestions/bulk {action:"dismiss"}.
      This IS the bulk-accept UI. It is complete.

  - Suggestion review inbox UI           static/app.js:600-737, callers.html:39-53
      Three tabs: To review / Sorted / Dismissed.
      Per-row accept (with category dropdown) + dismiss buttons.
      Search filter (name, phone, reason).

  - contact_import.ingest()              contact_import.py (ingest function)
      Pre-sort: booking history -> customer, ORG field + no booking -> vendor.
      Dedup: skips numbers in contacts + accepted + dismissed suggestions.
      Returns {contacts, suggested, skipped, unclassified}.

  - tick_once()                          reminders.py:554-602
      The scheduler pass. Runs triage.scan_all_suggestions() (line 567).
      Pattern for the nightly contacts re-sync addition (see S2 below).

MISSING (the build target for 5f):
  - google_contacts_sync_all() in reminders.py
      ~20-line function. Nightly per-business sync. Not yet written.
  - Cadence guard: "only once per UTC day per business"
      No db tracking for last contacts sync date exists yet.
  - Partial-sync failure notice
      fetch_contacts() silently returns partial results. No notice to Dave.
  - Import summary UI: zero-result messages (S3 from F08-FINAL)
      Missing 422 branch for "contacts found but none have phone numbers".


----------------------------------------------------------------------
2. OWNER-OPS (what the OPERATOR must set; code is INERT without these)
----------------------------------------------------------------------

These three env vars must be set in Render (or .env for local dev) BEFORE
the nightly sync or the "Connect Google Contacts" button has any effect.
Until they are set, configured() returns False (google_contacts.py:37-39),
the button shows "Coming soon" (callers.html:29-31), and the nightly sync
function skips every business (is_connected() returns False for all).

  GOOGLE_CLIENT_ID          # config.py:168 -- Shared with Calendar OAuth.
  GOOGLE_CLIENT_SECRET      # config.py:169 -- Same OAuth client as Calendar.
  GOOGLE_CONTACTS_REDIRECT_URI  # config.py:175-177
      Default: http://127.0.0.1:8800/api/contacts/google/callback
      Production: https://app.firstback.io/api/contacts/google/callback

Google Cloud Console steps (OPERATOR does these once):
  1. Open the existing OAuth 2.0 Client ID (already created for Calendar).
  2. Add GOOGLE_CONTACTS_REDIRECT_URI to "Authorized redirect URIs".
     (The same client covers both scopes; contacts.readonly is added at consent time.)
  3. Ensure the OAuth client's "Application type" is "Web application".

Per-business steps (each OWNER does once, after the operator sets creds):
  - Open /callers -> click "Connect Google Contacts".
  - Google OAuth consent -> grants contacts.readonly scope.
  - App stores refresh token via db.set_oauth_tokens(biz_id, 'google_contacts', ...).
  - First sync runs automatically on OAuth return (app.js:899-901).
  - Nightly sync then runs automatically for this business from next tick.

The code enforces this in two layers:
  Layer 1 -- configured(): GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET must be set
             (google_contacts.py:37-39). Checked before every OAuth redirect and
             in the nightly sync (skip unless configured()).
  Layer 2 -- is_connected(biz_id): the business must have a refresh_token stored
             (google_contacts.py:42-45). Checked in the nightly sync per business.

INERT GUARANTEE: if either layer fails, the nightly sync function logs nothing,
touches no suggestion rows, and returns immediately. Zero risk of false "synced"
state or errors bubbling to the user.


----------------------------------------------------------------------
3. NIGHTLY RE-SYNC DESIGN (tick_once addition -- ~20 lines)
----------------------------------------------------------------------

FUNCTION: google_contacts_sync_all(now=None)
FILE: reminders.py (add after scan_screening_graduation, before tick_once)
RETURNS: dict {businesses_checked, businesses_synced, suggestions_created}

GATING (fail-safe hierarchy, checked in order):
  1. google_contacts.configured() -- app credentials set. If False: return immediately,
     no log (not an error; operator simply hasn't set creds yet).
  2. Per business: google_contacts.is_connected(biz_id) -- has refresh token.
     If False: skip silently (business never connected Contacts).
  3. Cadence guard: only run once per UTC day per business.
     Key: meta key "contacts_sync_date:{biz_id}" stores the last ISO date (UTC).
     If today's date == stored date: skip.

CADENCE / DEDUPE:
  - Runs on every tick_once call (every TICK_SECONDS, default 60s).
  - The cadence guard makes it a no-op on all but the first tick after midnight UTC.
  - No separate cron job needed; piggybacks on the existing ticker.
  - Implementation uses db.get_meta / db.set_meta (already used for heartbeat).
    Key: "contacts_sync_date:{biz_id}". Value: UTC date string "YYYY-MM-DD".

IDEMPOTENCY:
  - contact_import.ingest() already deduplicates via the 'skip' set:
    numbers in contacts + accepted + dismissed suggestions are skipped.
  - upsert_suggestion() only touches pending rows (db.py:2247 WHERE clause).
  - Running sync twice on the same day: second pass finds all numbers in 'skip'
    set -> returns {suggested: 0, skipped: N}. No duplicate suggestions. Safe.

PARTIAL-SYNC FAILURE NOTICE:
  - fetch_contacts() catches exceptions per page and breaks the loop (google_contacts.py:136-139).
  - google_contacts_sync_all() detects partial sync: track total returned vs
    expected (via nextPageToken absence before _MAX_PAGES is reached).
  - Simple heuristic: if fetched == 0 and is_connected() is True, log stderr notice.
    Full partial-page detection is deferred (L/later) per F08-FINAL §5.

BADGE ON INBOX ICON (if new suggestions created):
  - ingest() returns {suggested: N}. If N > 0, the inbox badge is already updated
    on next /api/suggestions load (count_pending_suggestions is called on each
    /api/suggestions GET). No extra work needed from the nightly sync itself.

PSEUDO-CODE (~20 lines):

    def google_contacts_sync_all(now=None):
        import google_contacts
        if not google_contacts.configured():
            return {"businesses_checked": 0, "businesses_synced": 0, "suggestions_created": 0}
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        checked = synced = created = 0
        for biz in db.list_businesses():
            bid = biz.get("id")
            if not bid:
                continue
            checked += 1
            if not google_contacts.is_connected(bid):
                continue
            cadence_key = f"contacts_sync_date:{bid}"
            if db.get_meta(cadence_key) == today_utc:
                continue  # already ran today for this business
            try:
                result = google_contacts.sync(bid)
                db.set_meta(cadence_key, today_utc)
                synced += 1
                created += result.get("suggested", 0)
            except Exception as e:
                print(f"[firstback] contacts nightly sync failed (biz {bid}): {e}",
                      file=sys.stderr, flush=True)
        return {"businesses_checked": checked, "businesses_synced": synced, "suggestions_created": created}

INSERTION POINT IN tick_once() (reminders.py:554):
  Add after scan_screening_graduation (line 600), before run_due_once (line 601):

        # 5f: nightly Google Contacts re-sync (per-business, cadence-gated).
        try:
            google_contacts_sync_all(now)
        except Exception as e:
            print(f"[firstback] contacts nightly sync tick failed: {e}", file=sys.stderr, flush=True)

  Pattern mirrors every other scan in tick_once (try/except, never fatal).


----------------------------------------------------------------------
4. BULK-ACCEPT ENDPOINT + UI (CURRENT STATE)
----------------------------------------------------------------------

STATUS: ALREADY COMPLETE. No new code needed.

Endpoint (app.py:2129-2158):
  POST /api/suggestions/bulk
  Body: {ids: [int, ...], action: "accept" | "dismiss" | "reopen"}
  - Each id: tenant-scoped via get_suggestion(id, biz_id). Unknown ids: skipped.
  - accept: db.set_contact() writes to directory + db.set_suggestion_status("accepted").
  - dismiss: db.set_suggestion_status("dismissed").
  - reopen (accepted): db.delete_contact() + set to pending.
  Returns: {ok: true, count: N}

UI (static/app.js:683-721, callers.html:48-52):
  - "To review" tab: checkboxes per row, select-all header checkbox.
  - Bulk toolbar appears when >=1 selected: "Accept selected" + "Dismiss selected".
  - "Accept selected" -> bulk({action:"accept"}) -> POST /api/suggestions/bulk.
  - "Sorted" / "Dismissed" tabs: "Undo selected" available.
  - After bulk action: selected.clear() + loadTab() + loadDirectory().

"Accept all customers" CTA (from F08-FINAL §3):
  The existing bulk-accept covers this: owner checks all, clicks "Accept selected".
  A dedicated "Accept all N customers" one-tap button is NOT currently in the UI.
  Decision: Add a single "Accept all" shortcut button in the bulk toolbar for the
  "pending" tab to match the F08 spec ("two taps, thirty seconds, done").
  This is a UI-only addition (~5 lines of HTML/JS). See slice S2-UI below.


----------------------------------------------------------------------
5. FILE-DISJOINT SLICE SPLIT
----------------------------------------------------------------------

SLICE S1: nightly sync function (pure backend, no UI touch)
  Files touched:
    reminders.py     -- add google_contacts_sync_all() + hook into tick_once()
    test_f08_nightly.py  -- NEW test file (all tests for S1)
  Files NOT touched:
    app.py, db.py, google_contacts.py, contact_import.py, templates/*, static/*

SLICE S2-UI: "Accept all N" shortcut button (pure frontend, no backend touch)
  Files touched:
    static/app.js    -- add "Accept all N" button render in syncBulk() pending branch
    templates/callers.html  -- no change needed (button rendered by JS)
  Files NOT touched:
    reminders.py, app.py, db.py, google_contacts.py, contact_import.py

SLICE S3: import summary zero-result messages (app.py only)
  Files touched:
    app.py           -- add branches at api_contacts_import() (around line 2185)
    test_import.py   -- extend existing test (or new test_f08_import.py)
  Files NOT touched:
    reminders.py, db.py, google_contacts.py, static/app.js, templates/*

  NOTE: S3 is from F08-FINAL §6 S3. It is included in 5f scope because it's a
  <15-line change and directly affects the import UX that 5f exercises.

COLLISION RISK:
  S1 + S2-UI are file-disjoint. Can be built and PR'd simultaneously if needed.
  S3 touches app.py independently from any other 5f change. Safe to parallelize.
  No file is touched by more than one slice.


----------------------------------------------------------------------
6. TESTS
----------------------------------------------------------------------

NEW TEST FILE: test_f08_nightly.py (covers S1)

  T1 -- inert when not configured:
    Monkeypatch google_contacts.configured() = False.
    Call google_contacts_sync_all(). Assert: 0 synced, 0 suggestions, no DB write.

  T2 -- inert when no business is connected:
    configured() = True. Two businesses, neither is_connected.
    Call google_contacts_sync_all(). Assert: 0 synced, 0 suggestions.

  T3 -- syncs connected business, skips unconnected:
    configured() = True. Biz 1 connected, Biz 2 not.
    Monkeypatch google_contacts.sync(biz_id) = {"suggested": 3, ...}.
    Call google_contacts_sync_all(). Assert: synced=1, created=3.
    Assert db.get_meta("contacts_sync_date:1") == today_utc.
    Assert db.get_meta("contacts_sync_date:2") is None.

  T4 -- cadence guard: second call same day is a no-op:
    Pre-set meta "contacts_sync_date:1" = today_utc. Biz 1 connected.
    Monkeypatch sync to raise if called (must not be called).
    Call google_contacts_sync_all(). Assert: synced=0, no exception.

  T5 -- cadence guard: different day triggers sync:
    Pre-set meta "contacts_sync_date:1" = "2000-01-01" (old date).
    Biz 1 connected. sync() monkeypatched to return {"suggested": 2}.
    Call google_contacts_sync_all(). Assert: synced=1.

  T6 -- exception in sync is swallowed, other businesses proceed:
    Biz 1 connected, sync raises. Biz 2 connected, sync ok.
    Call google_contacts_sync_all(). Assert: no exception raised, synced=1.

  T7 -- tick_once calls google_contacts_sync_all (smoke test):
    Monkeypatch google_contacts_sync_all to record called=True.
    Call tick_once(). Assert called=True.

  T8 (S3) -- import zero-result: contacts found, none have phones:
    POST /api/contacts/import with a vCard that has no phone numbers.
    Assert 422 response with human-readable message.

EXISTING TESTS TO VERIFY (must remain green after 5f):
  test_ticker_health.py  -- tick_once heartbeat write (no change expected)
  test_reminders.py      -- reminder/followup logic (no change expected)
  test_f04_google.py     -- Calendar OAuth pattern (no change expected)
  test_import.py         -- contact import parse (no change expected)


----------------------------------------------------------------------
7. INTEGRATION RISKS
----------------------------------------------------------------------

RISK 1 (HIGHEST) -- Google People API quota exhaustion on large fleets.
  If the platform scales to hundreds of businesses each with 50k contacts,
  the nightly sync loop runs 50 API pages per business per night. Google's
  People API default quota is 1000 requests/minute per project (shared across
  all businesses). At 50 requests per business, 20+ simultaneous large syncs
  could hit the quota ceiling.
  MITIGATION (5f scope): cadence guard prevents more than once/day per business.
  DEFERRED: request-per-minute rate limiter across the loop (add when fleet > 20).

RISK 2 -- Token refresh race on concurrent ticks.
  Two tick_once calls (in-process + external cron) running simultaneously could
  both hit _access_token() and race on the token write. Google returns the same
  refresh token, so the race is idempotent (last writer wins, both tokens valid).
  MITIGATION: db.set_oauth_tokens() uses ON CONFLICT REPLACE (SQLite single-writer).
  No code change needed.

RISK 3 -- Meta key namespace collision.
  "contacts_sync_date:{biz_id}" pattern. Check existing meta keys in db.py:
  "last_tick_utc" (reminders.py). No collision risk; different key prefix.
  Pattern: one meta row per business per feature. Scales cleanly.

RISK 4 -- ingest() modifying contact_suggestions while a bulk-accept is in flight.
  SQLite's WAL mode serializes concurrent writes. Worst case: sync writes a new
  suggestion row while the owner is clicking "Accept selected" -- the row appears
  on next loadTab(). The bulk endpoint's get_suggestion() tenant scope check ensures
  no cross-tenant contamination. Not a correctness risk.

RISK 5 -- "Accept all N" button accepting suggestions across categories.
  If pending suggestions are a mix of customer + vendor + spam, "Accept all" writes
  each contact with its suggested_category -- same as the existing bulk endpoint.
  Owner should verify the "To review" tab before using "Accept all". Document in
  the button tooltip: "Accepts each caller with the suggested category shown."

RISK 6 -- No-creds deploy: "Coming soon" label after operator sets creds.
  After GOOGLE_CLIENT_ID/SECRET are set and the server restarts, configured()
  returns True and the button changes from "Coming soon" to "Connect Google Contacts"
  automatically (callers.html:26-31). No code change or redeploy of templates needed.
  Risk: Render caches the HTML. Clear cache / hard reload resolves.


----------------------------------------------------------------------
8. CODE vs OWNER-OPS vs DEFERRED
----------------------------------------------------------------------

CODE (5f builds this):
  - google_contacts_sync_all() in reminders.py  (~22 lines)
  - tick_once() addition: the try/except call block  (~5 lines)
  - "Accept all N" shortcut button in JS syncBulk()  (~5 lines)
  - Import zero-result 422 branch in api_contacts_import()  (~10 lines)
  - test_f08_nightly.py  (new test file, ~120 lines)

OWNER-OPS (operator must do before sync is real):
  - Set GOOGLE_CLIENT_ID in Render env
  - Set GOOGLE_CLIENT_SECRET in Render env
  - Set GOOGLE_CONTACTS_REDIRECT_URI in Render env
    (https://app.firstback.io/api/contacts/google/callback)
  - In Google Cloud Console: add GOOGLE_CONTACTS_REDIRECT_URI to the
    existing OAuth 2.0 Web Client's "Authorized redirect URIs"

DEFERRED (not in 5f):
  - S1 partial-page detection + "we'll retry tonight" notice (F08-FINAL §5 M3 add-on)
  - S5 auto-confirm toggle schema + UI (F08-FINAL §6 S5)
  - M1 contact_group_id grouping (F08-FINAL §6 M1)
  - M2 nudge-card for repeat-client pending >30d (F08-FINAL §6 M2)
  - M3 lead<->contact thread merge on accept (F08-FINAL §6 M3)
  - M4 spoof-alert badge (F08-FINAL §6 M4)
  - M5 Jobber/HCP CSV field mapping (F08-FINAL §6 M5)
  - L1-L3 CRM OAuth integrations (F08-FINAL §6)
  - "Recognized" badge on call log (F08-FINAL §7)
  - "Not my customer" button on trusted-call notification (F08-FINAL §6 S4)


----------------------------------------------------------------------
9. GATES
----------------------------------------------------------------------

GATE A (operator gate -- must be true before 5f code matters):
  google_contacts.configured() returns True.
  Verified by: curl https://app.firstback.io/api/contacts/google/connect
  (redirects to Google if configured; returns 302 to /callers?gcerror=unconfigured if not).

GATE B (per-business gate -- must be true for a given business to sync):
  google_contacts.is_connected(biz_id) returns True.
  Verified by: /callers page shows "Google Contacts connected." label.

GATE C (test gate -- before merge):
  All 8 tests in test_f08_nightly.py pass.
  All existing tests in test_ticker_health.py, test_reminders.py pass.
  tick_once() return dict now includes "contacts_synced" key.

GATE D (honesty gate -- invariants that must not break):
  - google_contacts_sync_all() MUST NOT be called when configured() is False.
    Enforced: first line of function.
  - Sync MUST NOT auto-apply suggestions to the directory.
    Enforced: google_contacts.sync() calls contact_import.ingest() which calls
    upsert_suggestion(), not set_contact(). Directory only changes on owner tap.
  - upsert_suggestion() MUST NOT overwrite accepted/dismissed rows.
    Enforced: db.py:2247 WHERE contact_suggestions.status='pending'.


----------------------------------------------------------------------
10. SUMMARY TABLE
----------------------------------------------------------------------

Feature              | Built? | File(s)                  | Lines est.
---------------------|--------|--------------------------|------------
configured() gate    | YES    | google_contacts.py:37    | --
is_connected() gate  | YES    | google_contacts.py:42    | --
OAuth flow           | YES    | google_contacts.py:49-82 | --
_access_token()      | YES    | google_contacts.py:89    | --
sync(biz_id)         | YES    | google_contacts.py:160   | --
Manual sync endpoint | YES    | app.py:2216              | --
Bulk-accept endpoint | YES    | app.py:2129              | --
Bulk-accept UI       | YES    | static/app.js:683        | --
NIGHTLY SYNC FUNC    | NO     | reminders.py (add)       | ~22
tick_once hook       | NO     | reminders.py:600 (add)   | ~5
"Accept all N" btn   | NO     | static/app.js (add)      | ~5
Import zero-result   | NO     | app.py:~2185 (add)       | ~10
test_f08_nightly.py  | NO     | (new file)               | ~120
======================================================================
