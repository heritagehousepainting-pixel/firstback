"""RingBack — central configuration.

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
APP_NAME = "RingBack"
TAGLINE = "Never lose another job to a missed call."

# --- AI brain / provider --------------------------------------------------
# Which brain answers leads. Pick the provider here:
#   "minimax" -> MiniMax (what we run today)
#   "claude"  -> Anthropic Claude (what we switch to for the public launch)
#   "demo"    -> built-in rule-based script (zero setup, but no real understanding)
# If the chosen provider has no API key, RingBack safely falls back to the demo
# brain so the app always runs.
PROVIDER = os.environ.get("RINGBACK_PROVIDER", "minimax")

# MiniMax — OpenAI-compatible chat completions. Set MINIMAX_API_KEY to turn on.
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2.5")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io")

# Claude — for the public launch. Set ANTHROPIC_API_KEY and RINGBACK_PROVIDER=claude.
# For high-volume SMS a faster/cheaper model (claude-sonnet-4-6 / claude-haiku-4-5)
# may beat Opus on cost and latency.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

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
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
# Public base URL where Twilio can reach this app's webhooks (an ngrok https URL
# in dev, your real domain in prod). Used when provisioning a number's Voice/SMS
# webhooks; leave empty until you have a public URL.
PUBLIC_BASE_URL = os.environ.get("RINGBACK_PUBLIC_URL", "")

# --- Voice callback (AI voice agent via Twilio ConversationRelay) ----------
# The AI voice callback runs as a SEPARATE async service (voice_service.py) because
# Flask/WSGI cannot host the ConversationRelay WebSocket. VOICE_PUBLIC_URL is that
# service's public https base (its wss URL is the same with https -> wss). Empty
# disables the voice leg, so an SMS "call me" simply continues by text.
# CONVERSATIONRELAY_VOICE optionally overrides the TTS voice id.
VOICE_PUBLIC_URL = os.environ.get("RINGBACK_VOICE_URL", "")
try:
    VOICE_SERVICE_PORT = int(os.environ.get("RINGBACK_VOICE_PORT", "8810") or "8810")
except ValueError:
    VOICE_SERVICE_PORT = 8810
CONVERSATIONRELAY_VOICE = os.environ.get("RINGBACK_VOICE_TTS", "")

# The voice service runs as a SEPARATE process/Render service and cannot share the
# web app's SQLite disk, so it relays each spoken turn to the web app's
# /internal/voice/turn endpoint rather than writing the DB directly — keeping booking
# writes single-writer. WEB_INTERNAL_URL is the web app's base URL (set ON the voice
# service); INTERNAL_SECRET is the shared secret both sides check (constant-time).
# When WEB_INTERNAL_URL is empty (local/tests), the voice service runs the shared
# engine in-process instead, so nothing extra is needed to develop or test.
WEB_INTERNAL_URL = os.environ.get("RINGBACK_WEB_URL", "")
INTERNAL_SECRET = os.environ.get("RINGBACK_INTERNAL_SECRET", "")

# --- Email / SMTP (optional — owner alerts by email) ----------------------
# Lets RingBack email the owner when a lead arrives or an estimate books. Until
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
# OFF unless you explicitly opt in for local debugging (RINGBACK_DEBUG=1).
DEBUG = os.environ.get("RINGBACK_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

# Signs the login session cookie. MUST be set to a long random value in
# production (RINGBACK_SECRET); the fallback is for local dev only.
SECRET_KEY = os.environ.get("RINGBACK_SECRET", "dev-insecure-secret-change-me")

# Cookie hardening. SameSite=Lax (applied in app.py) keeps the session cookie off
# cross-site POSTs (CSRF). Secure = HTTPS-only; it stays OFF so local http dev and
# the preview still work, and you turn it on in production (behind TLS) by setting
# RINGBACK_HTTPS=1 so the session cookie is never sent over plain http.
SESSION_COOKIE_SECURE = os.environ.get("RINGBACK_HTTPS", "").strip().lower() in ("1", "true", "yes", "on")

# The single timezone the whole app reasons in. Timestamps are STORED in UTC, but
# every date/time the user sees (calendar, slots, clocks) is rendered in this
# zone, so dates never drift by a day. Set RINGBACK_TZ to an IANA name like
# "America/New_York"; empty falls back to the server's local zone.
# (A per-business timezone for true multi-tenant is a later feature.)
TIMEZONE = os.environ.get("RINGBACK_TZ", "").strip()


def app_tz():
    """Resolve TIMEZONE to a tzinfo (cached-free; cheap). Falls back to the
    server's local zone if RINGBACK_TZ is unset or invalid."""
    if TIMEZONE:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(TIMEZONE)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo

# Starter owner login seeded for "client zero" (business 1) so the existing demo
# data is reachable immediately. Change the password after first login.
SEED_OWNER_EMAIL = os.environ.get("RINGBACK_OWNER_EMAIL", "heritagehousepainting@gmail.com")
SEED_OWNER_PASSWORD = os.environ.get("RINGBACK_OWNER_PASSWORD", "ringback123")

# --- Storage --------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
# Defaults to a file beside the code. On a host (e.g. Render) point RINGBACK_DB_PATH
# at a PERSISTENT disk (e.g. /var/data/ringback.db) so leads/bookings survive a
# redeploy; without it the database resets on every deploy.
DB_PATH = os.environ.get("RINGBACK_DB_PATH", "").strip() or (BASE_DIR / "ringback.db")

# --- Scheduling -----------------------------------------------------------
# The estimate windows RingBack offers on each OPEN day. The in-house calendar
# fills open days with these times; the AI offers the soonest two. (Later these
# can come from a connected Google/Outlook/Apple calendar.)
ESTIMATE_TIMES = ["9:00 AM", "2:00 PM"]
# How far ahead the AI may offer / the calendar treats as bookable.
BOOKING_HORIZON_DAYS = 21

# --- Reminders & follow-ups (Feature 1) -----------------------------------
# A background ticker (started in app.py) texts a reminder before each booked
# estimate and one gentle nudge to a warm lead that went cold. All hours are
# business-local (see RINGBACK_TZ); texts only go out within quiet hours.
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
TASKS_SECRET = os.environ.get("RINGBACK_TASKS_SECRET", "")

# --- Default business profile --------------------------------------------
# This is "client zero" — your own painting company. Edit it in Settings;
# these are just the seed values the database starts with.
DEFAULT_BUSINESS = {
    "name": "Heritage House Painting",
    "trade": "Residential & commercial painting",
    "service_area": "Greater metro area (30-mile radius)",
    "hours": "Mon-Sat, 7am-6pm",
    "owner_name": "Jonathan",
    # The business's RingBack texting number (shown in the simulator header).
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
