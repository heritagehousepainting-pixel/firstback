# Hot File Governance

The app has three high-churn/high-blast-radius files. Do not casually split them during feature work;
govern them until an extraction has a tight reason and tests around the boundary.

## Hot Files

- `app.py`
  - Owns Flask routes, request/response contracts, session security, webhook entrypoints, and HTML page wiring.
  - Rule: keep route behavior changes small and grouped by surface. Add or update tests before touching webhook,
    billing, auth, assistant, setup, or customer-texting routes.

- `db.py`
  - Owns SQLite connection behavior, migrations, schema creation, and most persistence helpers.
  - Rule: migration changes must be idempotent, tenant-scoped, and covered by a standalone test. Avoid mixing
    schema work with unrelated behavior edits.

- `assistant.py`
  - Owns Vic's tool registry, deterministic routing, confirm-gated actions, streaming, and memory hooks.
  - Rule: any customer-outbound or state-changing tool must remain in the central `TOOLS` registry with an
    explicit `confirm` decision. Streaming and non-streaming paths must keep the same result shape.

## Extraction Order

1. Extract only leaf helpers first.
   - Good candidates: formatting helpers, route-local parsing, and pure DTO/card builders.

2. Extract one surface at a time.
   - Good candidates: `app_setup.py`, `app_callers.py`, `app_assistant.py`, or Flask blueprints once route tests
     can prove URLs and response shapes did not move.

3. Split persistence by domain only after migrations are stable.
   - Good candidates: `db_contacts.py`, `db_billing.py`, `db_assistant_memory.py`.
   - Keep `get_conn`, boot migrations, and backup behavior centralized until a real migration framework exists.

4. Preserve public contracts.
   - Assistant: `run()`, `run_stream()`, and `execute()` return `{reply, cards, pending_action, meta}`.
   - Messaging: outbound customer sends pass through `messaging.send_sms`.
   - Setup/live state: never claim live unless `connections.is_live()` says so.

## Review Checklist

- Does this touch `app.py`, `db.py`, or `assistant.py`?
- If yes, is the change isolated to one surface?
- Is there a test for the route/tool/schema behavior?
- Does it preserve tenant scoping by `business_id`?
- Does it preserve the confirm gate for customer-visible outbound actions?
- Does it avoid deploy/env assumptions that belong in docs or ops?
