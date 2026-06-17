#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# RingBack — local test instance for hands-on testing. NOT a deploy.
#
#   ./run_local.sh
#   open http://localhost:8800   ->   log in with the creds printed below
#
# Uses its OWN database (local_test.db, gitignored) so your real ringback.db is
# never touched, and the keyless "demo" brain so no API keys are needed. On first
# run it seeds the owner login + a few sample leads so the command center has
# something to act on (try: "show my leads", then "text the second lead saying
# running 10 minutes late" — you'll see the honest confirm before anything sends).
# -----------------------------------------------------------------------------
set -e
cd "$(dirname "$0")"

# Brain: inherit from .env (config.py loads it) -- so RINGBACK_PROVIDER=claude + your
# ANTHROPIC_API_KEY there runs the real Claude brain locally. With no key set, config safely
# falls back to the keyless demo brain, so this stays zero-setup. To force the demo brain
# regardless of .env:  RINGBACK_PROVIDER=demo ./run_local.sh
# NOTE: running Claude here spends your Anthropic API credit on every chat turn.
export RINGBACK_DB_PATH="$PWD/local_test.db"          # isolated DB, never the real one
export RINGBACK_OWNER_EMAIL="owner@ringback.local"
export RINGBACK_OWNER_PASSWORD="test1234"

# First-run seed: importing app initializes the DB + seeds the owner; add demo leads.
.venv/bin/python - <<'PY'
import app, db          # importing app runs init_db + seeds the owner login
if not db.leads_with_stage(1):
    for n, p in [("Maria Cortez", "+15125550111"),
                 ("Dave Ruiz",    "+15125550122"),
                 ("Sarah Hill",   "+15125550133")]:
        db.create_lead(1, n, p)
    print("[run_local] seeded 3 sample leads")
PY

echo "-----------------------------------------------------------------"
echo " RingBack local  ->  http://localhost:8800"
echo " Login:  owner@ringback.local  /  test1234"
echo " Brain:  demo (no API keys)    DB: local_test.db (isolated)"
echo " Ctrl-C to stop."
echo "-----------------------------------------------------------------"
exec .venv/bin/python app.py
