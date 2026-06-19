"""FirstBack — central configuration.

Everything you'll want to tweak early lives here. Change a value, restart the
server, done.
"""
import os
from datetime import datetime
from pathlib import Path

# Load a local .env (if present) so secrets like MINIMAX_API_KEY stay out of code.
# Real environment variables always win over .env values.
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _val = _line.split("=", 1)
            os.environ.setdefault(_key.strip(), _val.strip().strip('"').strip("'"))

# --- Branding -------------------------------------------------------------
APP_NAME = "FirstBack"
TAGLINE = "Never lose another job to a missed call."

# --- AI brain / provider --------------------------------------------------
# Which brain answers leads. Pick the provider here:
#   "minimax" -> MiniMax (what we run today)
#   "claude"  -> Anthropic Claude (what we switch to for the public launch)
#   "demo"    -> built-in rule-based script (zero setup, but no real understanding)
# If the chosen provider has no API key, FirstBack safely falls back to the demo
# brain so the app always runs.
# Default is "claude": the recommended brain for the launch (richest multi-step agentic
# writes + live token streaming on /assistant/stream). It engages only once ANTHROPIC_API_KEY
# is set -- with no key it falls back to the demo brain, so this default is a safe no-op
# locally. Set FIRSTBACK_PROVIDER=minimax to use MiniMax instead.
PROVIDER = os.environ.get("FIRSTBACK_PROVIDER", "claude")

# MiniMax — OpenAI-compatible chat completions. Set MINIMAX_API_KEY to turn on.
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.5")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io")

# Claude — for the public launch. Set ANTHROPIC_API_KEY and FIRSTBACK_PROVIDER=claude.
# Model-defaults region (Agent B — Phase 1 cost spine):
#   CLAUDE_MODEL      — SMS / booking / Vic-conversational brain: Sonnet (fast + cheap)
#   CLAUDE_MODEL_VOICE — voice-relay turns (very short): Haiku (cheapest)
# Each model has an env override so you can hot-swap without a code change.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MODEL_VOICE = os.environ.get("CLAUDE_MODEL_VOICE", "claude-haiku-4-5")
# Per-tenant dollar daily spending cap on the AI-reply path. Past this the bot
# degrades honestly ("resting" message) rather than silently breaking. Zero = no cap.
try:
    CLAUDE_DAILY_COST_CAP_USD = float(
        os.environ.get("FIRSTBACK_DAILY_COST_CAP", "") or "1.00")
except (TypeError, ValueError):
    CLAUDE_DAILY_COST_CAP_USD = 1.00

# --- Call screening (the "phone screen") ----------------------------------
# FirstBack texts back every missed caller. Two callers should NOT get that bot
# text: known/saved people (handled by you personally) and spam/robocallers. The
# screen is TIERED and PRECISION-FIRST: it only hard-suppresses on near-certainty
# (a real homeowner silenced by mistake is the one failure the product exists to
# prevent), so anything ambiguous is still engaged, just flagged "for review".
# Tier 0/0.5/1 (identity, auto-derived known-set, free hot-path signals) and the
# crowdsourced cross-tenant ledger are ALWAYS on. The paid reputation tier and the
# AI content screen are gated and OFF until configured.
# Rollout mode for the screen, so it can be cut over SAFELY:
#   "off"     -> no screening; every missed caller gets the text-back (the pre-screen
#                behavior). The instant rollback.
#   "monitor" -> COMPUTE + log each verdict but still text everyone. Lets the owner
#                watch the "would-have-screened" numbers before it can silence anyone.
#                The safe default for a fresh cutover.
#   "enforce" -> the verdict is acted on (spam/known callers are not texted).
# FIRSTBACK_SCREENING is still honored as a legacy off-switch (=0 forces "off").
_SCREEN_MODE_RAW = os.environ.get("FIRSTBACK_SCREEN_MODE", "monitor").strip().lower()
if os.environ.get("FIRSTBACK_SCREENING", "1").strip().lower() not in ("1", "true", "yes", "on"):
    _SCREEN_MODE_RAW = "off"
SCREEN_MODE = _SCREEN_MODE_RAW if _SCREEN_MODE_RAW in ("off", "monitor", "enforce") else "monitor"
# Back-compat: truthy whenever the screen runs at all (monitor or enforce).
SCREENING_ENABLED = SCREEN_MODE != "off"


def _int_env(key, default):
    try:
        return int(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return default


# Spam score (0-100) thresholds. >= HARD -> screened (no text). MID..HARD -> engage
# but flag for review. < MID -> a clean prospect, engaged normally.
SCREEN_SCORE_HARD = _int_env("FIRSTBACK_SCREEN_HARD", 80)
SCREEN_SCORE_MID = _int_env("FIRSTBACK_SCREEN_MID", 45)
# How many DISTINCT other businesses must have flagged a number as spam before the
# crowdsourced cross-tenant signal counts (privacy-safe: only a COUNT is ever read).
SCREEN_CROWD_MIN = _int_env("FIRSTBACK_SCREEN_CROWD_MIN", 2)

# Screening graduation (Phase 5c): after the owner watches monitor mode for this
# many days with at least this many would-have-blocked verdicts (and no rescues
# that reset the clock), the system auto-promotes to enforce and alerts the owner.
SCREEN_GRADUATION_DAYS = 7
SCREEN_GRADUATION_MIN_VERDICTS = 10

# Per-tenant sensitivity presets: (hard, mid) thresholds for the screening score.
# The UI settings radio maps to these; a NULL per-tenant override inherits config
# defaults (SCREEN_SCORE_HARD / SCREEN_SCORE_MID).
SCREEN_SENSITIVITY_PRESETS = {
    "conservative": (90, 55),
    "balanced":     (80, 45),
    "aggressive":   (65, 35),
}

# AI content screen (Tier 3): classify the caller's FIRST reply (real homeowner vs.
# sales pitch / survey / wrong number) and bail mid-conversation on spam. Uses the
# same brain as the conversation engine; OFF unless explicitly enabled AND a real
# provider key is present (the demo brain always returns "prospect" -> fail open).
SCREEN_AI_CONTENT = os.environ.get("FIRSTBACK_SCREEN_AI", "").strip().lower() in ("1", "true", "yes", "on")

# --- Number reputation (optional paid robocall lookup) --------------------
# Tier 2 of the screen: a per-number spam/line-type lookup, consulted ONLY for
# unknown callers the free tiers can't clear, cached per number, with a tight
# timeout and FAIL-OPEN (any error -> treat as clean, never silence a real caller).
# Off until a provider is chosen:
#   "off"            -> never call out (default; the free tiers still screen spam)
#   "twilio_nomorobo"-> Twilio Lookup v2 line-type + the Nomorobo Spam Score add-on
#                       (reuses TWILIO_ACCOUNT_SID/AUTH_TOKEN; no new account)
#   "hiya"           -> Hiya number-reputation API (needs HIYA_API_KEY)
REPUTATION_PROVIDER = os.environ.get("FIRSTBACK_REPUTATION_PROVIDER", "off").strip().lower()
HIYA_API_KEY = os.environ.get("HIYA_API_KEY", "")
HIYA_BASE_URL = os.environ.get("HIYA_BASE_URL", "https://api.hiya.com")
# How long a cached reputation row stays fresh (hours). Reputation is sticky, so a
# day keeps cost/latency down without going stale.
try:
    REPUTATION_TTL_HOURS = float(os.environ.get("FIRSTBACK_REPUTATION_TTL_HOURS", "") or 24)
except (TypeError, ValueError):
    REPUTATION_TTL_HOURS = 24.0
# Hard ceiling on the outbound lookup so the Twilio voice webhook always answers fast.
try:
    REPUTATION_TIMEOUT_SECONDS = float(os.environ.get("FIRSTBACK_REPUTATION_TIMEOUT", "") or 2.5)
except (TypeError, ValueError):
    REPUTATION_TIMEOUT_SECONDS = 2.5


# Phase 3 — Google Places API (optional; gated — /api/places/lookup returns {} when unset).
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

# Phase 3 — Micro-site domain for A2P opt-in landing pages. <slug>.<MICRO_SITE_DOMAIN>
# is the brand opt-in URL submitted to TCR. The /c/<slug> Flask route works for tests today;
# full subdomain routing requires OWNER-OPS: *.firstback.io wildcard DNS + Cloudflare routing.
MICRO_SITE_DOMAIN = os.environ.get("MICRO_SITE_DOMAIN", "firstback.io")

# Phase 3 — Catch-all email domain for A2P contact emails. Requires OWNER-OPS:
# Cloudflare Email Routing catch-all @clients.firstback.com -> forward address.
CLIENTS_EMAIL_DOMAIN = os.environ.get("CLIENTS_EMAIL_DOMAIN", "clients.firstback.com")

# Phase 3 — Auto-flush safety: max age (hours) for a blocked send to be replayed.
# A "we'll text you right back" that lands days later is incoherent; default 6h.
try:
    FLUSH_MAX_AGE_HOURS = float(os.environ.get("FIRSTBACK_FLUSH_MAX_AGE_HOURS", "") or 6)
except (TypeError, ValueError):
    FLUSH_MAX_AGE_HOURS = 6.0

# --- Google Calendar (optional real two-way sync) -------------------------
# To turn on: in Google Cloud Console create an OAuth 2.0 Client ID of type
# "Web application", add the redirect URI below to its "Authorized redirect
# URIs", then set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (in .env). Until both
# are set, the Google card shows "Coming soon" and nothing ever calls Google.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "http://127.0.0.1:8800/api/calendar/google/callback")
# Google Contacts import (the People API) is a SEPARATE OAuth connection from the
# calendar, so it has its own redirect URI (add this one to the same OAuth client's
# "Authorized redirect URIs" too). Same GOOGLE_CLIENT_ID/SECRET power both.
GOOGLE_CONTACTS_REDIRECT_URI = os.environ.get(
    "GOOGLE_CONTACTS_REDIRECT_URI",
    "http://127.0.0.1:8800/api/contacts/google/callback")

# --- Twilio (optional real SMS / voice) -----------------------------------
# Powers real outbound texts (reminders, owner alerts) and, later, inbound SMS
# and missed-call handling. Until the account SID and auth token are set,
# messaging.configured() is False: messages run in SIMULATED mode (recorded on the
# lead's thread, shown in the simulator) and nothing ever calls Twilio. A real send
# also needs a from-number -- the app-wide TWILIO_FROM_NUMBER, or a business's own
# provisioned number -- otherwise it stays simulated. TWILIO_FROM_NUMBER must be an
# E.164 number Twilio owns (e.g. +15551234567).
# Phase 3 — A2P Trust Hub write API. The Trust Hub product SID is required before
# any brand/campaign creation call is made (prevents accidental real, billable submissions).
# TWILIO_A2P_RESELLER_SID is optional: included in campaigns only when set (ISV reseller path).
TWILIO_TRUST_PRODUCT_SID = os.environ.get("TWILIO_TRUST_PRODUCT_SID", "")
TWILIO_A2P_RESELLER_SID = os.environ.get("TWILIO_A2P_RESELLER_SID", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
# Platform number used for OWNER alerts (separate from the tenant's A2P customer-facing number).
# When set, owner-alert SMS sends FROM this number so they don't depend on any tenant's A2P approval.
# Falls back to the tenant's own from-number when unset (original behavior).
ALERT_FROM_NUMBER = os.environ.get("ALERT_FROM_NUMBER", "")
# Public base URL where Twilio can reach this app's webhooks (an ngrok https URL
# in dev, your real domain in prod). Used when provisioning a number's Voice/SMS
# webhooks; leave empty until you have a public URL.
PUBLIC_BASE_URL = os.environ.get("FIRSTBACK_PUBLIC_URL", "")

# --- Voice callback (AI voice agent via Twilio ConversationRelay) ----------
# The AI voice callback runs as a SEPARATE async service (voice_service.py) because
# Flask/WSGI cannot host the ConversationRelay WebSocket. VOICE_PUBLIC_URL is that
# service's public https base (its wss URL is the same with https -> wss). Empty
# disables the voice leg, so an SMS "call me" simply continues by text.
# CONVERSATIONRELAY_VOICE optionally overrides the TTS voice id.
VOICE_PUBLIC_URL = os.environ.get("FIRSTBACK_VOICE_URL", "")
try:
    VOICE_SERVICE_PORT = int(os.environ.get("FIRSTBACK_VOICE_PORT", "8810") or "8810")
except ValueError:
    VOICE_SERVICE_PORT = 8810
CONVERSATIONRELAY_VOICE = os.environ.get("FIRSTBACK_VOICE_TTS", "")

# Voice metering constants (Slice 2 / Slice 3 / Slice 4 cost enforcement).
# VOICE_MONTHLY_CAP_CENTS: maximum voice spend per business per calendar month
#   before calls are halted and the owner is alerted. Default = $20 (2000 cents).
# VOICE_CREDIT_RATE_CENTS: cost per 30-second billing block. Default = 25 cents.
# Both are env-overridable so the owner can adjust without a code deploy.
try:
    VOICE_MONTHLY_CAP_CENTS = int(
        os.environ.get("FIRSTBACK_VOICE_MONTHLY_CAP_CENTS", "") or "2000")
except (TypeError, ValueError):
    VOICE_MONTHLY_CAP_CENTS = 2000
try:
    VOICE_CREDIT_RATE_CENTS = int(
        os.environ.get("FIRSTBACK_VOICE_CREDIT_RATE_CENTS", "") or "25")
except (TypeError, ValueError):
    VOICE_CREDIT_RATE_CENTS = 25

# The voice service runs as a SEPARATE process/Render service and cannot share the
# web app's SQLite disk, so it relays each spoken turn to the web app's
# /internal/voice/turn endpoint rather than writing the DB directly — keeping booking
# writes single-writer. WEB_INTERNAL_URL is the web app's base URL (set ON the voice
# service); INTERNAL_SECRET is the shared secret both sides check (constant-time).
# When WEB_INTERNAL_URL is empty (local/tests), the voice service runs the shared
# engine in-process instead, so nothing extra is needed to develop or test.
WEB_INTERNAL_URL = os.environ.get("FIRSTBACK_WEB_URL", "")
INTERNAL_SECRET = os.environ.get("FIRSTBACK_INTERNAL_SECRET", "")

# --- Email / SMTP (optional — owner alerts by email) ----------------------
# Lets FirstBack email the owner when a lead arrives or an estimate books. Until
# SMTP_HOST and SMTP_FROM are set, mail.configured() is False and email alerts
# are skipped (SMS + in-app still work). For Gmail: host smtp.gmail.com, port
# 587, and use an App Password as SMTP_PASS.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
try:
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
except ValueError:
    SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "1").strip().lower() in ("1", "true", "yes", "on")

# --- Runtime --------------------------------------------------------------
# The Werkzeug interactive debugger is a remote-code-execution risk, so it stays
# OFF unless you explicitly opt in for local debugging (FIRSTBACK_DEBUG=1).
DEBUG = os.environ.get("FIRSTBACK_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

# Signs the login session cookie. MUST be set to a long random value in
# production (FIRSTBACK_SECRET); the fallback is for local dev only.
_SECRET_KEY_DEFAULT = "dev-insecure-secret-change-me"
SECRET_KEY = os.environ.get("FIRSTBACK_SECRET", _SECRET_KEY_DEFAULT)

# Phase 1 C: fail-fast if the insecure default key is used in production.
# "Production" = FIRSTBACK_HTTPS=1 (meaning we're behind TLS and the Secure cookie
# flag is on) OR FIRSTBACK_ENV=production.  In those modes an insecure default key
# means session cookies can be forged — hard fail so this ships correctly or not at all.
_is_prod = (
    os.environ.get("FIRSTBACK_HTTPS", "").strip().lower() in ("1", "true", "yes", "on")
    or os.environ.get("FIRSTBACK_ENV", "").strip().lower() == "production"
)
if _is_prod and SECRET_KEY == _SECRET_KEY_DEFAULT:
    raise RuntimeError(
        "CRITICAL: FIRSTBACK_SECRET is not set (using the insecure default). "
        "Set a long random value in your environment before deploying."
    )

# Encrypts stored OAuth tokens (Google access/refresh) at rest in SQLite. A single
# symmetric key; any non-empty string works (it's run through HKDF, see
# token_crypto.py). When UNSET, encryption is a safe no-op so local dev keeps
# working and existing plaintext rows still read. Set it in production to protect
# the refresh tokens in the database file. Rotating it makes existing encrypted
# tokens unreadable -- affected businesses simply reconnect (see SETUP_NEEDED.md).
TOKEN_ENC_KEY = os.environ.get("FIRSTBACK_TOKEN_KEY", "").strip()

# Cookie hardening. SameSite=Lax (applied in app.py) keeps the session cookie off
# cross-site POSTs (CSRF). Secure = HTTPS-only; it stays OFF so local http dev and
# the preview still work, and you turn it on in production (behind TLS) by setting
# FIRSTBACK_HTTPS=1 so the session cookie is never sent over plain http.
SESSION_COOKIE_SECURE = os.environ.get("FIRSTBACK_HTTPS", "").strip().lower() in ("1", "true", "yes", "on")

# The single timezone the whole app reasons in. Timestamps are STORED in UTC, but
# every date/time the user sees (calendar, slots, clocks) is rendered in this
# zone, so dates never drift by a day. Set FIRSTBACK_TZ to an IANA name like
# "America/New_York"; empty falls back to the server's local zone.
# (A per-business timezone for true multi-tenant is a later feature.)
TIMEZONE = os.environ.get("FIRSTBACK_TZ", "").strip()


def app_tz():
    """Resolve TIMEZONE to a tzinfo (cached-free; cheap). Falls back to the
    server's local zone if FIRSTBACK_TZ is unset or invalid."""
    if TIMEZONE:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(TIMEZONE)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


# NPA (area-code) to IANA timezone mapping for ~50 US area codes covering all
# six mainland + territory zones. Used by biz_tz() as a fallback when a
# business has no explicit timezone stored but its Twilio number area code is
# known. Covers the most common painting/contractor markets; not exhaustive.
NPA_TO_IANA = {
    # Eastern (UTC-5/-4 DST)
    "201": "America/New_York", "202": "America/New_York", "203": "America/New_York",
    "212": "America/New_York", "215": "America/New_York", "301": "America/New_York",
    "302": "America/New_York", "305": "America/New_York", "404": "America/New_York",
    "407": "America/New_York", "412": "America/New_York", "413": "America/New_York",
    "414": "America/Chicago",  # Milwaukee — Central, not Eastern
    "470": "America/New_York", "478": "America/New_York", "508": "America/New_York",
    "516": "America/New_York", "617": "America/New_York", "703": "America/New_York",
    "704": "America/New_York", "718": "America/New_York", "813": "America/New_York",
    "914": "America/New_York", "954": "America/New_York",
    # Central (UTC-6/-5 DST)
    "214": "America/Chicago",  "224": "America/Chicago",  "312": "America/Chicago",
    "314": "America/Chicago",  "346": "America/Chicago",  "469": "America/Chicago",
    "512": "America/Chicago",  "630": "America/Chicago",  "713": "America/Chicago",
    "773": "America/Chicago",  "815": "America/Chicago",  "901": "America/Chicago",
    "936": "America/Chicago",
    # Mountain (UTC-7/-6 DST)
    "303": "America/Denver",   "480": "America/Phoenix",  "520": "America/Phoenix",
    "602": "America/Phoenix",  "623": "America/Phoenix",  "720": "America/Denver",
    "801": "America/Denver",   "970": "America/Denver",
    # Pacific (UTC-8/-7 DST)
    "206": "America/Los_Angeles", "213": "America/Los_Angeles",
    "408": "America/Los_Angeles", "415": "America/Los_Angeles",
    "503": "America/Los_Angeles", "619": "America/Los_Angeles",
    "626": "America/Los_Angeles", "650": "America/Los_Angeles",
    "702": "America/Los_Angeles", "714": "America/Los_Angeles",
    "818": "America/Los_Angeles", "916": "America/Los_Angeles",
    "949": "America/Los_Angeles",
    # Alaska (UTC-9/-8 DST)
    "907": "America/Anchorage",
    # Hawaii (UTC-10, no DST)
    "808": "America/Honolulu",
}


def biz_tz(business):
    """Return a tzinfo for a business. `business` may be a dict (the hot path:
    reads business['timezone'] with NO db hit) or an int id (does a lazy db
    lookup). Resolution order:

      1. business['timezone'] / db row timezone column (valid IANA name) -> ZoneInfo
      2. NPA of business['twilio_number'] (then business['phone']) -> NPA_TO_IANA -> ZoneInfo
      3. app_tz() global fallback

    Never raises; always returns a tzinfo.
    """
    from zoneinfo import ZoneInfo
    if isinstance(business, int):
        import db as _db
        business = _db.get_business(business)
    if not isinstance(business, dict):
        return app_tz()
    # 1. Stored IANA name
    tz_name = (business.get("timezone") or "").strip()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass  # fall through to NPA
    # 2. NPA fallback. Prefer the provisioned Twilio number (usually a local NPA);
    #    fall back to the contractor's own phone, the better region signal before a
    #    number is provisioned.
    import re as _re
    for number in (business.get("twilio_number"), business.get("phone")):
        digits = _re.sub(r"\D", "", (number or "").strip())
        # US numbers: +1NXXNXXXXXX -> NPA is digits[1:4] (after leading 1)
        npa = None
        if len(digits) == 11 and digits.startswith("1"):
            npa = digits[1:4]
        elif len(digits) == 10:
            npa = digits[:3]
        if npa and npa in NPA_TO_IANA:
            try:
                return ZoneInfo(NPA_TO_IANA[npa])
            except Exception:
                pass
    # 3. Global fallback
    return app_tz()


def sms_status_callback_url():
    """Return the full URL for Twilio's SMS status callback webhook, or '' if
    FIRSTBACK_PUBLIC_URL is not set. Callers should treat '' as 'no callback'
    (don't pass it to Twilio)."""
    base = PUBLIC_BASE_URL.rstrip("/") if PUBLIC_BASE_URL else ""
    if not base:
        return ""
    return base + "/webhooks/twilio/sms/status"

# Starter owner login seeded for "client zero" (business 1) so the existing demo
# data is reachable immediately. Change the password after first login.
# Phase 1 C: the insecure "firstback123" default is replaced. In production,
# FIRSTBACK_OWNER_PASSWORD MUST be set explicitly — the prod fail-fast (above) ensures
# the server won't start with the dev default key, which makes a known seed password
# equally dangerous. In dev/local the dev-only default below is intentionally different
# from "firstback123" and labeled as dev-only so it's never confused with a real credential.
_SEED_PW_DEV_DEFAULT = "dev-change-me-not-for-prod"
SEED_OWNER_EMAIL = os.environ.get("FIRSTBACK_OWNER_EMAIL", "heritagehousepainting@gmail.com")
SEED_OWNER_PASSWORD = os.environ.get("FIRSTBACK_OWNER_PASSWORD", _SEED_PW_DEV_DEFAULT)
if _is_prod and SEED_OWNER_PASSWORD == _SEED_PW_DEV_DEFAULT:
    raise RuntimeError(
        "CRITICAL: FIRSTBACK_OWNER_PASSWORD is using the dev default. "
        "Set a strong password in your environment before deploying."
    )

# --- Storage --------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
# Defaults to a file beside the code. On a host (e.g. Render) point FIRSTBACK_DB_PATH
# at a PERSISTENT disk (e.g. /var/data/firstback.db) so leads/bookings survive a
# redeploy; without it the database resets on every deploy.
# FIRSTBACK_DB_PATH is the durable at-rest location (e.g. Render's network-attached /var/data).
_db_at_rest = os.environ.get("FIRSTBACK_DB_PATH", "").strip() or str(BASE_DIR / "firstback.db")
# Durable local-disk mode (the fix for the network-FS boot hang): when FIRSTBACK_DB_LOCAL_MIRROR
# is truthy, SQLite runs on a fast LOCAL disk (FIRSTBACK_DB_LOCAL_PATH, default /tmp/firstback.db)
# where it never hangs, and the at-rest path becomes the durable backup we snapshot to on a timer
# + at shutdown and restore from on boot. Only plain file copies ever touch the (network) at-rest
# disk -- never a SQLite open. A build that doesn't know this flag just keeps using
# FIRSTBACK_DB_PATH, so flipping it on/off is a safe no-op for old code (no dangerous transition).
# See db.py restore_from_backup_if_needed / backup_to_durable + [[reference-firstback-wal-boot-hazard]].
if os.environ.get("FIRSTBACK_DB_LOCAL_MIRROR", "").strip().lower() in ("1", "true", "yes", "on"):
    DB_BACKUP_PATH = _db_at_rest
    DB_PATH = os.environ.get("FIRSTBACK_DB_LOCAL_PATH", "").strip() or "/tmp/firstback.db"
else:
    DB_PATH = _db_at_rest
    DB_BACKUP_PATH = os.environ.get("FIRSTBACK_DB_BACKUP_PATH", "").strip()

# --- ROI / pricing -------------------------------------------------------
# Monthly subscription cost used for the roi_multiple calculation in db.analytics
# and roi.check_roi_milestone. Callers: A (analytics/roi), B (digest).
PLAN_COST_MONTHLY = 99

# Industry-average job values by trade, used when the owner has not set their own
# avg_job_value. Keyed on the trade strings the app uses (set at signup / default
# business profile). The $800 floor is the fallback for an unrecognised trade.
# Source: contractor industry benchmarks (2024-2025 national averages). These are
# ESTIMATES for internal ROI display only — never present as the owner's actual data.
TRADE_JOB_VALUE_DEFAULTS = {
    "plumbing":          1800,
    "electrical":        1500,
    "hvac":              2200,
    "roofing":           8500,
    "painting":          3200,
    "residential & commercial painting": 3200,
    "landscaping":       1200,
    "general":            900,
    "home services":      900,
    "carpentry":         1600,
    "flooring":          2800,
    "remodeling":        6000,
    "concrete":          3000,
    "drywall":           1400,
    "insulation":        1800,
    "gutters":           1200,
    "windows":           2500,
    "fencing":           2800,
    "decking":           5000,
    "pest control":       450,
    "cleaning":           400,
}

# --- Scheduling -----------------------------------------------------------
# The estimate windows FirstBack offers on each OPEN day. The in-house calendar
# fills open days with these times; the AI offers the soonest two. (Later these
# can come from a connected Google/Outlook/Apple calendar.)
ESTIMATE_TIMES = ["9:00 AM", "2:00 PM"]
# How far ahead the AI may offer / the calendar treats as bookable.
BOOKING_HORIZON_DAYS = 21
# Defaults a tenant inherits until they customize their scheduling (db.scheduling_prefs).
# Weekday ints, Mon=0..Sun=6. Default Mon-Sat (matches the default "hours" string).
DEFAULT_WORKING_DAYS = [0, 1, 2, 3, 4, 5]
# Minimum minutes between two booked estimates. 0 = no buffer (original behavior); the
# owner can raise it so the AI never books two estimates too close to make both.
DEFAULT_BUFFER_MINUTES = 0

# --- Reminders & follow-ups (Feature 1) -----------------------------------
# A background ticker (started in app.py) texts a reminder before each booked
# estimate and one gentle nudge to a warm lead that went cold. All hours are
# business-local (see FIRSTBACK_TZ); texts only go out within quiet hours.
def _num_env(key, default, cast=float):
    try:
        return cast(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return default

REMINDER_LEAD_HOURS = _num_env("REMINDER_LEAD_HOURS", 24, float)  # hours before the estimate
FOLLOWUP_IDLE_HOURS = _num_env("FOLLOWUP_IDLE_HOURS", 24, float)  # idle before nudging a cold lead
TICK_SECONDS = _num_env("TICK_SECONDS", 60, int)                 # scheduler wake interval
QUIET_START = _num_env("QUIET_START", 8, int)                    # business-local hour; no texts before
QUIET_END = _num_env("QUIET_END", 21, int)                      # business-local hour; no texts at/after
# Optional shared secret protecting POST /tasks/run-due, so an external cron can
# drive the scheduler in production (where an in-process ticker would die with the
# process). Unset => the endpoint is disabled (always 403).
TASKS_SECRET = os.environ.get("FIRSTBACK_TASKS_SECRET", "")

# --- Operator (concierge admin) allowlist -------------------------------------
# A2P brand/campaign SIDs are recorded by the OPERATOR (us), never the contractor,
# who must not be able to flip their own a2p_status to "approved" on the shared
# Twilio account. Identified by login email; comma-separated, case-insensitive.
# Empty => no operator exists and the record action is closed to everyone.
OPERATOR_EMAILS = frozenset(
    e.strip().lower()
    for e in os.environ.get("FIRSTBACK_OPERATOR_EMAILS", "").split(",")
    if e.strip()
)

# --- Default business profile --------------------------------------------
# This is "client zero" — your own painting company. Edit it in Settings;
# these are just the seed values the database starts with.
DEFAULT_BUSINESS = {
    "name": "Heritage House Painting",
    "trade": "Residential & commercial painting",
    "service_area": "Greater metro area (30-mile radius)",
    "hours": "Mon-Sat, 7am-6pm",
    "owner_name": "Jonathan",
    # The business's FirstBack texting number (shown in the simulator header).
    "phone": "(555) 314-2270",
    # The AI uses this to sound like YOUR business and to know what to ask.
    # >>> THIS IS WHERE YOU ADD YOUR LOGIC AND FLOW <<<
    "ai_instructions": (
        "You are the assistant for Heritage House Painting, replying by text to a "
        "caller we just missed. Use a professional, clear, and courteous tone with "
        "complete sentences and correct grammar. Be personable but not casual: no "
        "slang, no filler, and no emoji. Keep each reply concise, about one to three "
        "sentences. First find out what they would like painted. Next, ask for their "
        "address so we can confirm they are within our service area. Then offer two of "
        "our available estimate windows and book the one they choose. Never quote prices "
        "or give a dollar range; let them know we will provide a quote at the free "
        "in-person estimate."
    ),
    # NOTE: the AI's availability now comes from the in-house calendar
    # (db.upcoming_slots over ESTIMATE_TIMES, skipping busy/taken days). The old
    # free-text `available_slots` seed was retired; the DB column is kept only for
    # backward compatibility and is no longer read or seeded.
}
