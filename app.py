"""FirstBack -- Flask app.

Run:
    python app.py
Then open http://localhost:8800
"""
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone, date
from functools import wraps
from urllib.parse import quote

from flask import (Flask, render_template, request, redirect, jsonify, session,
                   url_for, abort, Response, stream_with_context)
from werkzeug.security import generate_password_hash, check_password_hash

import json

import config
import db
import ai
import assistant
import convos
import google_cal
import outlook_cal
import messaging
import mail
import alerts
import reminders
import compliance
import consent
import connections
import triage
import reputation
import contact_import
import google_contacts
import growth
import jobber_fsm
import hcp_fsm
import fsm_sync
from config import (APP_NAME, TAGLINE, DEBUG, SECRET_KEY, TASKS_SECRET,
                    SESSION_COOKIE_SECURE, SEED_OWNER_EMAIL, SEED_OWNER_PASSWORD,
                    app_tz, VOICE_PUBLIC_URL, INTERNAL_SECRET,
                    SCREEN_MODE, SCREEN_AI_CONTENT, SCREEN_SCORE_MID, SCREEN_SCORE_HARD)

app = Flask(__name__)
app.secret_key = SECRET_KEY
# Reload edited templates without a restart, even though the debugger is off by
# default (keeps the dev workflow; negligible overhead for this app).
app.config["TEMPLATES_AUTO_RELOAD"] = True
# Cookie hardening: HttpOnly (Flask default) + SameSite=Lax stop the session cookie
# from riding cross-site POSTs (CSRF on /settings, /login, etc.). Secure (HTTPS-only)
# is gated on FIRSTBACK_HTTPS so local http dev / the preview keep working.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE
# Phase 6a D-4: bound request bodies (Flask default is UNBOUNDED). The cap must clear the
# largest legitimate request: /api/contacts/import accepts a 5 MB vCard/CSV (see
# _MAX_IMPORT_BYTES) which it size-checks itself with a friendly message — so the global
# ceiling sits just above that (multipart overhead) and the import route keeps its precise
# 5 MB limit. Everything else (assistant/confirm args_json, Stripe webhooks ~10 KB) is far
# smaller; the win is killing the unbounded-body abuse vector. Werkzeug returns 413 on oversize.
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
db.init_db()
db.start_backup_daemon()   # durable local-disk mode: snapshot to the network disk on a timer + at exit

# Wire the command-center memory into the assistant (no import cycle): the router consults
# taught corrections before the brain, and folds them into its routing prompt.
assistant._learning_lookup = convos.lookup
assistant._learning_examples_hook = convos.learnings_for_prompt
# When a gap recurs, let the assistant check if a real tool now fits it (proactive self-teaching).
convos._tool_suggest_hook = assistant.suggest_tool_for

# Seed an owner login for "client zero" (business 1) on first run so the existing
# demo data is reachable immediately. Change the password after first login.
# Phase 1 C: "firstback123" is gone. SEED_OWNER_PASSWORD uses a dev-only default
# in config.py that is NOT the old known password; in prod the config fail-fast
# guarantees it's set to a real value before the server starts.
if db.count_users() == 0:
    db.create_user(SEED_OWNER_EMAIL, generate_password_hash(SEED_OWNER_PASSWORD), 1)


# ---- Auth ----  (kernel: current_user/current_business/login_required/_safe_next/
# _EMAIL_RE live in auth.py — edit trades_core/auth.py, then run trades_core/sync.py)
from auth import current_user, current_business, login_required, _safe_next, _EMAIL_RE


def _is_operator(user):
    emails = getattr(config, "OPERATOR_EMAILS", frozenset())
    return bool(user) and (user.get("email") or "").strip().lower() in emails


def require_twilio_signature(view):
    """Reject webhook calls that aren't signed by Twilio (wired onto the inbound
    Twilio webhooks in Phase 1). Reconstructs the public https URL via
    X-Forwarded-Proto so validation works behind a TLS-terminating proxy / ngrok,
    the usual cause of false rejections."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        url = request.url
        proto = request.headers.get("X-Forwarded-Proto")
        if proto:
            url = url.replace("http://", proto + "://", 1)
        params = request.form.to_dict() if request.method == "POST" else {}
        sig = request.headers.get("X-Twilio-Signature", "")
        if not messaging.valid_signature(url, params, sig):
            return ("Invalid Twilio signature", 403)
        return view(*args, **kwargs)
    return wrapped


def _default_ai_instructions(name, trade):
    """Generic per-business instructions for a freshly signed-up tenant."""
    return (
        f"You are the assistant for {name}, a {trade} business, texting back a caller we "
        "just missed. Sound like a sharp, friendly person who works here, not a chatbot or "
        "a form. Your job: make the caller feel heard, confirm you can help, and get them "
        "booked for a free estimate. Warm, brief, direct. One or two sentences per text, and "
        "ask only one thing at a time. If they have already told you what they need and "
        "roughly where they are, skip those questions and go straight to offering estimate "
        "windows. Never dodge a price question: say honestly that you quote in person so the "
        "number is accurate, then offer to book the free estimate."
    )


# ---- Display formatting (the single source for §3 of the design system) ----
def fmt_phone(raw):
    """Normalize any stored phone to one format -> (555) 314-2270."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:]}"
    return str(raw or "")


def fmt_clock(iso):
    """ISO timestamp (stored UTC) -> '2:14 PM' in the app's configured timezone."""
    try:
        dt = datetime.fromisoformat(iso).astimezone(app_tz())
    except (TypeError, ValueError):
        return iso or ""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'}"


def fmt_date(iso):
    """ISO timestamp (stored UTC) -> 'Jun 13' in the app's configured timezone."""
    try:
        dt = datetime.fromisoformat(iso).astimezone(app_tz())
    except (TypeError, ValueError):
        return iso or ""
    return f"{dt.strftime('%b')} {dt.day}"


def fmt_slot_when(day_iso, slot_time=None):
    """Normalize an appointment to one display shape: '2026-06-15' + '14:00' ->
    'Mon Jun 15 . 2:00 PM'. Returns '' if the day is unparseable, so callers can
    fall back to the raw scheduled_for string."""
    try:
        d = datetime.strptime(day_iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    out = f"{d.strftime('%a %b ')}{d.day}"
    try:
        hh, mm = (int(x) for x in (slot_time or "").split(":"))
        out += f" · {hh % 12 or 12}:{mm:02d} {'PM' if hh >= 12 else 'AM'}"
    except (ValueError, AttributeError):
        pass
    return out


app.jinja_env.filters["phone"] = fmt_phone
app.jinja_env.filters["clock"] = fmt_clock
app.jinja_env.filters["nicedate"] = fmt_date
app.jinja_env.filters["slotwhen"] = fmt_slot_when


@app.context_processor
def inject_globals():
    # Available in every template. `business` is the logged-in tenant when signed
    # in, else client zero (business 1) for the marketing pages.
    u = current_user()
    biz = db.get_business(u["business_id"]) if u else db.get_business(1)
    # `golive_complete` drives the sidebar: Go Live sits pinned at the top until the
    # tenant is live, then retires to the bottom with a check. Only meaningful when
    # signed in (no template renders on JSON/webhook routes, so this stays cheap).
    golive_complete = bool(u and connections.is_live(biz))
    return {"app_name": APP_NAME, "tagline": TAGLINE, "brain": ai.brain_mode(),
            "business": biz, "current_user": u, "golive_complete": golive_complete,
            "csrf_token": _csrf_token()}


# ---- Command-center hardening: CSRF, history sanitization, rate limiting ----
# Per-tenant ceiling on assistant turns per minute. SameSite=Lax already blocks cross-site
# POSTs; this caps runaway cost/abuse from an authenticated client hammering the LLM.
ASSISTANT_RPM = int(os.environ.get("FIRSTBACK_ASSISTANT_RPM", "60") or "60")
# Per-tenant DAILY ceiling on LLM-backed assistant turns. The per-minute limiter above stops
# bursts; this caps cumulative daily cost. Past it the assistant keeps working but degrades to
# the deterministic keyword floor (allow_llm=False) -- booking, lists, and the confirm gate
# all still function; only the fuzzy/chat LLM path is withheld until the window rolls over.
ASSISTANT_DAILY = int(os.environ.get("FIRSTBACK_ASSISTANT_DAILY", "400") or "400")


def _assistant_budget(biz, message):
    """Spend one assistant turn against this tenant's rate budgets. Returns
    (allow_llm, throttled): `throttled` trips the per-minute burst limiter (caller should ask
    the owner to slow down); `allow_llm` is False once the daily LLM budget is spent (caller
    degrades to the keyword floor). No-op for an empty message.

    Phase 1: the dollar daily cap (ai.is_over_daily_cap) replaces the raw turn-count
    ceiling as the primary LLM gate; the turn-count window still caps burst abuse."""
    if not message:
        return True, False
    throttled = db.incr_rate(biz["id"], "assistant", 60) > ASSISTANT_RPM
    # Dollar cap (Phase 1): gate by spend, not turn count.
    over_cap = ai.is_over_daily_cap(biz["id"])
    # Fall back to the turn-count cap when no dollar ledger data exists yet.
    turn_cap_ok = db.incr_rate(biz["id"], "assistant_daily", 86400) <= ASSISTANT_DAILY
    allow_llm = (not over_cap) and turn_cap_ok
    return allow_llm, throttled


def _next_local_midnight_iso(biz):
    """Return the next local midnight for this tenant as an ISO-8601 string (UTC offset
    included). Used by the vic_status surface to tell the owner when full power returns.
    Never raises -- falls back to UTC midnight on any error."""
    try:
        from config import biz_tz as _biz_tz
        from datetime import datetime, timedelta
        tz = _biz_tz(biz)
        now_local = datetime.now(tz)
        next_midnight = (now_local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return next_midnight.isoformat()
    except Exception:
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        next_midnight_utc = (now_utc + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return next_midnight_utc.isoformat()


def _csrf_token():
    """Get-or-create this session's CSRF token (double-submit). Rendered into the command
    center and echoed back by the JS as `_csrf`; validated on every assistant POST."""
    tok = session.get("csrf_token")
    if not tok:
        tok = secrets.token_hex(32)
        session["csrf_token"] = tok
    return tok


def _csrf_ok():
    """The request CSRF token matches the session token (constant-time).

    Form posts carry `_csrf`; JSON/multipart fetches carry the same value in
    `X-CSRF-Token`. This keeps the defense consistent across regular forms,
    URL-encoded JS helpers, JSON APIs, and file uploads.
    """
    tok = session.get("csrf_token")
    sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf", "")
    return bool(tok and sent) and secrets.compare_digest(tok, sent)


def _sanitize_history(raw):
    """Client-supplied chat history is UNTRUSTED (it feeds the LLM). Keep only well-formed
    user/assistant turns, cap each to 500 chars and the list to 12, and drop anything else
    (impersonated 'system' turns, non-strings, junk) before it reaches the brain."""
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    clean = []
    for item in data[-12:]:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant") \
                and isinstance(item.get("content"), str):
            clean.append({"role": item["role"], "content": item["content"][:500]})
    return clean


# ---- Pages ----
@app.route("/")
def landing():
    # The front door — the onboarding hero.
    return render_template("onboarding.html",
                           voice_configured=bool(VOICE_PUBLIC_URL))


@app.route("/tour")
def tour():
    # The old Field-Blue landing was retired (off-brand, placeholder testimonial,
    # no signup path); the front door "/" is the landing now. landing.html is kept
    # only as reference material and is no longer routed.
    return redirect("/")


def _landing_path():
    """Where a signed-in owner lands: the Go-Live wizard until they're live, then the
    command center. Makes /setup the first screen a new tenant sees, and retires it
    automatically once setup is complete."""
    u = current_user()
    if not u:
        return "/login"
    return "/dashboard" if connections.is_live(db.get_business(u["business_id"])) else "/setup"


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("uid"):
        return redirect(_landing_path())
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        biz_name = (request.form.get("business") or "").strip()
        owner = (request.form.get("owner") or "").strip()
        missing = []
        if not biz_name:
            missing.append("your business name")
        if not _EMAIL_RE.match(email):
            missing.append("a valid work email")
        if len(password) < 8:
            missing.append("a password of at least 8 characters")
        if missing:
            return render_template("auth.html", mode="signup",
                                   error="Please enter " + ", ".join(missing) + "."), 400
        if db.get_user_by_email(email):
            return render_template("auth.html", mode="signup",
                                   error="That email is already registered. Try logging in."), 400
        trade = (request.form.get("trade") or "home services").strip()
        bid = db.create_business({
            "name": biz_name, "owner_name": owner or biz_name, "trade": trade,
            "service_area": "Your service area", "hours": "Mon-Fri, 8am-5pm",
            "ai_instructions": _default_ai_instructions(biz_name, trade)})
        uid = db.create_user(email, generate_password_hash(password), bid)
        # Populate owner-alert prefs so SMS/email alerts are non-NULL from day 1.
        # alert_sms uses the phone field when present in the form (e.g. a future signup
        # step that collects it); alert_email defaults to the login email.
        signup_phone = (request.form.get("phone") or "").strip()
        db.update_alert_prefs(bid, {
            "alert_email": email,
            "alert_sms": signup_phone,
            "alert_on_lead": 1,
            "alert_on_booking": 1,
            "alert_on_urgent": 1,
            "alert_on_daily_digest": 1,
            "alert_on_roi_milestone": 1,
        })
        # Phase 3 SF-8: set business_type from the "Do you have an EIN?" checkbox.
        # has_ein present (any truthy value) -> "llc"; absent -> "sole_prop".
        has_ein = bool(request.form.get("has_ein"))
        db.set_business_type(bid, "llc" if has_ein else "sole_prop")
        session.clear()
        session["uid"] = uid
        return redirect("/setup")   # a brand-new tenant always starts at Go Live
    return render_template("auth.html", mode="signup")


# Phase 1 C: login rate limiting. Track failed attempts per (email, remote IP) in an
# in-memory dict with a short TTL so a credential-stuffing burst is blunted without
# a Redis dependency. A per-tenant DB table would be better but would add migration
# risk; this is the zero-dependency, good-enough approach for Phase 1.
import collections, time as _time
_LOGIN_FAILURES: dict = collections.defaultdict(list)  # key -> [timestamp, ...]
LOGIN_MAX_ATTEMPTS = int(os.environ.get("FIRSTBACK_LOGIN_MAX_ATTEMPTS", "10") or "10")
LOGIN_WINDOW_SECONDS = int(os.environ.get("FIRSTBACK_LOGIN_WINDOW", "300") or "300")  # 5 min


def _login_rate_key(email):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    return f"{email}:{ip}"


def _login_blocked(email):
    """True if this (email, IP) pair has exceeded the failure ceiling in the window."""
    key = _login_rate_key(email)
    cutoff = _time.monotonic() - LOGIN_WINDOW_SECONDS
    _LOGIN_FAILURES[key] = [t for t in _LOGIN_FAILURES[key] if t > cutoff]
    return len(_LOGIN_FAILURES[key]) >= LOGIN_MAX_ATTEMPTS


def _login_record_failure(email):
    key = _login_rate_key(email)
    _LOGIN_FAILURES[key].append(_time.monotonic())


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("uid"):
        return redirect(_landing_path())
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if _login_blocked(email):
            return render_template("auth.html", mode="login",
                                   error="Too many failed attempts. Please wait a few minutes "
                                         "and try again."), 429
        user = db.get_user_by_email(email)
        if not user or not check_password_hash(user["password_hash"], password):
            _login_record_failure(email)
            return render_template("auth.html", mode="login",
                                   error="Email or password is incorrect."), 401
        session.clear()
        session["uid"] = user["id"]
        nxt = request.args.get("next")
        return redirect(_safe_next(nxt) if nxt else _landing_path())
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# Phase 1 C: password reset routes.  Uses the Phase-0 platform email channel (mail.py).
# Token lifetime: 1 hour.  Tokens are single-use (burned on redemption).
_RESET_TOKEN_TTL_HOURS = int(os.environ.get("FIRSTBACK_RESET_TTL_HOURS", "1") or "1")


@app.route("/auth/forgot", methods=["GET", "POST"])
def auth_forgot():
    """Issue a password-reset email. Always shows the same message to prevent
    email enumeration (200 regardless of whether the address exists)."""
    sent = False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if email:
            user = db.get_user_by_email(email)
            if user:
                from datetime import timezone, timedelta
                token = secrets.token_urlsafe(32)
                expires = (datetime.now(timezone.utc)
                           + timedelta(hours=_RESET_TOKEN_TTL_HOURS)).isoformat()
                db.create_password_reset_token(user["id"], token, expires)
                base = (config.PUBLIC_BASE_URL or request.host_url.rstrip("/"))
                link = f"{base}/auth/reset?token={token}"
                subject = f"{APP_NAME} — Password reset"
                body = (
                    f"Hi,\n\nSomeone requested a password reset for {email}.\n\n"
                    f"Click the link below to set a new password (valid for "
                    f"{_RESET_TOKEN_TTL_HOURS} hour):\n\n  {link}\n\n"
                    f"If you didn't request this, you can ignore this email.\n\n"
                    f"— {APP_NAME}"
                )
                mail.send_email(email, subject, body)
        sent = True   # always show the same message (no enumeration)
    return render_template("auth.html", mode="forgot", sent=sent)


@app.route("/auth/reset", methods=["GET", "POST"])
def auth_reset():
    """Redeem a password-reset token and set a new password."""
    token = (request.args.get("token") or request.form.get("token") or "").strip()
    error = None
    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if not token:
            error = "Missing or invalid reset link."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            uid = db.consume_password_reset_token(token)
            if uid is None:
                error = "This reset link has already been used or has expired. Request a new one."
            else:
                db.update_user_password(uid, generate_password_hash(password))
                return redirect("/login?reset=1")
    elif not token:
        return redirect("/auth/forgot")
    return render_template("auth.html", mode="reset", token=token, error=error)


# ---- Marketing pages (linked from the front door) ----
@app.route("/product")
def product():
    return render_template("product.html",
                           voice_configured=bool(VOICE_PUBLIC_URL))


@app.route("/solutions")
def solutions():
    return render_template("solutions.html",
                           voice_configured=bool(VOICE_PUBLIC_URL))


@app.route("/resources")
def resources():
    return render_template("resources.html")


@app.route("/company")
def company():
    return render_template("company.html")


@app.route("/pricing")
def pricing():
    return render_template("pricing.html",
                           voice_configured=bool(VOICE_PUBLIC_URL))


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip()
        message = (request.form.get("message") or "").strip()
        if not name or not _EMAIL_RE.match(email.lower()) or not message:
            return render_template(
                "contact.html", sent=None,
                error="Please enter your name, a valid email, and a short message."), 400
        # Persist the inquiry (so it is not silently dropped); pull business/phone
        # into the message body since the table keeps a single message field.
        extra = " | ".join(p for p in (
            ("Business: " + request.form.get("business", "").strip())
            if request.form.get("business", "").strip() else "",
            ("Phone: " + request.form.get("phone", "").strip())
            if request.form.get("phone", "").strip() else "") if p)
        db.add_contact_message(name, email, (message + ("\n\n" + extra if extra else "")))
        return redirect("/contact?sent=1")
    return render_template("contact.html", sent=request.args.get("sent"))


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/c/<slug>")
def contractor_microsite(slug):
    """Public contractor micro-site for TCR opt-in URL verification.
    No FirstBack branding visible -- this page represents the contractor's identity.
    No smart/curly quotes anywhere (Jinja rendering guard).
    """
    conn = db.get_conn()
    row = conn.execute(
        "SELECT name, legal_business_name, business_address, trade, service_area "
        "FROM businesses WHERE micro_site_slug=?",
        (slug,)
    ).fetchone()
    conn.close()
    if row is None:
        abort(404)
    biz = dict(row) if hasattr(row, "keys") else {
        "name": row[0], "legal_business_name": row[1],
        "business_address": row[2], "trade": row[3], "service_area": row[4]
    }
    return render_template("microsite.html", biz=biz)


@app.route("/api/places/lookup")
@login_required
def api_places_lookup():
    """Google Places prefill for business legal name + address.
    Gated on GOOGLE_PLACES_API_KEY. Returns {} when unset or on any error.
    Never raises into the response.
    """
    api_key = getattr(config, "GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        return jsonify({})
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({})
    try:
        import requests as _req
        resp = _req.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": q, "key": api_key},
            timeout=5
        )
        data = resp.json()
        results = data.get("results") or []
        if not results:
            return jsonify({})
        top = results[0]
        return jsonify({
            "legal_name": top.get("name", ""),
            "address": top.get("formatted_address", "")
        })
    except Exception:
        return jsonify({})


# ---- Resources sub-pages (each Resources card links to one of these) ----
@app.route("/guides")
def guides():
    return render_template("guides.html")


@app.route("/help")
def help_center():
    return render_template("help.html")


@app.route("/resources/customer-stories")
def customer_stories():
    # Marketing "customer stories" page. Moved off /customers (plan 07-2), which is now
    # the signed-in Customer Book. All marketing links point here instead.
    return render_template("customers.html")


@app.route("/customers")
@login_required
def customer_book():
    """The owner's customer book: their database rendered as a visible, switching-cost
    asset (names, repeat bookings, lifetime jobs). All from existing leads + appointments."""
    biz = current_business()
    if not biz:
        return redirect("/login")
    stats = db.customer_book_stats(biz["id"])
    avg = growth._job_value(biz)
    # Honest money: avg is the owner's real avg_job_value when set, otherwise a trade
    # default. Tell the owner which, so a ~$ estimate is never shown as an exact figure.
    avg_is_estimated = not (biz.get("avg_job_value") or 0)
    lifetime_revenue = stats["total_jobs"] * avg
    return render_template("customer_book.html", business=biz, stats=stats,
                           lifetime_revenue=lifetime_revenue, avg=avg,
                           avg_is_estimated=avg_is_estimated,
                           lifetime_str=f"{lifetime_revenue:,}", avg_str=f"{avg:,}")


@app.route("/blog")
def blog():
    return render_template("blog.html")


@app.route("/webinars")
def webinars():
    return render_template("webinars.html")


@app.route("/templates")
def templates_page():
    return render_template("templates.html")


@app.route("/simulator")
@login_required
def simulator():
    return render_template("simulator.html", business=current_business())


# ---- Public demo + sandbox business (Agent GAMMA — Phase 0) ----
# The demo runs against a DEDICATED sandbox business that is NEVER a real tenant.
# Identified by the sentinel name stored in _DEMO_BIZ_SENTINEL; created once on
# first use and reused on subsequent calls.  No real-tenant data is ever read or
# written by the /demo or /api/demo/* routes.
_DEMO_BIZ_SENTINEL = "__firstback_demo_sandbox__"
_demo_biz_id_cache = None   # module-level cache so we skip the SELECT on hot paths
_demo_biz_lock = threading.Lock()


def _get_or_create_demo_biz():
    """Return the ID of the dedicated sandbox business, creating it the first time.
    Thread-safe; result cached for the lifetime of the process."""
    global _demo_biz_id_cache
    if _demo_biz_id_cache is not None:
        return _demo_biz_id_cache
    with _demo_biz_lock:
        if _demo_biz_id_cache is not None:  # double-checked under lock
            return _demo_biz_id_cache
        conn = db.get_conn()
        row = conn.execute(
            "SELECT id FROM businesses WHERE name=? LIMIT 1",
            (_DEMO_BIZ_SENTINEL,),
        ).fetchone()
        if row:
            bid = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO businesses (name, trade, service_area, hours, "
                "owner_name, ai_instructions, phone) VALUES (?,?,?,?,?,?,?)",
                (
                    _DEMO_BIZ_SENTINEL,
                    "Residential & commercial painting",
                    "Greater metro area (30-mile radius)",
                    "Mon-Sat, 7am-6pm",
                    "Demo",
                    (
                        "You are the assistant for a home-services demo, replying by "
                        "text to a caller you just missed. Be friendly and brief. "
                        "Find out what they need painted, confirm they are in your "
                        "service area, then offer two estimate windows and book one."
                    ),
                    "(555) 314-2270",
                ),
            )
            conn.commit()
            bid = cur.lastrowid
        conn.close()
        _demo_biz_id_cache = bid
        return bid


def _demo_biz():
    """Return the sandbox business dict, with a display-friendly name injected."""
    bid = _get_or_create_demo_biz()
    biz = db.get_business(bid)
    biz["name"] = "FirstBack Demo"   # override sentinel name for UI display only
    return biz


@app.route("/demo")
def demo():
    """Public demo — NO login required.  Runs the simulator against the dedicated
    sandbox business so visitors can experience the text-back flow without touching
    any real tenant's data."""
    return render_template("simulator.html", business=_demo_biz(), demo_mode=True)


@app.route("/api/demo/incoming", methods=["POST"])
def demo_sim_incoming():
    """Public sim incoming — scoped EXCLUSIVELY to the sandbox business."""
    biz = _demo_biz()
    data = request.get_json(silent=True) or {}
    scenario = (data.get("scenario") or "prospect").strip().lower()
    if scenario in ("spam", "known"):
        if scenario == "spam":
            score, reasons = triage.spam_score(
                {"attestation": "TN-Validation-Failed-C", "neighbor_spoof": True,
                 "line_type": "nonFixedVoip", "behavior": {"missed_calls": 4}})
            return jsonify(screened=True, status="screened_spam", label="Spam",
                           score=score, reasons=reasons)
        return jsonify(screened=True, status="trusted", label="Known caller", score=0,
                       reasons=["You've worked with this caller before — FirstBack leaves "
                                "them to you instead of sending an automated text."])
    bid = _get_or_create_demo_biz()
    lead_id = db.create_lead(bid, data.get("name") or "Demo Caller",
                             data.get("phone") or "+1 (415) 555-0142")
    reply = open_conversation(biz, db.get_lead(lead_id))
    return jsonify(lead_id=lead_id, reply=reply, _demo=True, _biz_id=bid)


@app.route("/api/demo/reply", methods=["POST"])
def demo_sim_reply():
    """Public sim reply — scoped EXCLUSIVELY to the sandbox business."""
    bid = _get_or_create_demo_biz()
    data, err = _get_json("lead_id", "body")
    if err:
        return err
    # Ownership-scoped: only leads that belong to the sandbox business are reachable
    lead = db.get_lead(data["lead_id"], bid)
    if not lead:
        return jsonify(error="Lead not found."), 404
    biz = _demo_biz()
    reply, booked, urgent = handle_inbound(biz, lead, data["body"])
    return jsonify(reply=reply, booked=booked, urgent=urgent, _demo=True)


def _time_ago(iso):
    """Human 'Xh/Xd/Xw ago' from an ISO timestamp, or None if missing/unparseable.
    Never raises -- callers use it for a best-effort orientation line."""
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
        now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
        secs = (now - then).total_seconds()
        if secs < 3600:
            return "just now"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
        days = int(secs // 86400)
        return f"{days}d ago" if days < 14 else f"{days // 7}w ago"
    except Exception:
        return None


def _fire_roi_milestone(biz):
    """Check for a due progressive ROI milestone (plan 07-3); if one crossed, alert the
    owner and record the level. Back-compat: also stamp roi_milestone_sent_at at level 2 so
    older code/reads still see the first milestone. Never raises (best-effort post-booking)."""
    try:
        import roi as _roi_mod
        m = _roi_mod.check_roi_milestone(biz["id"])
        if not m:
            return
        # Record the fire BEFORE the async SMS so a crash in between can't re-fire it.
        db.mark_roi_milestone(biz["id"], m["level"], m.get("revenue"))
        if m["level"] == 2:
            from datetime import timezone as _tzu
            db.set_roi_milestone_sent(biz["id"], datetime.now(_tzu.utc).isoformat())
        # Pass level so the alert dedupe key is per-level (two bookings seconds apart that
        # cross different levels must each send -- not collapse to one).
        alerts.notify_async(biz, "roi_milestone",
                            {"level": m["level"], "body": m.get("body", ""),
                             "multiple": m.get("multiple"), "revenue": m.get("revenue")})
    except Exception as _me:
        print(f"[firstback] milestone hook error (biz {biz.get('id')}): {_me}",
              file=sys.stderr, flush=True)


@app.route("/dashboard")
@login_required
def dashboard():
    """The signed-in home is now the conversational command center. The cockpit (leads,
    booked estimates, alerts) still lives at /pipeline for working by hand."""
    hour = datetime.now(app_tz()).hour
    part = "Morning" if hour < 12 else ("Afternoon" if hour < 17 else "Evening")
    biz = current_business()
    if not biz:
        return redirect("/login")
    owner = (biz.get("owner_name") or "").strip() if biz else ""
    hello = f"{part}, {owner.split()[0]}." if owner else f"{part}."
    brief, chips, feed_sig = _command_feed(biz)
    # Orientation line for the 'all clear' state: the most recent lead, if any.
    _last = db.last_lead(biz["id"])
    return render_template("command.html", hello=hello,
                           briefing=brief, feed_sig=feed_sig,
                           digest=convos.digest(biz["id"]),
                           golive=connections.golive_summary(biz),
                           suggestions=chips,
                           last_lead_name=(_last["name"] if _last else None),
                           last_lead_ago=(_time_ago(_last["created_at"]) if _last else None))


def _command_feed(biz):
    """The command-center feed payload: the briefing card, adaptive chips, and a content
    signature. Best-effort -- a DB hiccup degrades to a quiet briefing / static chips, never
    a 500. Shared by the /dashboard render and the /api/feed real-time poll."""
    try:
        brief = assistant.briefing(biz)
    except Exception as e:
        print(f"[firstback] briefing failed, hiding it: {e}", flush=True)
        brief = {"type": "briefing", "tone": "quiet", "headline": "", "sub": "", "items": []}
    try:
        chips = assistant.adaptive_suggestions(biz)
    except Exception as e:
        print(f"[firstback] adaptive suggestions failed, using static: {e}", flush=True)
        chips = assistant.suggestions()
    return brief, chips, assistant.briefing_signature(brief)


@app.route("/api/feed")
@login_required
def api_feed():
    """Real-time refresh for the command center (poll baseline). Returns the current briefing
    card, adaptive chips, and a signature so the client re-renders only on change. Read-only,
    tenant-scoped -- a just-missed call surfaces without a reload that would wipe the chat."""
    biz = current_business()
    brief, chips, sig = _command_feed(biz)
    return jsonify(briefing=brief, suggestions=chips, sig=sig)


@app.route("/pipeline")
@login_required
def pipeline():
    """The manual cockpit: leads, booked estimates, alerts, screened calls. Everything the
    command center can do by chat, you can still do here by hand."""
    biz = current_business()
    leads = db.leads_with_stage(biz["id"])
    appts = db.list_appointments(biz["id"])
    stats = {
        "leads": len(leads),
        "booked": len([l for l in leads if l["status"] == "booked"]),
        "appointments": len(appts),
    }
    # Spam Shield graduation state: how many days into the observation window, and
    # whether it has already promoted to enforce (screening_promoted_at set).
    _window_start = biz.get("screening_window_start")
    _grad_days = None
    # Only show the "Learning (Day N of 7)" card while actually observing in monitor mode --
    # never alongside enforce (auto-promoted or set by hand), where "nothing is silenced yet"
    # would contradict the live shield.
    if (_window_start and not biz.get("screening_promoted_at")
            and _effective_screen_mode(biz) == "monitor"):
        try:
            import datetime as _dt
            _ws = _dt.datetime.fromisoformat(_window_start)
            # now_iso() is tz-aware; match awareness so the subtraction never raises.
            _now = _dt.datetime.now(_ws.tzinfo) if _ws.tzinfo else _dt.datetime.utcnow()
            _grad_days = max(0, (_now - _ws).days)
        except Exception:
            pass
    _grad_total = getattr(config, "SCREEN_GRADUATION_DAYS", 7)
    return render_template("dashboard.html", leads=leads, appointments=appts, stats=stats,
                           alert_feed=db.recent_alerts(biz["id"], 8),
                           reminder_state=db.reminders_by_appointment(biz["id"]),
                           screened=db.recent_screened_calls(biz["id"], 8),
                           screen_stats=db.screening_stats(biz["id"]),
                           screen_mode=_effective_screen_mode(biz),
                           review_count=db.count_pending_suggestions(biz["id"]),
                           screening_promoted_at=biz.get("screening_promoted_at"),
                           screening_false_positives=biz.get("screening_false_positives") or 0,
                           grad_day=_grad_days,
                           grad_total=_grad_total)


@app.route("/assistant", methods=["POST"])
@login_required
def assistant_chat():
    """One natural-language turn against the command center. Same-origin JSON/form POST
    (FirstBack's API auth is the SameSite session cookie). Returns reply, inline cards, and
    an optional pending_action that needs an explicit confirm before it runs."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    message = (request.form.get("message") or "").strip()
    allow_llm, throttled = _assistant_budget(biz, message)
    if throttled:
        return jsonify({"reply": "One moment, that was a lot at once. Give it a few seconds "
                                 "and try again.", "cards": [], "pending_action": None,
                        "meta": {"tool": None, "status": "rate_limited"}}), 429
    history = _sanitize_history(request.form.get("history"))
    # Resolve the conversation up front (browser_key survives reloads) so we can recall what
    # was just shown and resolve referents like "text her back" before routing.
    convo_key = request.form.get("convo_key", "")
    browser_key = (request.form.get("browser_key") or "").strip()
    convo_id = db.start_or_get_convo(biz["id"], convo_key, browser_key) if message else None
    entities = db.recent_entities(biz["id"], convo_id) if convo_id else []
    out = assistant.run(biz, message, history, entities=entities, allow_llm=allow_llm)
    if not allow_llm:
        out["vic_status"] = "resting"
        out["resets_at"] = _next_local_midnight_iso(biz)
    if message:
        convo_id, _ = convos.record_exchange(biz["id"], convo_key, message, out,
                                             browser_key=browser_key, convo_id=convo_id)
        out["coach"] = convos.coach_offer(biz["id"], convo_id, message)
    return jsonify(out)


@app.route("/assistant/stream", methods=["POST"])
@login_required
def assistant_stream():
    """Streaming sibling of /assistant: the same turn over a Server-Sent-Events channel so
    the reply renders token-by-token. Identical auth + CSRF + rate-limit + memory contract;
    each frame is `data: {json}` -- a {"t":"delta","v":...} per text slice, then one
    {"t":"done","result":{...}} carrying the SAME shape /assistant returns (cards,
    pending_action, coach), so the client reuses the exact renderers and confirm gate. The
    non-streaming /assistant above stays the fallback for clients without streaming."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    message = (request.form.get("message") or "").strip()
    allow_llm, throttled = _assistant_budget(biz, message)
    # Throttle before the stream opens, so the HTTP status is an honest 429 (you can't set a
    # status code once SSE bytes have started). The client's fallback re-hits /assistant.
    if throttled:
        return jsonify({"reply": "One moment, that was a lot at once. Give it a few seconds "
                                 "and try again.", "cards": [], "pending_action": None,
                        "meta": {"tool": None, "status": "rate_limited"}}), 429
    history = _sanitize_history(request.form.get("history"))
    convo_key = request.form.get("convo_key", "")
    browser_key = (request.form.get("browser_key") or "").strip()
    convo_id = db.start_or_get_convo(biz["id"], convo_key, browser_key) if message else None
    entities = db.recent_entities(biz["id"], convo_id) if convo_id else []

    def _sse(obj):
        return "data: " + json.dumps(obj) + "\n\n"

    def gen():
        try:
            for kind, payload in assistant.run_stream(biz, message, history,
                                                      entities=entities, allow_llm=allow_llm):
                if kind == "delta":
                    yield _sse({"t": "delta", "v": payload})
                else:  # done
                    if not allow_llm:
                        payload["vic_status"] = "resting"
                        payload["resets_at"] = _next_local_midnight_iso(biz)
                    if message:
                        cid, _ = convos.record_exchange(biz["id"], convo_key, message, payload,
                                                        browser_key=browser_key,
                                                        convo_id=convo_id)
                        payload["coach"] = convos.coach_offer(biz["id"], cid, message)
                    yield _sse({"t": "done", "result": payload})
        except Exception as e:
            print(f"[firstback] assistant stream failed: {e}", flush=True)
            yield _sse({"t": "done", "result": {
                "reply": "Something went wrong on my end. Try that again.", "cards": [],
                "pending_action": None, "meta": {"tool": None, "status": "error"}}})

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/assistant/learn", methods=["POST"])
@login_required
def assistant_learn():
    """Accept the assistant's proactive teaching offer: store the learning + resolve the gap."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    pattern = (request.form.get("pattern") or "").strip()
    action = (request.form.get("action") or "route").strip()
    value = (request.form.get("value") or "").strip()
    if pattern:
        convos.accept_coach(biz["id"], pattern, action, value)
        db.add_audit(biz["id"], "learn", f"{action}: {pattern[:80]}")
    return jsonify({"ok": bool(pattern)})


@app.route("/assistant/confirm", methods=["POST"])
@login_required
def assistant_confirm():
    """Redeem a server-bound confirm token (SF-6). The owner approves by token ALONE; we
    re-run the EXACT tool+args we stored when the preview was issued -- the client can't swap
    the action or its recipient. Single-use (no replay/double-send), expiring, per-tenant. The
    send still flows through the gated messaging.send_sms seam (opt-outs + simulated honored)."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    token_id = (request.form.get("confirm_token") or "").strip()
    if not token_id:
        return jsonify({"reply": "I couldn't find that confirmation to run. Ask me again and "
                        "I'll set it back up.", "cards": [], "pending_action": None,
                        "meta": {"tool": None, "status": "error"}}), 400
    row = db.get_confirm_token(biz["id"], token_id)
    if not row:
        # Unknown / wrong-tenant / already cleaned up. Fail closed, but answer like a human
        # (a 4xx would surface the client's generic "something went wrong").
        return jsonify({"reply": "I couldn't find that confirmation -- it may have already run. "
                        "Ask me again if you still need it.", "cards": [],
                        "pending_action": None, "meta": {"tool": None, "status": "error"}})
    tool = row["tool"]
    # Parse stored args immediately -- used for both the P1-6 enforce gate and execution below.
    try:
        args = json.loads(row["args_json"] or "{}")
        if not isinstance(args, dict):
            args = {}
    except (ValueError, TypeError):
        args = {}
    # Idempotent replay: already redeemed -> return the stored result, never re-execute.
    if row["consumed"]:
        try:
            return jsonify(json.loads(row["result_json"]))
        except (ValueError, TypeError):
            return jsonify({"reply": "I already took care of that one.", "cards": [],
                            "pending_action": None, "meta": {"tool": tool, "status": "ok"}})
    # Expired -> never act on stale state; recompute on the owner's next ask.
    if float(row["expires_at"] or 0) < time.time():
        return jsonify({"reply": "That confirmation expired -- the situation may have changed. "
                        "Ask me again and I'll show you the current picture.", "cards": [],
                        "pending_action": None, "meta": {"tool": tool, "status": "expired"}})
    # P1-6 Enforce-mode second acknowledgment: silencing real callers requires two taps.
    # Gate fires BEFORE atomic claim so the token stays valid for the second tap.
    if (tool == "set_screen_mode" and args.get("mode") == "enforce"
            and request.form.get("enforce_ack") != "true"):
        return jsonify({"reply": "This silences real callers -- they get no text back. "
                        "Tap again to confirm.", "cards": [], "pending_action": None,
                        "meta": {"tool": tool, "status": "pending_ack"}})
    # Claim atomically: only the first redemption executes (race guard / no double-send).
    if not db.claim_confirm_token(biz["id"], token_id):
        again = db.get_confirm_token(biz["id"], token_id)
        if again and again.get("result_json"):
            try:
                return jsonify(json.loads(again["result_json"]))
            except (ValueError, TypeError):
                pass
        return jsonify({"reply": "I'm already running that one -- give me a second.",
                        "cards": [], "pending_action": None,
                        "meta": {"tool": tool, "status": "ok"}})
    # The ONLY client-overridable field: the text_lead body the owner may have edited on the
    # confirm card. The recipient + the action stay server-bound (from the stored args), so
    # an edit can't redirect the message or change what runs.
    if tool == "text_lead":
        edited = (request.form.get("message") or "").strip()[:1600]  # cap pathological input
        if edited:
            args["message"] = edited
    out = assistant.execute(biz, tool, args)
    db.set_confirm_result(token_id, json.dumps(out), business_id=biz["id"])
    # Audit the confirmed action (token id, no raw phone; body is the owner's own words).
    db.add_audit(biz["id"], f"confirm:{tool}",
                 f"token={token_id[:8]} {str(args.get('message') or '')[:100]}")
    convos.record_exchange(biz["id"], request.form.get("convo_key", ""),
                           f"[confirmed: {tool}]", out)
    return jsonify(out)


# ---- Vic's Memory / Training: review conversations, call out issues, teach ----
_ISSUE_LABEL = {"capability_gap": "FirstBack had no tool for this",
                "empty": "A tool returned nothing", "repeat": "You had to re-ask",
                "negative": "You pushed back on the answer",
                "unhelpful": "FirstBack's answer missed the mark"}


@app.route("/training")
@login_required
def training():
    """What the assistant has heard, where it fell short, and what you've taught it."""
    biz = current_business()
    return render_template("training.html",
                           flags=db.list_flags(biz["id"], resolved=0, limit=40),
                           counts=db.flag_counts(biz["id"]),
                           convos=db.list_convos(biz["id"], limit=12),
                           learnings=db.list_learnings(biz["id"]),
                           digest=convos.digest(biz["id"]),
                           top_unmet=convos.top_unmet(biz["id"]),
                           tools=sorted(assistant.TOOLS.keys()),
                           issue_label=_ISSUE_LABEL)


@app.route("/training/convo/<int:convo_id>")
@login_required
def training_convo(convo_id):
    biz = current_business()
    turns = db.get_convo_turns(convo_id, biz["id"])
    if not turns:
        return redirect("/training")
    return render_template("training_convo.html", convo_id=convo_id, turns=turns)


@app.route("/training/teach", methods=["POST"])
@login_required
def training_teach():
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    pattern = (request.form.get("pattern") or "").strip()
    action = (request.form.get("action") or "").strip()
    value = (request.form.get("value") or "").strip()
    if not pattern or not action:
        return redirect("/training")
    if action in assistant.TOOLS:
        convos.teach(biz["id"], pattern, action)
    elif action in ("route", "answer"):
        convos.teach(biz["id"], pattern, action, answer=value)
    flag_id = request.form.get("flag_id")
    if flag_id and flag_id.isdigit():
        db.resolve_flag(biz["id"], int(flag_id))
    return redirect("/training?taught=1")


@app.route("/training/resolve", methods=["POST"])
@login_required
def training_resolve():
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    flag_id = request.form.get("flag_id")
    if flag_id and flag_id.isdigit():
        db.resolve_flag(biz["id"], int(flag_id))
    return redirect("/training")


@app.route("/digest/send", methods=["POST"])
@login_required
def digest_send():
    """Email this owner their weekly digest now (gated/simulated until SMTP is set)."""
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    user = current_user()
    em = convos.digest_email(biz)
    res = mail.send_email(user["email"], em["subject"], em["body"])
    return redirect(f"/training?digest={res['status']}")


@app.route("/tasks/digest", methods=["POST"])
def tasks_digest():
    """The weekly-digest cron: email every tenant's owner. Locked behind the tasks secret
    (X-Tasks-Secret header), like /tasks/run-due; a scheduler hits this once a week."""
    sent_secret = request.headers.get("X-Tasks-Secret", "")
    if not TASKS_SECRET or not secrets.compare_digest(sent_secret, TASKS_SECRET):
        return jsonify(error="Forbidden."), 403
    n = 0
    for bid, email in db.all_owner_recipients():
        em = convos.digest_email(db.get_business(bid))
        mail.send_email(email, em["subject"], em["body"])
        n += 1
    return jsonify({"sent": n})


@app.route("/analytics")
@login_required
def analytics_page():
    return render_template("analytics.html")


@app.route("/callers")
@login_required
def callers_page():
    """The caller-triage inbox: review FirstBack's suggestions (To review / Sorted /
    Dismissed), import an address book, and manage the screened-numbers directory.
    JS-driven via /api/*."""
    return render_template("callers.html",
                           gc_configured=google_contacts.configured(),
                           gc_connected=google_contacts.is_connected(current_business()["id"]),
                           gcerror=request.args.get("gcerror"))


@app.route("/api/analytics")
@login_required
def api_analytics():
    """ROI metrics (read-only, tenant-scoped). range = 30d | 90d | all."""
    days = {"30d": 30, "90d": 90, "all": None}.get(request.args.get("range", "30d"), 30)
    return jsonify(db.analytics(current_business()["id"], days))


@app.route("/api/reputation")
@login_required
def api_reputation():
    """E4: Google review snapshot for the current business (tenant-scoped, read-only).
    Current count/rating + the baseline snapshot + last-updated. Null until the first poll."""
    biz = current_business()
    if not biz:
        return jsonify(review_count=None), 200
    return jsonify(
        review_count=biz.get("google_review_count"),
        star_rating=biz.get("google_star_rating"),
        baseline_count=biz.get("google_review_count_baseline"),
        baseline_rating=biz.get("google_star_rating_baseline"),
        updated_at=biz.get("review_count_updated_at"),
    )


def _save_screening_prefs(business_id, screen_hard, screen_mid, reputation_enabled,
                          screening_hold):
    """Persist per-tenant screening tuning columns. These live on `businesses` but are
    NOT in _BUSINESS_COLS (so a profile save never blanks them). Called from the settings
    POST; uses db.get_conn() directly so no db.py edit is required for the UI slice."""
    conn = db.get_conn()
    conn.execute(
        "UPDATE businesses SET screen_hard=?, screen_mid=?, "
        "reputation_enabled=?, screening_hold=? WHERE id=?",
        (screen_hard, screen_mid, reputation_enabled, screening_hold, business_id))
    conn.commit()
    conn.close()


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    biz = current_business()
    if request.method == "POST":
        if not _csrf_ok():
            abort(403)
        fields = {k: request.form.get(k, "") for k in
                  ["name", "trade", "service_area", "hours", "owner_name",
                   "phone", "ai_instructions"]}
        db.update_business(biz["id"], fields)
        def _clamp_form_int(name, default, lo, hi):
            try:
                return max(lo, min(hi, int(request.form.get(name) or default)))
            except (TypeError, ValueError):
                return default
        db.update_alert_prefs(biz["id"], {
            "alert_email": request.form.get("alert_email", "").strip(),
            "alert_sms": request.form.get("alert_sms", "").strip(),
            "alert_on_lead": 1 if request.form.get("alert_on_lead") else 0,
            "alert_on_booking": 1 if request.form.get("alert_on_booking") else 0,
            "alert_on_urgent": 1 if request.form.get("alert_on_urgent") else 0,
            "alert_on_daily_digest": 1 if request.form.get("alert_on_daily_digest") else 0,
            "alert_on_roi_milestone": 1 if request.form.get("alert_on_roi_milestone") else 0,
            # Plan 05 (Batch D): set-and-forget prefs.
            "alert_all_clear": 1 if request.form.get("alert_all_clear") else 0,
            "alert_quiet_start": _clamp_form_int("alert_quiet_start", 22, 0, 23),
            "alert_quiet_end": _clamp_form_int("alert_quiet_end", 7, 0, 23),
            "max_stall_alerts_day": _clamp_form_int("max_stall_alerts_day", 2, 0, 10),
            "alert_webhook_url": request.form.get("alert_webhook_url", "").strip(),
        })
        try:
            lead_hours = int(float(request.form.get("reminder_lead_hours") or 24))
        except (TypeError, ValueError):
            lead_hours = 24
        db.update_reminder_prefs(biz["id"], {
            "reminders_enabled": 1 if request.form.get("reminders_enabled") else 0,
            "followups_enabled": 1 if request.form.get("followups_enabled") else 0,
            "reminder_lead_hours": max(0, min(168, lead_hours)),
            # Batch G: opt-in lead-source toggles (default OFF).
            "voicemail_enabled": 1 if request.form.get("voicemail_enabled") else 0,
            "widget_enabled": 1 if request.form.get("widget_enabled") else 0,
        })
        # Scheduling preferences: the owner shapes their own availability (estimate
        # windows, working days) and a buffer so the AI never books two estimates
        # too close together. Blank fields fall back to the config defaults.
        db.set_scheduling_prefs(
            biz["id"],
            times=[t for t in (request.form.get("estimate_times") or "").split(",")],
            working_days=request.form.getlist("working_days"),
            buffer_minutes=(request.form.get("buffer_minutes") or 0))
        raw_avg = (request.form.get("avg_job_value") or "").strip().lstrip("$").replace(",", "")
        try:
            db.set_avg_job_value(biz["id"], float(raw_avg) if raw_avg else None)
        except ValueError:
            pass  # leave it unchanged on a non-numeric entry
        db.update_phone_voice(
            biz["id"],
            forward_to=(request.form.get("forward_to") or "").strip(),
            voice_callback_enabled=1 if request.form.get("voice_callback_enabled") else 0,
            inbound_voice_enabled=1 if request.form.get("inbound_voice_enabled") else 0)
        # Per-business screening mode: blank/"default" -> NULL (inherit the app default).
        db.set_screen_mode(biz["id"], (request.form.get("screen_mode") or "").strip())
        # Sensitivity preset: radio -> (screen_hard, screen_mid) stored per-tenant.
        # Blank/unknown preset -> NULL (inherit the config-level defaults).
        sensitivity = (request.form.get("screen_sensitivity") or "").strip()
        presets = getattr(config, "SCREEN_SENSITIVITY_PRESETS", {})
        if sensitivity in presets:
            hard_val, mid_val = presets[sensitivity]
        else:
            hard_val, mid_val = None, None
        # Per-tenant reputation lookup toggle (paid tier opt-in, defaults off).
        rep_enabled = 1 if request.form.get("reputation_enabled") else 0
        # Keep-in-observe: defer auto-graduation even after 7-day window passes.
        hold = 1 if request.form.get("screening_hold") else 0
        _save_screening_prefs(biz["id"], hard_val, mid_val, rep_enabled, hold)
        return redirect("/settings?saved=1")
    _presets = getattr(config, "SCREEN_SENSITIVITY_PRESETS", {})
    # Reverse-map current screen_hard/screen_mid back to the preset name for the radio.
    _cur_hard = biz.get("screen_hard")
    _cur_mid = biz.get("screen_mid")
    _cur_preset = ""
    for _name, (_h, _m) in _presets.items():
        if _cur_hard == _h and _cur_mid == _m:
            _cur_preset = _name
            break
    return render_template("settings.html", business=biz,
                           sched=db.scheduling_prefs(biz["id"]),
                           integrations=db.list_integrations(biz["id"]),
                           saved=request.args.get("saved"),
                           google_configured=google_cal.configured(),
                           google_connected=google_cal.is_connected(biz["id"]),
                           gconnected=request.args.get("gconnected"),
                           gerror=request.args.get("gerror"),
                           outlook_configured=outlook_cal.configured(),
                           outlook_connected=outlook_cal.is_connected(biz["id"]),
                           olconnected=request.args.get("olconnected"),
                           olerror=request.args.get("olerror"),
                           pw=request.args.get("pw"),
                           pwerror=request.args.get("pwerror"),
                           sms_configured=messaging.configured(),
                           email_configured=mail.configured(),
                           voice_configured=bool(VOICE_PUBLIC_URL),
                           screening_enabled=_effective_screen_mode(biz) != "off",
                           screen_mode=_effective_screen_mode(biz),
                           screen_mode_setting=(biz.get("screen_mode") or ""),
                           screen_mode_default=SCREEN_MODE,
                           reputation_configured=reputation.configured(),
                           reputation_label=reputation.provider_label(),
                           ai_screen_enabled=SCREEN_AI_CONTENT,
                           owner_email=db.owner_email(biz["id"]),
                           screen_sensitivity=_cur_preset,
                           screen_sensitivity_presets=list(_presets.keys()),
                           reputation_enabled=bool(biz.get("reputation_enabled")),
                           screening_hold=bool(biz.get("screening_hold")),
                           growth_mode=db.growth_mode(biz["id"]),
                           growth_saved=request.args.get("growth_saved"),
                           jobber_configured=jobber_fsm.configured(),
                           jobber_connected=jobber_fsm.is_connected(biz["id"]),
                           fsm_last_synced_at=biz.get("fsm_last_synced_at"),
                           fsm_clients_synced=biz.get("fsm_clients_synced") or 0,
                           fsmconnected=request.args.get("fsmconnected"),
                           fsmerror=request.args.get("fsmerror"),
                           hcp_configured=hcp_fsm.configured(),
                           hcp_connected=hcp_fsm.is_connected(biz["id"]),
                           hcpconnected=request.args.get("hcpconnected"),
                           hcperror=request.args.get("hcperror"))


@app.route("/settings/password", methods=["POST"])
@login_required
def settings_password():
    if not _csrf_ok():
        abort(403)
    user = current_user()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    if not check_password_hash(user["password_hash"], current):
        return redirect("/settings?pwerror=current")
    if len(new) < 8:
        return redirect("/settings?pwerror=short")
    db.update_user_password(user["id"], generate_password_hash(new))
    return redirect("/settings?pw=1")



# ---- Phase 5d GAMMA: Growth tray routes ----

def _parse_tray_reply(body):
    """Parse an owner SMS into a tray command. Pure function -- no side effects.
    Returns {"cmd": "go"} | {"cmd": "skip_all"} | {"cmd": "skip_n", "n": N} | None.
    None means: not a tray command, fall through to normal inbound handler."""
    import re as _re_tray
    text = (body or "").strip()
    upper = text.upper()
    if upper == "GO":
        return {"cmd": "go"}
    if upper in ("SKIP", "SKIP ALL"):
        return {"cmd": "skip_all"}
    m = _re_tray.match(r"^SKIP\s+(\d+)$", text, _re_tray.IGNORECASE)
    if m:
        return {"cmd": "skip_n", "n": int(m.group(1))}
    return None


def _handle_tray_reply(biz, cmd):
    """Execute a parsed tray command on behalf of the owner. Returns a TwiML Response.
    Confirmation SMS goes to the owner cell only (gate=False, A2P-exempt).
    NEVER sends to any customer number."""
    owner_cell = messaging.to_e164((biz.get("alert_sms") or "").strip())
    if cmd["cmd"] == "go":
        result = db.release_growth_batch(biz["id"], approved_via="sms_go")
        n = result["released"]
        if n > 0:
            db.record_growth_go(biz["id"])  # 07-4: count the GO toward the auto-unlock streak
        msg = f"{n} text{'s' if n != 1 else ''} queued. They will go out shortly."
        if owner_cell:
            messaging.send_sms(biz, owner_cell, msg, gate=False)
    elif cmd["cmd"] == "skip_all":
        held = db.list_held_messages(biz["id"])
        for row in held:
            db.cancel_growth_play(row["id"], biz["id"])
        if owner_cell:
            messaging.send_sms(biz, owner_cell, "Held for tomorrow.", gate=False)
    elif cmd["cmd"] == "skip_n":
        n_idx = cmd["n"]
        held = db.list_held_messages(biz["id"])
        # Cancel the Nth play (1-indexed, ordered by id -- same order as the digest)
        if 1 <= n_idx <= len(held):
            db.cancel_growth_play(held[n_idx - 1]["id"], biz["id"])
            # Release the remaining held plays
            remaining = [r for i, r in enumerate(held, 1) if i != n_idx]
            for row in remaining:
                db.release_growth_play(row["id"], biz["id"], approved_via="sms_go")
            msg = f"Skipped #{n_idx}, sending the rest."
        else:
            msg = "Play not found. Reply GO to send all or SKIP to hold all."
        if owner_cell:
            messaging.send_sms(biz, owner_cell, msg, gate=False)
    return _twiml("<Response/>")


@app.route("/settings/growth_mode", methods=["POST"])
@login_required
def settings_growth_mode():
    """Set growth mode for the business. 'auto' requires the earned 7-day streak; other
    values coerce to off. Growth mode gates TCPA-sensitive sending, so CSRF-guard it."""
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    mode = (request.form.get("mode") or "off").strip()
    if mode == "auto":
        # 07-4 streak gate: only allow auto once the owner has earned the 7-day GO streak.
        if not biz.get("growth_streak_unlocked_at"):
            mode = "tray"  # coerce silently; streak not yet earned
    elif mode not in ("off", "tray"):
        # Unknown mode -> off. Non-negotiable TCPA gate.
        mode = "off"
    db.set_growth_mode(biz["id"], mode)
    return redirect("/settings?growth_saved=1")


@app.route("/growth/tray")
@login_required
def growth_tray():
    """Show the growth tray: held plays awaiting Dave's one-tap approval."""
    biz = current_business()
    held = db.list_held_messages(biz["id"])
    # Compute money total (estimated when avg_job_value is unset)
    avg = biz.get("avg_job_value")
    is_estimated = (avg is None or avg == 0)
    try:
        job_val = int(float(avg)) if avg else 0
    except (TypeError, ValueError):
        job_val = 0
    if job_val <= 0:
        # Trade-keyword fallback (same logic as growth._job_value)
        try:
            from growth import _job_value as _gv
            job_val = _gv(biz)
        except Exception:
            job_val = 2000
        is_estimated = True
    total = job_val * len(held)
    streak_count = biz.get("growth_streak_count") or 0   # 07-4: auto-unlock progress
    # 07-5: surface the seasonal play + cohort count for the seasonal campaign card.
    try:
        _seasonal_play = next((p for p in growth.plays(biz) if p.get("kind") == "seasonal"), None)
        _seasonal_cohort_count = len(growth.seasonal_cohort(biz["id"], date.today())) if _seasonal_play else 0
    except Exception:
        _seasonal_play = None
        _seasonal_cohort_count = 0
    return render_template("growth_tray.html", business=biz, held=held,
                           growth_mode=db.growth_mode(biz["id"]),
                           total=total, is_estimated=is_estimated,
                           released=request.args.get("released"),
                           streak_count=streak_count,
                           seasonal_blocked=request.args.get("seasonal_blocked"),
                           seasonal_queued=request.args.get("seasonal_queued"),
                           seasonal_play=_seasonal_play,
                           seasonal_cohort_count=_seasonal_cohort_count)


@app.route("/growth/tray/release", methods=["POST"])
@login_required
def growth_tray_release():
    """One-tap batch release: flip all held plays to pending. Dave taps Send All,
    release_growth_batch IS the approval event (writes growth_approvals audit rows)."""
    # Phase 6a D-1: CSRF guard — a forged release fires marketing SMS to real customers
    # (a TCPA event), so this is the highest-consequence owner action in the product.
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    result = db.release_growth_batch(biz["id"], approved_via="ui_tap")
    if result["released"] > 0:
        db.record_growth_go(biz["id"])  # 07-4: count the GO toward the auto-unlock streak
    return redirect(f"/growth/tray?released={result['released']}")


@app.route("/growth/tray/skip/<int:sched_id>", methods=["POST"])
@login_required
def growth_tray_skip(sched_id):
    """Cancel one held play (skip this round; dedupe allows it to resurface next cycle)."""
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    db.cancel_growth_play(sched_id, biz["id"])
    return redirect("/growth/tray")


@app.route("/growth/seasonal/launch", methods=["POST"])
@login_required
def launch_seasonal_campaign():
    """07-5 tray-gated cohort blast: queue one seasonal SMS per eligible past customer, each
    as 'held' through the scheduled_messages spine (CSRF-gated). Frequency cap: one blast per
    business per 28-day window; skips opt-outs and recently-touched leads."""
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    if db.recent_growth_touch_kind(biz["id"], "seasonal", within_days=28):
        return redirect("/growth/tray?seasonal_blocked=already_sent")
    from growth import seasonal_cohort, _copy_seasonal
    today = date.today()
    service = request.form.get("service", "seasonal work")
    cohort = seasonal_cohort(biz["id"], today)
    queued = 0
    for lead in cohort:
        phone = (lead.get("phone") or "").strip()
        if not phone or messaging.outbound_mode(biz, phone) == "suppressed":
            continue
        if db.recent_growth_touch(biz["id"], lead["id"], within_days=30):
            continue
        body = _copy_seasonal(lead.get("first", ""), biz, service)
        if "[" in body:
            continue
        send_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        db.add_scheduled_message(biz["id"], lead["id"], None, "seasonal",
                                 send_at, body, status="held")
        queued += 1
    return redirect(f"/growth/tray?seasonal_queued={queued}")


# ---- Go-Live wizard: a contractor connects their phone without a shell or the
#      Twilio console. Driven by connections.step_state + compliance.launch_blockers
#      so the UI can never claim "live" before the number is bound, A2P is approved,
#      and forwarding is confirmed. See connections.py. ----
@app.route("/setup")
@login_required
def setup():
    biz = current_business()
    if biz is None:
        return redirect("/dashboard")
    # Refresh A2P status from Twilio on view so the contractor sees current truth
    # (cheap no-op unless a campaign is on file and not yet approved).
    if biz.get("a2p_campaign_sid") and compliance.a2p_status(biz) != "approved":
        connections.a2p_sync(biz)
        biz = current_business()
    sms_configured = messaging.configured()
    steps = connections.step_state(biz, sms_configured)
    current = connections.current_step(biz, sms_configured)
    area_code = (request.args.get("area_code") or "").strip() or connections.default_area_code(biz)
    # Only hit Twilio for a number list when the buyer is actually on that step.
    available = (connections.available_numbers(area_code)
                 if sms_configured and (current == "number" or request.args.get("edit") == "number")
                 else [])
    # Carrier forwarding code for the forwarding step (defaults to universal GSM).
    carrier = (request.args.get("carrier") or "other").strip()
    fwd = connections.forwarding_code(carrier, biz.get("twilio_number") or "")
    # Most recent inbound call, to verify a real test came through end-to-end.
    last_call = db.last_inbound_call(biz["id"]) if biz.get("twilio_number") else None
    is_live = connections.is_live(biz, sms_configured)
    live_verified = bool(is_live and last_call and last_call.get("engaged"))
    # "Fully set up" tier: recommended connections beyond go-live. Status-only and
    # deep-linked into Settings; never gates `is_live`/`live_verified`. Signals reuse the
    # same checks Settings does (calendar/contacts connection, the business flags) plus
    # whether the owner moved off the seed password.
    user = current_user()
    recommended = connections.recommended_setup(
        biz,
        calendar_connected=google_cal.is_connected(biz["id"]),
        contacts_connected=google_contacts.is_connected(biz["id"]),
        jobber_connected=(fsm_sync.push_configured() and jobber_fsm.is_connected(biz["id"])),
        outlook_connected=(outlook_cal.configured() and outlook_cal.is_connected(biz["id"])),
        hcp_connected=(hcp_fsm.configured() and hcp_fsm.is_connected(biz["id"])),
        password_changed=not (user and check_password_hash(user["password_hash"], SEED_OWNER_PASSWORD)),
        ai_default=config.DEFAULT_BUSINESS.get("ai_instructions", ""))
    return render_template(
        "setup.html", business=biz, steps=steps,
        done_count=sum(1 for s in steps if s["done"]), last_call=last_call,
        current=current, area_code=area_code, available=available,
        carrier=fwd["carrier"], fwd=fwd, carriers=connections.CARRIER_FORWARD_CODES,
        blockers=connections.blockers(biz, sms_configured),
        is_live=is_live, live_verified=live_verified, recommended=recommended,
        sms_configured=sms_configured,
        edit=request.args.get("edit"),
        saved=request.args.get("saved"),
        err=request.args.get("err"))


@app.route("/setup/profile", methods=["POST"])
@login_required
def setup_profile():
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    if biz is None:
        return redirect("/dashboard")
    db.update_business(biz["id"], {
        "name": (request.form.get("name") or "").strip(),
        "trade": (request.form.get("trade") or "").strip(),
        "owner_name": (request.form.get("owner_name") or "").strip(),
        "service_area": (request.form.get("service_area") or "").strip()})
    db.update_a2p_profile(biz["id"], {
        "legal_business_name": (request.form.get("legal_business_name") or "").strip(),
        "ein": (request.form.get("ein") or "").strip(),
        "business_address": (request.form.get("business_address") or "").strip(),
        "website": (request.form.get("website") or "").strip()})
    return redirect("/setup?saved=profile")


@app.route("/setup/number", methods=["POST"])
@login_required
def setup_number():
    """Give the business its FirstBack number: buy a new local one (auto-wires the
    Voice+SMS webhooks via provision_number) or attach a number already owned in the
    Twilio account (the manual path, now one click)."""
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    if biz is None:
        return redirect("/dashboard")
    if not messaging.configured():
        return redirect("/setup?err=twilio")
    mode = request.form.get("mode") or "buy"
    if mode == "attach":
        num = (request.form.get("number") or "").strip()
        e164 = messaging.to_e164(num)
        if not e164:
            return redirect("/setup?err=number")
        if not messaging.account_owns_number(e164):
            return redirect("/setup?err=not_owned")
        if not messaging.attach_owned_number(e164, biz["id"]):
            return redirect("/setup?err=attach")
        return redirect("/setup?saved=number")
    # Buy: a specific picked number, else the first available in the area code.
    phone = (request.form.get("number") or "").strip() or None
    area = (request.form.get("area_code") or "").strip() or connections.default_area_code(biz)
    got = messaging.provision_number(biz["id"], phone=phone, area_code=area or None)
    return redirect("/setup?saved=number" if got else "/setup?err=buy")


@app.route("/setup/a2p", methods=["POST"])
@login_required
def setup_a2p():
    """Carrier texting registration. `mode=record` is the operator-paste path
    (unchanged). `mode=auto` (default) dispatches via connections.submit_a2p which
    calls the Twilio Write API when Trust Hub is configured. `mode=submit` is an alias
    for `auto` kept for template back-compat."""
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    if biz is None:
        return redirect("/dashboard")
    # Default to "auto" so the contractor's submit button triggers the automated path.
    mode = request.form.get("mode") or "auto"
    if mode == "record":
        # Operator-paste path: unchanged. Only operators may use this.
        if not _is_operator(current_user()):
            abort(403)
        db.set_a2p_registration(
            biz["id"],
            brand_sid=(request.form.get("brand_sid") or "").strip() or None,
            campaign_sid=(request.form.get("campaign_sid") or "").strip() or None,
            messaging_service_sid=(request.form.get("messaging_service_sid") or "").strip() or None)
        connections.a2p_sync(biz["id"])          # try to confirm immediately
        return redirect("/setup?saved=a2p")
    if mode in ("auto", "submit"):
        # Automated path: guard on profile completeness, then dispatch.
        if not connections.profile_complete(biz):
            return redirect("/setup?err=profile")
        result = connections.submit_a2p(biz["id"])
        if isinstance(result, dict) and result.get("status") == "error":
            return redirect("/setup?err=a2p_submit")
        return redirect("/setup?saved=a2p")
    # Unknown mode: treat as profile-incomplete guard (safe fallback).
    return redirect("/setup?err=profile")


@app.route("/setup/forwarding", methods=["POST"])
@login_required
def setup_forwarding():
    """Confirm how missed calls reach FirstBack. Default = the catcher model: the owner
    sets carrier conditional-forwarding on their own phone (forward_to stays BLANK, so
    any inbound call is treated as already-missed -> instant text-back). Advanced =
    dial-through: FirstBack rings the owner's cell first, then texts if unanswered.

    SF-7: when in dial mode AND Twilio is configured, fire a sentinel call to verify
    forwarding is live. forwarding_confirmed is intentionally left False here; it is
    set True ONLY when the sentinel call arrives inbound (twilio_voice_inbound).
    [DECIDED] Honesty rule: never set confirmed=True on 'placed'."""
    if not _csrf_ok():
        abort(403)
    biz = current_business()
    if biz is None:
        return redirect("/dashboard")
    mode = request.form.get("mode") or "catcher"
    if mode == "dial":
        cell = (request.form.get("forward_to") or "").strip()
        canonical = messaging.to_e164(cell)
        if not canonical:
            return redirect("/setup?err=forward")
        db.update_phone_voice(biz["id"], forward_to=canonical)
        # SF-7: fire the sentinel to verify forwarding; leave confirmed=False.
        if messaging.configured():
            sentinel_result = connections.send_sentinel_call(biz["id"])
            if sentinel_result.get("status") == "placed":
                # Leave confirmed=0; the inbound webhook confirms it.
                return redirect("/setup?saved=forwarding&verifying=1")
            # Twilio is configured but the sentinel could not be placed (almost always
            # FIRSTBACK_PUBLIC_URL is unset, so Twilio has no TwiML URL to dial). We do
            # NOT self-attest confirmed here -- that would re-introduce the exact unproven
            # "confirmed" SF-7 exists to kill. Leave it unconfirmed and surface the misconfig.
            return redirect("/setup?saved=forwarding&unverified=1")
        else:
            # No telephony configured at all (pure local dev) -> nothing to verify
            # against; allow a manual confirm so the local wizard can complete.
            db.set_forwarding_confirmed(biz["id"], True)
    else:
        db.update_phone_voice(biz["id"], forward_to="")   # catcher: blank = text immediately
        # Catcher mode relies on the SAME carrier conditional-forwarding as dial mode,
        # so it gets the SAME honest proof: sentinel the owner's own cell (forward_to
        # is blank here by design). If the carrier star code is set, the call rings
        # back to the FirstBack number -> twilio_voice_inbound confirms it. We never
        # self-attest confirmed=True here. [DECIDED] honesty rule.
        owner_cell = messaging.to_e164((biz.get("alert_sms") or biz.get("phone") or "").strip())
        if messaging.configured():
            if not owner_cell:
                # We have telephony but no cell to sentinel -> can't verify honestly.
                return redirect("/setup?saved=forwarding&unverified=nocell")
            sentinel_result = connections.send_sentinel_call(biz["id"], to_number=owner_cell)
            if sentinel_result.get("status") == "placed":
                return redirect("/setup?saved=forwarding&verifying=1")
            # Configured but the sentinel couldn't be placed (no public URL / error):
            # do NOT self-attest. Leave unconfirmed and surface the misconfig.
            return redirect("/setup?saved=forwarding&unverified=1")
        else:
            # No telephony configured at all (pure local dev) -> manual confirm.
            db.set_forwarding_confirmed(biz["id"], True)
    return redirect("/setup?saved=forwarding")


# ---- SF-7: Sentinel TwiML (POST, Twilio-signed) ----
@app.route("/webhooks/twilio/voice/sentinel-twiml", methods=["POST"])
@require_twilio_signature
def twilio_sentinel_twiml():
    """TwiML served to Twilio when it dials the owner's phone as a sentinel probe.
    A brief message + Hangup so the owner hears almost nothing and the call ends.
    The real confirmation happens in twilio_voice_inbound when this call rings back
    to the FirstBack number via the carrier's call-forwarding."""
    return _twiml("<Response><Say>Forwarding verified.</Say><Hangup/></Response>")


# ---- Phase-4 C: Dispatcher Call TwiML routes ----
def _dispatcher_lead_owned(lead_id):
    """SF-10 P2 (Phase-4 carryover): defense-in-depth tenant ownership for dispatcher
    TwiML. The routes are Twilio-signed, but the lead_id is in the URL with no tenant
    scope. When the request's From/To positively resolves to a FirstBack business
    (place_call dials From=the business number To=the owner cell), the lead MUST belong
    to that business. An unresolvable number (shared/simulated) falls back to the Twilio
    signature gate. Returns the lead dict when allowed, else None."""
    lead = db.get_lead(lead_id)
    if not lead:
        return None
    for num in (request.form.get("From"), request.form.get("To")):
        biz = db.get_business_by_twilio_number((num or "").strip()) if num else None
        if biz:
            return lead if lead.get("business_id") == biz["id"] else None
    return lead  # no resolvable business number -> rely on the Twilio signature


@app.route("/twiml/dispatcher/<int:lead_id>", methods=["POST"])
@require_twilio_signature
def dispatcher_twiml(lead_id):
    """TwiML served to the owner's phone when FirstBack places an urgent dispatcher
    call. Reads the caller's exact last inbound message, then offers press-1 to
    connect. The words come from db.get_last_inbound_message — always synchronous,
    never relies on async-enriched summary which may not have landed yet."""
    if _dispatcher_lead_owned(lead_id) is None:
        return _twiml("<Response><Say>Goodbye.</Say><Hangup/></Response>")
    last_words = db.get_last_inbound_message(lead_id)
    safe_words = _xesc(last_words) if last_words else "an urgent message"
    connect_url = _public_base() + f"/twiml/dispatcher/connect/{lead_id}"
    xml = (
        f'<Response>'
        f'<Say>Urgent lead alert. Your caller said: {safe_words}. '
        f'Press 1 to connect to them now.</Say>'
        f'<Gather numDigits="1" action="{connect_url}" method="POST">'
        f'<Say>Press 1 to connect.</Say>'
        f'</Gather>'
        f'<Say>No input received. Goodbye.</Say>'
        f'</Response>'
    )
    return _twiml(xml)


@app.route("/twiml/dispatcher/connect/<int:lead_id>", methods=["POST"])
@require_twilio_signature
def dispatcher_connect_twiml(lead_id):
    """TwiML that dials the lead's phone number when the owner presses 1.
    If the digit pressed is not 1, hang up gracefully."""
    digit = (request.form.get("Digits") or "").strip()
    if digit != "1":
        return _twiml("<Response><Say>Goodbye.</Say><Hangup/></Response>")
    lead = _dispatcher_lead_owned(lead_id)  # SF-10 P2: tenant-ownership scoped
    if not lead or not lead.get("phone"):
        return _twiml("<Response><Say>Lead not found. Goodbye.</Say><Hangup/></Response>")
    caller_number = _xesc(lead["phone"])
    return _twiml(
        f'<Response><Dial>{caller_number}</Dial></Response>'
    )


# ---- Design-system gallery (internal): renders every UI component + state ----
@app.route("/ui")
def ui_kit():
    return render_template("ui_kit.html")


# ---- API (the pages call these; the Twilio webhooks will too) ----
# >>> TWILIO SEAM: the missed-call + inbound-SMS webhooks (Phase 1) reuse the
#     shared conversation engine below (open_conversation / handle_inbound) and
#     transmit via messaging.send_sms. See CALLBACK_SYSTEM_PLAN.md. <<<
def _get_json(*required):
    """Parse the JSON request body and verify required fields are present.

    Returns (data, None) on success, or (None, (response, 400)) when the body is
    not a JSON object or a required field is missing, so a malformed request gets
    a clean 400 with a helpful message instead of an unhandled 500 (KeyError)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify(error="Request body must be a JSON object."), 400)
    missing = [k for k in required if data.get(k) in (None, "")]
    if missing:
        return None, (jsonify(error="Missing required field(s): "
                              + ", ".join(missing)), 400)
    return data, None
# Notes are precomputed OFF the request path (see _schedule_notes). The guard
# keeps two overlapping triggers for the same lead from both calling the LLM, and
# coalesces a burst of turns into one trailing recompute so the FINAL state (e.g.
# a just-booked lead) is always the one summarized.
_notes_lock = threading.Lock()
_notes_inflight = set()   # leads with a worker running now
_notes_dirty = set()      # leads that got new messages while their worker ran


def _notes_stale(lead, msgs):
    """True if this lead's stored notes need a (re)compute: it has at least one
    customer reply and either has no notes yet or has grown since we last summed."""
    has_inbound = any(m["direction"] == "in" for m in msgs)
    return has_inbound and (not lead.get("summary")
                            or (lead.get("notes_msgs") or 0) < len(msgs))


def _ensure_lead_notes(lead_id):
    """Compute + store a lead's conversation notes (one LLM call via
    ai.summarize_lead). Idempotent: recomputes only when new messages have
    arrived. Meant to run in a background thread, never inside a GET handler."""
    lead = db.get_lead(lead_id)
    if not lead:
        return None
    msgs = db.get_messages(lead_id)
    if _notes_stale(lead, msgs):
        notes = ai.summarize_lead(db.get_business(lead["business_id"]), msgs)
        if notes:
            if lead.get("status") == "booked":
                notes["stage"] = "scheduled"
            db.set_lead_notes(lead_id, name=notes.get("name", ""),
                              address=notes.get("address", ""),
                              project_type=notes.get("project_type", ""),
                              stage=notes.get("stage", ""),
                              summary=notes.get("summary", ""), notes_msgs=len(msgs))
            lead = db.get_lead(lead_id)
    return lead


def _schedule_notes(lead_id):
    """Precompute notes in the background so neither the reply path nor the
    lead-detail GET ever blocks on the LLM. Cheap-gates on staleness first; if a
    worker is already running for this lead, marks it dirty so it recomputes once
    more (catching turns that landed mid-summary) instead of double-spawning."""
    lead = db.get_lead(lead_id)
    if not lead or not _notes_stale(lead, db.get_messages(lead_id)):
        return
    with _notes_lock:
        if lead_id in _notes_inflight:
            _notes_dirty.add(lead_id)
            return
        _notes_inflight.add(lead_id)

    def _run():
        try:
            while True:
                _ensure_lead_notes(lead_id)
                with _notes_lock:
                    # Release atomically unless more messages arrived meanwhile.
                    if lead_id in _notes_dirty:
                        _notes_dirty.discard(lead_id)
                        continue  # loop once more to catch the newer messages
                    _notes_inflight.discard(lead_id)
                    return
        except Exception as e:  # never let a background failure crash the thread
            print(f"[firstback] notes precompute failed for lead {lead_id}: {e}",
                  file=sys.stderr, flush=True)
            with _notes_lock:
                _notes_dirty.discard(lead_id)
                _notes_inflight.discard(lead_id)

    threading.Thread(target=_run, daemon=True).start()


# ---- Conversation engine: transport-agnostic, shared by the simulator and (in
# Phase 1) the Twilio inbound webhooks, so booking-integrity logic lives in ONE
# place. These RECORD the thread (in/out messages) and book; TRANSMISSION (a real
# SMS via messaging.send_sms) is the caller's job, so the simulator stays a pure
# offline demo and only the webhooks actually text the customer. ----
def open_conversation(biz, lead):
    """Generate + record the opening text-back for a brand-new lead, booking if the
    brain already proposed a slot. Returns the reply text."""
    # A new lead just reached out (the missed-call event itself, NOT the text-back)
    # -> alert the owner. Off the hot path; a no-op if alerts are off/unconfigured.
    alerts.notify_async(biz, "lead", {"lead_id": lead["id"], "name": lead.get("name"),
                                      "phone": lead.get("phone")})
    # Batch B: turn 0 has no caller input yet, so a cold LLM call (5-30s) only added latency
    # before the first text landed -- and every second after a missed call costs reply rate.
    # Send a zero-latency hardcoded opener; the LLM takes over from turn 1 (handle_inbound)
    # once the caller has said something. booking is always None here (no slot named yet),
    # so the booking block below is correctly skipped.
    reply, booking = ai.instant_opener(biz)
    db.add_message(lead["id"], "out", reply)
    if booking:
        if db.book_appointment(biz["id"], lead["id"], booking):
            # F04: mirror post-booking hooks from handle_inbound for first-turn bookings.
            gday, gtime = db.parse_day(booking), db.time_key(booking)
            if gday and gtime:
                try:
                    from config import biz_tz as _biz_tz
                    _tz = _biz_tz(biz)
                except (ImportError, AttributeError):
                    _tz = config.app_tz()
                appt = db.find_appointment(biz["id"], lead["id"], gday, gtime)
                appt_id = appt["id"] if appt else None
                google_cal.create_event_async(
                    biz["id"], appt_id,
                    f"Estimate: {lead['name']}",
                    f"FirstBack booked a free estimate for {lead['name']} ({lead.get('phone')}).",
                    gday, gtime, tz=_tz)
                # Plan 14: mirror onto Outlook Calendar (additive, guarded, daemon thread).
                if outlook_cal.is_connected(biz["id"]):
                    outlook_cal.create_event_async(
                        biz["id"], appt_id,
                        f"Estimate: {lead['name']}",
                        f"FirstBack booked a free estimate for {lead['name']} ({lead.get('phone')}).",
                        gday, gtime, tz=_tz)
                reminders.enqueue_reminder(biz, lead, gday, gtime)
                reminders.enqueue_morning_reminder(biz, lead, gday, gtime)
                # Plan 13: push to Jobber as a quote request (additive, guarded daemon thread).
                fsm_sync.push_booking_async(biz["id"], appt_id, lead, {"day": gday, "when": booking})
            # Phase-4: a first-turn booking is a real booking -> fire the owner's
            # Show-Up-Prepared alert + the ROI milestone check, same as handle_inbound
            # (previously only the reply-turn path did this).
            _book_ctx = {"lead_id": lead["id"], "name": lead.get("name"),
                         "phone": lead.get("phone"), "when": booking}
            if lead.get("address"):
                _book_ctx["address"] = lead["address"]
            if lead.get("project_type"):
                _book_ctx["project"] = lead["project_type"]
            if lead.get("summary"):
                _book_ctx["summary"] = lead["summary"]
            alerts.notify_async(biz, "booking", _book_ctx)
            _fire_roi_milestone(biz)
    return reply


def handle_inbound(biz, lead, body):
    """Run one inbound customer turn: record it, detect urgency, get the AI reply,
    and book if a slot was accepted (mirroring to Google Calendar + precomputing
    notes off the hot path). Returns (reply, booked, urgent)."""
    lead_id = lead["id"]
    db.add_message(lead_id, "in", body)
    # Phase 5e S4: Lead re-engaged -- cancel any queued follow-up touches so they
    # don't fire after the customer has already replied.
    try:
        db.cancel_pending_followup_touches(lead_id)
    except Exception as _cfe:
        import sys as _sys
        print(f"[firstback] cancel_pending_followup_touches failed (lead {lead_id}): {_cfe}",
              file=_sys.stderr, flush=True)
    urgent = ai.detect_urgency(body)
    if urgent:
        db.mark_lead_urgent(lead_id, biz["id"])
        alerts.notify_async(biz, "urgent", {"lead_id": lead_id,
                                            "name": lead.get("name"),
                                            "phone": lead.get("phone")})
        # Phase-4 C: Dispatcher Call — call the owner with the caller's exact last
        # words + a press-1-to-connect TwiML.  Rate-limit: one call per lead urgency
        # event (guard on dispatcher_call_last_at on the lead row).  If place_call
        # returns simulated/error, the existing SMS alert is the backstop — NEVER
        # claim a call was placed when it wasn't.
        _owner_cell = (biz.get("alert_sms") or biz.get("phone") or "").strip()
        _already_called = lead.get("dispatcher_call_last_at")
        if _owner_cell and not _already_called:
            # Gate on VOICE_PUBLIC_URL (master switch), but build the TwiML URL from
            # the FLASK base (PUBLIC_BASE_URL or _public_base()) because the
            # /twiml/dispatcher/<id> route lives on the Flask app, NOT the separate
            # voice service.  Using VOICE_PUBLIC_URL here causes a 404 in split-service
            # prod where the two services run at different origins.
            # Prefer the explicit config value so this also works in unit tests that
            # call handle_inbound() outside a request context.
            if VOICE_PUBLIC_URL:
                _fb = (config.PUBLIC_BASE_URL.rstrip("/")
                       if config.PUBLIC_BASE_URL else _public_base().rstrip("/"))
                _disp_base = _fb
            else:
                _disp_base = None
            if _disp_base:
                _disp_url = f"{_disp_base}/twiml/dispatcher/{lead_id}"
                _call_result = messaging.place_call(biz, _owner_cell, _disp_url)
                if _call_result.get("status") == "placed":
                    from datetime import timezone as _tz_utc
                    _now_ts = datetime.now(_tz_utc.utc).isoformat()
                    db.set_dispatcher_call_at(lead_id, _now_ts)

    # F05: RSVP classification -- wire classify_rsvp into handle_inbound.
    rsvp = reminders.classify_rsvp(body)
    if rsvp == "yes":
        # Phase-4 C: Show-Up-Prepared — enrich context with lead address/project/summary.
        _rsvp_ctx = {"lead_id": lead_id, "name": lead.get("name"),
                     "phone": lead.get("phone"), "when": "confirmed (RSVP)"}
        if lead.get("address"):
            _rsvp_ctx["address"] = lead["address"]
        if lead.get("project_type"):
            _rsvp_ctx["project"] = lead["project_type"]
        if lead.get("summary"):
            _rsvp_ctx["summary"] = lead["summary"]
        alerts.notify_async(biz, "booking", _rsvp_ctx)
    elif rsvp == "no":
        # Owner notified; AI handles rebooking -- do NOT auto-cancel.
        alerts.notify_async(biz, "canceled", {"lead_id": lead_id, "name": lead.get("name"),
                                              "phone": lead.get("phone")})

    history = db.get_messages(lead_id)
    # Merge Google + Outlook busy slots; both return empty set when not connected.
    exclude = google_cal.busy_slot_ids(biz["id"]) | outlook_cal.busy_slot_ids(biz["id"])
    reply, booking = ai.generate_reply(biz, history, exclude_slot_ids=exclude,
                                       lead_id=lead_id)
    db.add_message(lead_id, "out", reply)
    booked = None
    if booking:
        gday, gtime = db.parse_day(booking), db.time_key(booking)
        prior = db.lead_booked_appointments(biz["id"], lead_id)
        if any(a.get("day") == gday and a.get("slot_time") == gtime for a in prior):
            booked = booking  # already holds this exact slot -> a re-confirmation
        # Reserve the new slot first. book_appointment returns False if that
        # (day, time) is already taken by THIS business, so only claim a real win.
        elif db.book_appointment(biz["id"], lead_id, booking):
            booked = booking
            # A caller who books is a real customer: remember the number so a future
            # call from them is engaged, not mistaken for a cold lead (never
            # overrides an owner-set personal/vendor/blocked tag).
            db.learn_customer(biz["id"], lead.get("phone"), lead.get("name"))
            # They booked: stop any queued growth chase (quote follow-up / reactivation),
            # same auto-pause the command-center booking does.
            db.cancel_lead_growth_touches(lead_id, ("quote_followup", "reactivation"))
            # Phase 5e S4: Also cancel any pending follow-up touches -- belt-and-suspenders
            # with the live-status check in run_due_once.
            try:
                db.cancel_pending_followup_touches(lead_id)
            except Exception as _cfe2:
                import sys as _sys2
                print(f"[firstback] cancel_pending_followup_touches (booking) failed (lead {lead_id}): {_cfe2}",
                      file=_sys2.stderr, flush=True)
            # Reschedule: now that the new slot is held, release the lead's old
            # estimate(s) so a re-book never double-books or orphans a slot.
            for a in prior:
                db.cancel_appointment(biz["id"], a["id"])
            # Phase-4 C: Show-Up-Prepared — enrich context with lead address/project/summary.
            _book_ctx = {"lead_id": lead_id, "name": lead.get("name"),
                         "phone": lead.get("phone"), "when": booking}
            if lead.get("address"):
                _book_ctx["address"] = lead["address"]
            if lead.get("project_type"):
                _book_ctx["project"] = lead["project_type"]
            if lead.get("summary"):
                _book_ctx["summary"] = lead["summary"]
            alerts.notify_async(biz, "booking", _book_ctx)
            # Phase-4 C: Milestone hook — check if this booking crosses the ROI
            # Progressive ROI milestone (plan 07-3): highest newly-crossed level, idempotent.
            _fire_roi_milestone(biz)
            # Mirror onto Google Calendar + queue the pre-estimate reminder (both
            # best-effort, off the hot path; no-ops unless configured).
            if gday and gtime:
                try:
                    from config import biz_tz as _biz_tz
                    _tz = _biz_tz(biz)
                except (ImportError, AttributeError):
                    _tz = config.app_tz()
                appt = db.find_appointment(biz["id"], lead_id, gday, gtime)
                appt_id = appt["id"] if appt else None
                google_cal.create_event_async(
                    biz["id"], appt_id,
                    f"Estimate: {lead['name']}",
                    f"FirstBack booked a free estimate for {lead['name']} ({lead['phone']}).",
                    gday, gtime, tz=_tz)
                # Plan 14: mirror onto Outlook Calendar (additive, guarded, daemon thread).
                if outlook_cal.is_connected(biz["id"]):
                    outlook_cal.create_event_async(
                        biz["id"], appt_id,
                        f"Estimate: {lead['name']}",
                        f"FirstBack booked a free estimate for {lead['name']} ({lead['phone']}).",
                        gday, gtime, tz=_tz)
                reminders.enqueue_reminder(biz, lead, gday, gtime)
                reminders.enqueue_morning_reminder(biz, lead, gday, gtime)
                # Plan 13: push to Jobber as a quote request (additive, guarded daemon thread).
                fsm_sync.push_booking_async(biz["id"], appt_id, lead, {"day": gday, "when": booking})
        else:
            # F03 double-booking recovery: slot was taken between turns.
            # Generate a recovery reply offering the next open slot.
            recovery_history = db.get_messages(lead_id)
            recovery_msg = {"direction": "in", "body":
                "I'm sorry, that slot is no longer available. Could you please suggest "
                "a different time?"}
            recovery_history_ext = list(recovery_history) + [recovery_msg]
            try:
                recovery_reply, _ = ai.generate_reply(
                    biz, recovery_history_ext,
                    exclude_slot_ids=exclude, lead_id=lead_id)
                # Replace the already-recorded out reply.
                db.add_message(lead_id, "out", recovery_reply)
                reply = recovery_reply
            except Exception as _e:
                import sys
                print(f"[firstback] double-booking recovery failed: {_e}",
                      file=sys.stderr, flush=True)
    # Precompute notes off the hot path (after booking, so a booked lead is summed
    # as 'scheduled'); never blocks this turn.
    _schedule_notes(lead_id)
    return reply, booked, urgent


@app.route("/api/sim/incoming", methods=["POST"])
@login_required
def sim_incoming():
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    data = request.get_json(silent=True) or {}  # name/phone/scenario optional
    biz = current_business()
    scenario = (data.get("scenario") or "prospect").strip().lower()
    # Spam / known-caller demos show the SCREEN in action: a representative verdict,
    # no lead created and no text "sent" -- so contractors can watch FirstBack skip a
    # robocaller or hand a saved contact back to them, exactly as it does live.
    if scenario in ("spam", "known"):
        if scenario == "spam":
            score, reasons = triage.spam_score(
                {"attestation": "TN-Validation-Failed-C", "neighbor_spoof": True,
                 "line_type": "nonFixedVoip", "behavior": {"missed_calls": 4}})
            return jsonify(screened=True, status="screened_spam", label="Spam",
                           score=score, reasons=reasons)
        return jsonify(screened=True, status="trusted", label="Known caller", score=0,
                       reasons=["You’ve worked with this caller before — FirstBack leaves "
                                "them to you instead of sending an automated text."])
    lead_id = db.create_lead(biz["id"], data.get("name") or "New Caller",
                             data.get("phone") or "+1 (555) 000-0000")
    reply = open_conversation(biz, db.get_lead(lead_id))
    return jsonify(lead_id=lead_id, reply=reply)


@app.route("/api/sim/reply", methods=["POST"])
@login_required
def sim_reply():
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    data, err = _get_json("lead_id", "body")
    if err:
        return err
    biz = current_business()
    lead = db.get_lead(data["lead_id"], biz["id"])  # ownership-scoped
    if not lead:
        return jsonify(error="Lead not found."), 404
    reply, booked, urgent = handle_inbound(biz, lead, data["body"])
    return jsonify(reply=reply, booked=booked, urgent=urgent)


@app.route("/api/leads")
@login_required
def api_leads():
    return jsonify(db.list_leads(current_business()["id"]))


@app.route("/api/leads/<int:lead_id>/messages")
@login_required
def api_lead_messages(lead_id):
    # Instant + read-only: notes are precomputed on the write path, so opening a
    # lead never triggers an LLM call or writes to the DB. Scoped to the business.
    lead = db.get_lead(lead_id, current_business()["id"])
    if not lead:
        return jsonify(error="Lead not found."), 404
    return jsonify(lead=lead, messages=db.get_messages(lead_id))


@app.route("/api/appointments")
@login_required
def api_appointments():
    return jsonify(db.list_appointments(current_business()["id"]))


# ---- Scheduling: the in-house calendar + provider connections ----
@app.route("/api/calendar")
@login_required
def api_calendar():
    month = request.args.get("month", "")
    now = datetime.now()
    try:
        y, m = (int(x) for x in month.split("-"))
        datetime(y, m, 1)  # validate
    except (ValueError, TypeError):
        y, m = now.year, now.month
    return jsonify(db.calendar_month(current_business()["id"], y, m))


@app.route("/api/calendar/busy", methods=["POST"])
@login_required
def api_calendar_busy():
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    data, err = _get_json("date")
    if err:
        return err
    busy = bool(data.get("busy"))
    db.set_day_busy(current_business()["id"], data["date"], busy)
    return jsonify(date=data["date"], busy=busy)


@app.route("/api/integrations", methods=["POST"])
@login_required
def api_integrations():
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    data, err = _get_json("provider")
    if err:
        return err
    connected = db.set_integration(current_business()["id"], data["provider"],
                                   bool(data.get("connected")))
    return jsonify(provider=data["provider"], connected=connected)


# ---- Real Google Calendar OAuth (gated on GOOGLE_CLIENT_ID/SECRET) ----
@app.route("/api/calendar/google/connect")
@login_required
def google_connect():
    if not google_cal.configured():
        return redirect("/settings?gerror=unconfigured")
    state = secrets.token_urlsafe(16)
    session["g_state"] = state  # CSRF guard, verified on callback
    return redirect(google_cal.auth_url(state))


@app.route("/api/calendar/google/callback")
@login_required
def google_callback():
    expected = session.pop("g_state", None)
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/settings?gerror=denied")
    if not expected or request.args.get("state") != expected:
        return redirect("/settings?gerror=state")
    try:
        google_cal.connect_with_code(current_business()["id"], request.args["code"])
    except Exception as e:
        print(f"[firstback] google connect failed: {e}", file=sys.stderr, flush=True)
        return redirect("/settings?gerror=exchange")
    return redirect("/settings?gconnected=1")


@app.route("/api/calendar/google/disconnect", methods=["POST"])
@login_required
def google_disconnect():
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    google_cal.disconnect(current_business()["id"])
    return jsonify(connected=False)


# ---- Real Outlook Calendar OAuth (gated on MICROSOFT_CLIENT_ID/SECRET) ----
@app.route("/api/calendar/outlook/connect")
@login_required
def outlook_connect():
    if not outlook_cal.configured():
        return redirect("/settings?olerror=unconfigured")
    state = secrets.token_urlsafe(16)
    session["ol_state"] = state  # CSRF guard, verified on callback
    return redirect(outlook_cal.auth_url(state))


@app.route("/api/calendar/outlook/callback")
@login_required
def outlook_callback():
    expected = session.pop("ol_state", None)
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/settings?olerror=denied")
    if not expected or request.args.get("state") != expected:
        return redirect("/settings?olerror=state")
    try:
        outlook_cal.connect_with_code(current_business()["id"], request.args["code"])
    except Exception as e:
        print(f"[firstback] outlook connect failed: {e}", file=sys.stderr, flush=True)
        return redirect("/settings?olerror=exchange")
    return redirect("/settings?olconnected=1")


@app.route("/api/calendar/outlook/disconnect", methods=["POST"])
@login_required
def outlook_disconnect():
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    outlook_cal.disconnect(current_business()["id"])
    return jsonify(connected=False)


@app.route("/api/appointments/<int:appt_id>/cancel", methods=["POST"])
@login_required
def api_cancel_appointment(appt_id):
    """Owner-initiated cancel from the dashboard. Frees the slot + cancels its
    reminders (db.cancel_appointment), then texts the customer a heads-up (simulated
    until Twilio, recorded on the thread). F04: also cancels the Google Calendar event
    asynchronously when one was created. Scoped to the owner's business."""
    biz = current_business()
    # Pre-deploy A1: this both mutates state AND sends the customer a cancellation SMS --
    # a forged cross-site POST must not be able to cancel an estimate + text a customer.
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    appt = db.cancel_appointment(biz["id"], appt_id)
    if not appt:
        return jsonify(error="Appointment not found."), 404
    # F04: cancel the Google Calendar event if one was created for this appointment.
    google_event_id = appt.get("google_event_id")
    if google_event_id and google_cal.is_connected(biz["id"]):
        google_cal.cancel_event_async(biz["id"], google_event_id)
    # Plan 14: cancel the Outlook Calendar event if one was created.
    outlook_event_id = appt.get("outlook_event_id")
    if outlook_event_id and outlook_cal.is_connected(biz["id"]):
        outlook_cal.cancel_event_async(biz["id"], outlook_event_id)
    when = fmt_slot_when(appt.get("day"), appt.get("slot_time")) or appt.get("scheduled_for") or "your estimate"
    lead = db.get_lead(appt["lead_id"], biz["id"])
    if lead and (lead.get("phone") or "").strip():
        messaging.send_sms(
            biz, lead["phone"],
            f"Your free estimate {when} has been canceled. Reply here any time to rebook.",
            lead_id=lead["id"])
    return jsonify(ok=True, when=when)


# ---- Caller triage: the owner's contact directory + screened-call override ----
@app.route("/api/contacts")
@login_required
def api_contacts():
    """The owner-managed screening directory (personal/vendor/blocked) + a count of
    auto-recognized customers. Powers the Settings 'Caller screening' card."""
    rows = db.list_contacts(current_business()["id"])
    managed = [r for r in rows if r["category"] in ("personal", "vendor", "blocked")]
    customers = sum(1 for r in rows if r["category"] == "customer")
    return jsonify(managed=managed, customers=customers)


@app.route("/api/contacts", methods=["POST"])
@login_required
def api_contacts_add():
    """Tag a number so FirstBack never cold-texts it. Only the owner-set categories
    are accepted (customer/prospect are learned automatically, never set by hand)."""
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    data, err = _get_json("number", "category")
    if err:
        return err
    if data["category"] not in ("personal", "vendor", "blocked"):
        return jsonify(error="Choose personal, vendor, or blocked."), 400
    if len(re.sub(r"\D", "", str(data["number"]))) < 7:
        return jsonify(error="Enter a valid phone number."), 400
    db.set_contact(current_business()["id"], data["number"], data["category"],
                   name=(data.get("name") or "").strip() or None)
    return jsonify(ok=True)


@app.route("/api/contacts/delete", methods=["POST"])
@login_required
def api_contacts_delete():
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    data, err = _get_json("number")
    if err:
        return err
    db.delete_contact(current_business()["id"], data["number"])
    return jsonify(ok=True)


@app.route("/api/calls/<int:call_id>/engage", methods=["POST"])
@login_required
def api_engage_screened_call(call_id):
    """Owner override from the dashboard: a screened caller was actually worth
    reaching. Forget the screen tag, then engage exactly like a fresh missed call
    (open the conversation + send the text-back). Refuses an opted-out number, so the
    override can never re-text someone who replied STOP."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    call = db.get_call(call_id, biz["id"])  # tenant-scoped
    if not call:
        return jsonify(error="Call not found."), 404
    caller = (call.get("from_number") or "").strip()
    if not caller:
        return jsonify(error="No caller number on file."), 400
    if db.is_suppressed(biz["id"], caller):
        return jsonify(error="This caller opted out, so we can’t text them."), 400
    db.delete_contact(biz["id"], caller)  # no longer a screened non-prospect
    lead = (db.get_lead_by_phone(biz["id"], caller)
            or db.get_lead(db.create_lead(biz["id"], "New Caller", caller)))
    if not db.get_messages(lead["id"]):
        reply = open_conversation(biz, lead)    # records the thread + alerts the owner
        messaging.send_sms(biz, caller, reply)  # transmit (already recorded)
    db.mark_call_engaged(call_id, lead["id"], business_id=biz["id"])
    return jsonify(ok=True, lead_id=lead["id"])


@app.route("/api/calls/<int:call_id>/real", methods=["POST"])
@login_required
def api_rescue_screened_call(call_id):
    """Owner override from the dashboard: a screened caller was actually a real customer.
    Records the rescue (marks them trusted + resets the observation window so graduation
    is deferred), then re-engages exactly like api_engage_screened_call: open the
    conversation + send the text-back if the thread is empty, mark the call engaged.
    Tenant-scoped; refuses an opted-out number (never re-text a STOP)."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    call = db.get_call(call_id, biz["id"])   # tenant-scoped
    if not call:
        return jsonify(error="Call not found."), 404
    caller = (call.get("from_number") or "").strip()
    if not caller:
        return jsonify(error="No caller number on file."), 400
    if db.is_suppressed(biz["id"], caller):
        return jsonify(error="This caller opted out, so we can't text them."), 400
    # Core contract: upsert as customer + increment false_positives + reset window.
    db.record_screening_rescue(biz["id"], caller)
    # Re-engage: create/find the lead and send the text-back only on an empty thread.
    lead = (db.get_lead_by_phone(biz["id"], caller)
            or db.get_lead(db.create_lead(biz["id"], "New Caller", caller)))
    if not db.get_messages(lead["id"]):
        reply = open_conversation(biz, lead)    # records the thread + alerts the owner
        messaging.send_sms(biz, caller, reply)  # transmit (already recorded)
    db.mark_call_engaged(call_id, lead["id"], business_id=biz["id"])
    return jsonify(ok=True, lead_id=lead["id"])


def _mark_number_spam(biz_id, number):
    """Owner says a caller is spam: tag the number 'blocked' for THIS business (so it's
    never cold-pitched again) and add a cross-tenant spam flag that helps pre-screen it
    for every other business (privacy-safe -- only a count is ever shared). Idempotent.
    Shared by the call-log and conversation-panel 'Mark spam' actions."""
    db.set_contact(biz_id, number, "blocked", source="owner-spam")
    db.add_spam_flag(biz_id, number)


@app.route("/api/calls/<int:call_id>/flag-spam", methods=["POST"])
@login_required
def api_flag_call_spam(call_id):
    """'Mark spam' from the dashboard screened-calls strip."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    call = db.get_call(call_id, biz["id"])   # tenant-scoped
    if not call:
        return jsonify(error="Call not found."), 404
    caller = (call.get("from_number") or "").strip()
    if not caller:
        return jsonify(error="No caller number on file."), 400
    _mark_number_spam(biz["id"], caller)
    return jsonify(ok=True)


@app.route("/api/leads/<int:lead_id>/flag-spam", methods=["POST"])
@login_required
def api_flag_lead_spam(lead_id):
    """'Mark spam' from the conversation panel: block the lead's number + feed the
    cross-tenant ledger, so FirstBack stops cold-pitching this caller. Tenant-scoped."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    lead = db.get_lead(lead_id, biz["id"])   # ownership-scoped
    if not lead:
        return jsonify(error="Lead not found."), 404
    number = (lead.get("phone") or "").strip()
    if not number:
        return jsonify(error="No phone number on file for this lead."), 400
    _mark_number_spam(biz["id"], number)
    return jsonify(ok=True)


@app.route("/api/leads/<int:lead_id>/won", methods=["POST"])
@login_required
def api_mark_lead_won(lead_id):
    """E5 / 06-4: owner records the actual closed-job dollar amount for a lead.
    Form: {amount, _csrf}. Validates amount > 0 and tenant ownership (404 otherwise).
    Update allowed (correct a mis-entry). Returns {"status":"ok","won_amount": float}."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    lead = db.get_lead(lead_id, biz["id"])
    if not lead:
        return jsonify(error="Lead not found."), 404
    try:
        amount = float(request.form.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify(error="amount must be a positive number"), 400
    try:
        db.mark_lead_won(lead_id, amount)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="ok", won_amount=amount)


# ---- Caller triage: the suggestion / "for review" queue ----
@app.route("/api/suggestions")
@login_required
def api_suggestions():
    """One tab of the Callers review inbox (status = pending | accepted | dismissed),
    plus the counts for all three tabs. Read-only; suggestions are generated off the
    hot path (the ticker)."""
    bid = current_business()["id"]
    status = request.args.get("status", "pending")
    if status not in ("pending", "accepted", "dismissed"):
        status = "pending"
    counts = {s: db.count_suggestions(bid, s) for s in ("pending", "accepted", "dismissed")}
    return jsonify(suggestions=db.list_suggestions(bid, status), counts=counts, status=status)


@app.route("/api/suggestions/<int:sug_id>/accept", methods=["POST"])
@login_required
def api_suggestion_accept(sug_id):
    """Confirm a suggestion (optionally recategorized): write it to the directory and
    mark the suggestion accepted. The owner is always the one who decides."""
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    biz = current_business()
    sug = db.get_suggestion(sug_id, biz["id"])
    if not sug:
        return jsonify(error="Suggestion not found."), 404
    data = request.get_json(silent=True) or {}
    category = data.get("category") or sug["suggested_category"]
    if category not in ("personal", "vendor", "blocked", "customer"):
        return jsonify(error="Invalid category."), 400
    db.set_contact(biz["id"], sug["number"], category, name=sug.get("name"), source="suggested")
    db.set_suggestion_status(sug_id, "accepted", biz["id"])
    return jsonify(ok=True, category=category)


@app.route("/api/suggestions/<int:sug_id>/dismiss", methods=["POST"])
@login_required
def api_suggestion_dismiss(sug_id):
    """Dismiss a suggestion -- it won't be raised again for this number."""
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    biz = current_business()
    if not db.get_suggestion(sug_id, biz["id"]):
        return jsonify(error="Suggestion not found."), 404
    db.set_suggestion_status(sug_id, "dismissed", biz["id"])
    return jsonify(ok=True)


@app.route("/api/suggestions/<int:sug_id>/reopen", methods=["POST"])
@login_required
def api_suggestion_reopen(sug_id):
    """Undo: move a sorted/dismissed suggestion back to 'to review'. If it had been
    accepted, the directory entry that accept created is reverted too."""
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    biz = current_business()
    sug = db.get_suggestion(sug_id, biz["id"])
    if not sug:
        return jsonify(error="Suggestion not found."), 404
    if sug["status"] == "accepted":
        db.delete_contact(biz["id"], sug["number"])
    db.set_suggestion_status(sug_id, "pending", biz["id"])
    return jsonify(ok=True)


@app.route("/api/suggestions/bulk", methods=["POST"])
@login_required
def api_suggestions_bulk():
    """Apply one action (accept | dismiss | reopen) to many suggestions at once -- the
    bulk-select path that makes an imported address book tractable."""
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    biz = current_business()
    data, err = _get_json("ids", "action")
    if err:
        return err
    action = data["action"]
    if action not in ("accept", "dismiss", "reopen"):
        return jsonify(error="Invalid action."), 400
    ids = data["ids"] if isinstance(data["ids"], list) else []
    done = 0
    for sid in ids:
        sug = db.get_suggestion(sid, biz["id"]) if isinstance(sid, int) else None
        if not sug:
            continue
        if action == "accept":
            db.set_contact(biz["id"], sug["number"], sug["suggested_category"],
                           name=sug.get("name"), source="suggested")
            db.set_suggestion_status(sid, "accepted", biz["id"])
        elif action == "dismiss":
            db.set_suggestion_status(sid, "dismissed", biz["id"])
        else:  # reopen
            if sug["status"] == "accepted":
                db.delete_contact(biz["id"], sug["number"])
            db.set_suggestion_status(sid, "pending", biz["id"])
        done += 1
    return jsonify(ok=True, count=done)


# ---- Contact import: bulk-load an address book into the review queue ----
_MAX_IMPORT_BYTES = 5 * 1024 * 1024   # 5 MB is ample for a vCard / CSV export


@app.route("/api/contacts/import", methods=["POST"])
@login_required
def api_contacts_import():
    """Upload a vCard (.vcf) or CSV export -> parse -> pre-sort -> queue each contact
    as a PENDING suggestion in the review inbox. Nothing is screened automatically;
    the owner confirms (in bulk). Returns an import summary for the UI."""
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    biz = current_business()
    f = request.files.get("file")
    if not f or not (f.filename or "").strip():
        return jsonify(error="Choose a .vcf or .csv file to import."), 400
    raw = f.read(_MAX_IMPORT_BYTES + 1)
    if len(raw) > _MAX_IMPORT_BYTES:
        return jsonify(error="That file is too large (limit 5 MB)."), 413
    try:
        contacts = contact_import.parse_file(f.filename, raw)
    except Exception as e:
        print(f"[firstback] contact import parse failed: {e}", file=sys.stderr, flush=True)
        return jsonify(error="Could not read that file. Export a vCard (.vcf) or a CSV."), 400
    if not contacts:
        # S3: distinguish "no contact records at all" (400) from "contacts found but
        # none had phone numbers" (422 -- human-readable; owner needs to check their export).
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        has_records = (
            text.upper().count("BEGIN:VCARD") > 0
            or sum(1 for ln in text.splitlines() if ln.strip() and not ln.startswith(",")) > 1
        )
        if has_records:
            return jsonify(
                error="Your contacts were found but none had a phone number. "
                      "Try exporting your address book again with phone numbers included."
            ), 422
        return jsonify(error="No contacts with phone numbers were found in that file."), 400
    summary = contact_import.ingest(biz["id"], contacts, source="import-file")
    return jsonify(ok=True, **summary)


# ---- Google Contacts import (a SEPARATE OAuth connection from Calendar, gated) ----
@app.route("/api/contacts/google/connect")
@login_required
def google_contacts_connect():
    if not google_contacts.configured():
        return redirect("/callers?gcerror=unconfigured")
    state = secrets.token_urlsafe(16)
    session["gc_state"] = state           # CSRF guard, verified on callback
    return redirect(google_contacts.auth_url(state))


@app.route("/api/contacts/google/callback")
@login_required
def google_contacts_callback():
    expected = session.pop("gc_state", None)
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/callers?gcerror=denied")
    if not expected or request.args.get("state") != expected:
        return redirect("/callers?gcerror=state")
    try:
        google_contacts.connect_with_code(current_business()["id"], request.args["code"])
    except Exception as e:
        print(f"[firstback] google contacts connect failed: {e}", file=sys.stderr, flush=True)
        return redirect("/callers?gcerror=exchange")
    return redirect("/callers?gcsync=1")   # the UI auto-runs a first sync on return


@app.route("/api/contacts/google/sync", methods=["POST"])
@login_required
def google_contacts_sync():
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    biz = current_business()
    if not google_contacts.is_connected(biz["id"]):
        return jsonify(error="Connect Google Contacts first."), 400
    try:
        summary = google_contacts.sync(biz["id"])
    except Exception as e:
        print(f"[firstback] google contacts sync failed: {e}", file=sys.stderr, flush=True)
        return jsonify(error="Google Contacts sync failed. Please try again."), 502
    return jsonify(ok=True, **summary)


@app.route("/api/contacts/google/disconnect", methods=["POST"])
@login_required
def google_contacts_disconnect():
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    google_contacts.disconnect(current_business()["id"])
    return jsonify(connected=False)


# ---- Plan 13: FSM / Jobber OAuth + sync routes ----

@app.route("/api/fsm/jobber/connect")
@login_required
def fsm_jobber_connect():
    """Start the Jobber OAuth2 flow. Redirect to the Jobber authorization page."""
    if not jobber_fsm.configured():
        return redirect("/settings?fsmerror=unconfigured")
    state = secrets.token_urlsafe(16)
    session["fsm_j_state"] = state   # CSRF guard, verified + consumed on callback
    return redirect(jobber_fsm.auth_url(state))


@app.route("/api/fsm/jobber/callback")
@login_required
def fsm_jobber_callback():
    """Jobber OAuth2 callback: exchange the code for tokens and trigger a first sync."""
    expected = session.pop("fsm_j_state", None)
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/settings?fsmerror=denied")
    if not expected or request.args.get("state") != expected:
        return redirect("/settings?fsmerror=state")
    try:
        jobber_fsm.connect_with_code(current_business()["id"], request.args["code"])
    except Exception as e:
        print(f"[firstback] jobber connect failed: {e}", file=sys.stderr, flush=True)
        return redirect("/settings?fsmerror=exchange")
    # Kick off an immediate background sync now that we're connected.
    biz_id = current_business()["id"]
    threading.Thread(target=_fsm_background_sync, args=(biz_id,), daemon=True).start()
    return redirect("/settings?fsmconnected=1")


def _fsm_background_sync(business_id):
    """Run a sync_clients pass in a daemon thread (called after connect + /api/fsm/sync)."""
    try:
        result = fsm_sync.sync_clients(business_id)
        db.set_fsm_sync_stamp(business_id, datetime.now(timezone.utc).isoformat(),
                              result.get("clients_fetched", 0))
    except Exception as e:
        print(f"[firstback] fsm background sync error (biz {business_id}): {e}",
              file=sys.stderr, flush=True)


@app.route("/api/fsm/jobber/disconnect", methods=["POST"])
@login_required
def fsm_jobber_disconnect():
    """Disconnect Jobber for this business. Keeps already-synced contact suggestions."""
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    jobber_fsm.disconnect(current_business()["id"])
    return jsonify(connected=False)


# ---- Plan 16: FSM / Housecall Pro OAuth + sync routes ----

@app.route("/api/fsm/hcp/connect")
@login_required
def fsm_hcp_connect():
    """Start the Housecall Pro OAuth2 flow. Redirect to the HCP authorization page."""
    if not hcp_fsm.configured():
        return redirect("/settings?hcperror=unconfigured")
    state = secrets.token_urlsafe(16)
    session["fsm_h_state"] = state   # CSRF guard, verified + consumed on callback
    return redirect(hcp_fsm.auth_url(state))


@app.route("/api/fsm/hcp/callback")
@login_required
def fsm_hcp_callback():
    """Housecall Pro OAuth2 callback: exchange the code for tokens and trigger a first sync."""
    expected = session.pop("fsm_h_state", None)
    if request.args.get("error") or not request.args.get("code"):
        return redirect("/settings?hcperror=denied")
    if not expected or request.args.get("state") != expected:
        return redirect("/settings?hcperror=state")
    try:
        hcp_fsm.connect_with_code(current_business()["id"], request.args["code"])
    except Exception as e:
        print(f"[firstback] hcp connect failed: {e}", file=sys.stderr, flush=True)
        return redirect("/settings?hcperror=exchange")
    # Kick off an immediate background sync now that we're connected.
    biz_id = current_business()["id"]
    threading.Thread(target=_fsm_background_sync, args=(biz_id,), daemon=True).start()
    return redirect("/settings?hcpconnected=1")


@app.route("/api/fsm/hcp/disconnect", methods=["POST"])
@login_required
def fsm_hcp_disconnect():
    """Disconnect Housecall Pro for this business. Keeps already-synced contact suggestions."""
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    hcp_fsm.disconnect(current_business()["id"])
    return jsonify(connected=False)


@app.route("/api/fsm/sync", methods=["POST"])
@login_required
def fsm_sync_now():
    """Trigger an immediate FSM client sync for this business (Jobber or HCP)."""
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    biz = current_business()
    # FIX-10: provider-neutral checks — works for both Jobber and HCP.
    if not fsm_sync.configured():
        return jsonify(error="No FSM provider is configured."), 400
    if fsm_sync._get_active_provider(biz["id"]) is None:
        return jsonify(error="Connect an FSM provider (Jobber or Housecall Pro) first."), 400
    try:
        result = fsm_sync.sync_clients(biz["id"])
        db.set_fsm_sync_stamp(biz["id"], datetime.now(timezone.utc).isoformat(),
                              result.get("clients_fetched", 0))
    except Exception as e:
        print(f"[firstback] fsm sync_now error (biz {biz['id']}): {e}",
              file=sys.stderr, flush=True)
        return jsonify(error="Sync failed. Please try again."), 502
    return jsonify(ok=True, **result)


# ---- Phase 1: usage fuel gauge ----
@app.route("/api/usage")
@login_required
def api_usage():
    """Usage gauge data for the fuel-gauge surface. Returns conversations consumed/remaining
    (from the usage_grants table) and today's AI spend. Trades language only -- no
    'credits', 'grants', 'bundle', 'Twilio', or 'A2P' words reach the surface."""
    biz = current_business()
    bid = biz["id"]
    remaining, grant = db.conversations_remaining(bid)
    granted = int(grant.get("conversations_granted") or 0) if grant else None
    consumed = (granted - remaining) if (granted is not None and remaining is not None) else None
    # The allotment refills on the 1st of next month (monthly cadence for monthly AND annual plans).
    period_end = None
    if grant:
        from datetime import timezone as _tz
        _now = datetime.now(_tz.utc)
        period_end = (f"{_now.year + 1:04d}-01-01" if _now.month == 12
                      else f"{_now.year:04d}-{_now.month + 1:02d}-01")
    spend_today = db.get_llm_spend_today(bid)
    cap = config.CLAUDE_DAILY_COST_CAP_USD
    over_cap = ai.is_over_daily_cap(bid)
    return jsonify({
        "conversations_used": consumed,
        "conversations_total": granted,
        "conversations_remaining": remaining,
        "period_ends": period_end,
        "spend_today_usd": round(spend_today, 4),
        "daily_cap_usd": cap,
        "over_daily_cap": over_cap,
        "has_plan": grant is not None,
    })


def _cancel_estimate_for(biz, caller):
    """Cancel a caller's booked estimate(s) by phone (frees the slot, cancels
    reminders, alerts the owner). Returns True if anything was canceled, so the SMS
    handler can tell 'canceled an estimate' apart from 'nothing to cancel'."""
    lead = db.get_lead_by_phone(biz["id"], caller)
    if not lead:
        return False
    appts = db.lead_booked_appointments(biz["id"], lead["id"])
    if not appts:
        return False
    when = ""
    for a in appts:
        canceled = db.cancel_appointment(biz["id"], a["id"])
        if canceled and not when:
            when = fmt_slot_when(canceled.get("day"), canceled.get("slot_time")) or ""
    alerts.notify_async(biz, "canceled", {"lead_id": lead["id"], "name": lead.get("name"),
                                          "phone": lead.get("phone"), "when": when})
    return True


# ---- Twilio webhooks: the real callback system (Phase 1) ----
# Dormant until a number is provisioned and pointed here. All four are
# signature-verified so only Twilio can invoke them. The voice flow rings the
# contractor's cell and, on a miss, fires the instant text-back; inbound SMS runs
# the SAME conversation engine the simulator uses (so it books estimates, alerts
# the owner, and queues reminders for free). See CALLBACK_SYSTEM_PLAN.md.
_MISSED_DIAL = {"no-answer", "busy", "failed", "canceled"}
# "cancel" is handled separately (see twilio_sms_inbound): a booked customer almost
# always means "cancel my estimate", not "opt out of all texts" -- so we cancel the
# estimate when they have one, and only fall back to opt-out when they don't.
_STOP_WORDS = {"stop", "stopall", "stop all", "unsubscribe", "end",
               "quit", "optout", "opt out", "revoke"}
_HELP_WORDS = {"help", "info"}
_CALL_WORDS = {"call", "call me", "call me back", "callback", "call back",
               "yes call", "please call", "give me a call"}


def _xesc(s):
    """Minimal XML escaping for dynamic values placed inside TwiML."""
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _twiml(body):
    """Wrap a TwiML body in a response with the right content type."""
    return app.response_class('<?xml version="1.0" encoding="UTF-8"?>' + body,
                              mimetype="text/xml")


def _public_base():
    """The public origin Twilio reached us on (honoring a TLS-terminating proxy),
    used to build absolute callback URLs that match what Twilio signs."""
    proto = request.headers.get("X-Forwarded-Proto") or request.scheme
    return f"{proto}://{request.host}"


def _caller_behavior(biz_id, caller):
    """This caller's behavioral aggregates {missed_calls, inbound_msgs, booked} for the
    spam score, or {} if we've never seen them. Pulled from the same signals that drive
    the suggestion inbox, filtered to the one number."""
    key = db._digits10(caller)
    if not key:
        return {}
    for s in db.caller_signals(biz_id):
        if s.get("number") == key:
            return s
    return {}


def _effective_reputation_enabled(biz):
    """True only when the paid reputation provider is configured AND this business has
    the per-tenant toggle on. Parallel to _effective_screen_mode: a configured provider
    with the toggle off (or unset) must NOT fire paid lookups. The toggle defaults 0
    (off) so existing tenants are never charged without opting in."""
    return reputation.configured() and bool((biz or {}).get("reputation_enabled"))


def _screen_missed_caller(biz, caller):
    """Run the full 'phone screen' for a missed caller: gather the layered signals
    (STIR/SHAKEN attestation off the request, neighbor-spoof, behavior, crowdsource,
    and -- only if the free tiers leave it ambiguous -- a paid reputation lookup), then
    return triage.screen_caller's verdict. Reputation is consulted lazily so the common
    (clean or obviously-known) case never pays for or waits on a network call.

    Per-tenant thresholds: screen_hard/screen_mid override the app defaults when set,
    so an owner can make screening stricter or more relaxed without a config deploy."""
    attestation = request.form.get("StirVerstat") if request else None
    nspoof = triage.neighbor_spoof(biz.get("twilio_number") or biz.get("phone"), caller)
    behavior = _caller_behavior(biz["id"], caller)
    hard = biz.get("screen_hard") or SCREEN_SCORE_HARD
    mid = biz.get("screen_mid") or SCREEN_SCORE_MID
    verdict = triage.screen_caller(biz["id"], caller, attestation=attestation,
                                   neighbor_spoof=nspoof, behavior=behavior,
                                   hard=hard, mid=mid)
    # Only an UNKNOWN caller in the ambiguous band is worth a paid lookup: identity
    # tiers already settled the rest, and a clean/known caller shouldn't cost anything.
    # Gate on per-tenant toggle: even when the provider is configured, only call it
    # if this business has reputation_enabled set (paid-tier opt-in).
    if (_effective_reputation_enabled(biz) and verdict["status"] in ("prospect", "review")
            and verdict["score"] >= mid - 20):
        rep = reputation.lookup(caller)
        if rep:
            verdict = triage.screen_caller(biz["id"], caller, attestation=attestation,
                                           neighbor_spoof=nspoof, behavior=behavior,
                                           reputation=rep, hard=hard, mid=mid)
    return verdict


def _effective_screen_mode(biz):
    """This business's screening mode: its own setting (off|monitor|enforce) when chosen
    in Settings, otherwise the app-wide config.SCREEN_MODE default. NULL/blank -> inherit."""
    m = (biz or {}).get("screen_mode")
    return m if m in ("off", "monitor", "enforce") else SCREEN_MODE


def _missed_call_textback(biz, caller, call_sid="", dial_status=""):
    """Shared missed-call handling: SCREEN the caller, persist the verdict on the call
    log, then (only if they're worth engaging) find/create their lead and -- when the
    thread is empty -- generate + send the instant text-back. Returns True if we
    engaged, False if screened out (so the voice prompt stays honest about whether a
    text actually went out).

    Screened-out cases get NO bot text: opted-out (STOP), a known non-prospect, a
    KNOWN/saved caller the owner handles personally (faithful-Apple), or a near-certain
    spam/robocaller. Everything else engages; an ambiguous unknown engages but is
    tagged 'review' for the owner.

    Rollout-safe via the business's effective mode: 'off' engages everyone; 'monitor'
    computes + LOGS the verdict but still texts everyone (so the owner can review what it
    WOULD screen before it can ever silence a real caller); 'enforce' acts on it."""
    mode = _effective_screen_mode(biz)
    if mode != "off":
        verdict = _screen_missed_caller(biz, caller)
    else:   # screening off -> the original behavior: engage everyone we can text
        verdict = {"engage": True, "status": "prospect", "score": 0,
                   "category": "prospect", "reasons": []}
    reasons = "; ".join(verdict.get("reasons") or []) or None
    common = dict(from_number=caller, to_number=biz.get("twilio_number") or "",
                  dial_status=dial_status, missed=1, category=verdict["category"],
                  screen_status=verdict["status"], spam_score=verdict.get("score"),
                  screen_reasons=reasons, screen_mode=(mode if mode != "off" else None))
    # Monitor mode observes but never blocks: a screened verdict is still texted, so the
    # owner can trust the numbers before enforcing. Only 'enforce' actually suppresses.
    if not verdict["engage"] and mode == "enforce":
        db.log_call(biz["id"], call_sid, engaged=0, **common)
        # Batch B: a TRUSTED past customer was intentionally not auto-texted (the owner
        # handles them personally) -- but a silent drop means the owner may never know they
        # called. Alert the owner so they can ring back. Spam/opted-out callers get NO alert.
        if verdict.get("status") == "trusted":
            _kc_lead = db.get_lead_by_phone(biz["id"], caller)
            alerts.notify_async(biz, "lead", {
                "lead_id": _kc_lead["id"] if _kc_lead else None,
                "name": _kc_lead.get("name") if _kc_lead else None,
                "phone": caller, "known": True})
        return False
    lead = db.get_lead_by_phone(biz["id"], caller)
    if not lead:
        lead = db.get_lead(db.create_lead(biz["id"], "New Caller", caller))
    db.log_call(biz["id"], call_sid, lead_id=lead["id"], engaged=1, **common)
    # Greet only an empty thread, so a repeat missed call mid-conversation does not
    # re-introduce us (the owner is still alerted via the 'lead' alert + call log).
    if not db.get_messages(lead["id"]):
        reply = open_conversation(biz, lead)    # records the thread + alerts the owner
        result = messaging.send_sms(biz, caller, reply)  # transmit (already recorded)
        # F1 (honesty): the voice prompt keys off this return. True ONLY when a text
        # actually went out this call. When the send is blocked (A2P not yet approved),
        # suppressed (opted out), deferred, or errors, no text reached the caller -- the
        # lead is still captured + the owner alerted, and a blocked send flushes on A2P
        # approval -- but the caller must NOT be told "we sent you a text."
        return (result or {}).get("status") in ("sent", "simulated")
    return False   # repeat call on an existing thread: no NEW text went out this call


def _connect_inbound_to_ai(biz, caller, call_sid, verdict=None):
    """Plan 17: attempt to connect an inbound missed call to the AI ConversationRelay.

    Gates (any fail -> return None -> caller falls through to _missed_call_textback):
      1. VOICE_PUBLIC_URL is set (voice service deployed).
      2. biz.inbound_voice_enabled == 1 (per-business opt-in; default 0/off).
      3. Monthly voice spend is under config.VOICE_MONTHLY_CAP_CENTS.
      4. FIX-3: uses the passed-in screening verdict (do NOT re-screen here).
         If the caller is confirmed spam in enforce mode, bail.
      Q3: health-probe GET VOICE_PUBLIC_URL with a tight 400ms timeout; on
         ConnectionError or Timeout returns None (caller gets text-back, no dead air).

    On success: logs the call (FIX-2), opens a voice_calls row, builds the
    /twiml URL (FIX-5: quote() on name AND greeting), and returns a <Redirect>
    TwiML that sends Twilio to the voice service's /twiml endpoint, which returns
    ConversationRelay TwiML (reusing build_twiml with the custom inbound greeting).
    Inbound greeting is AI-disclosed with no recording claim (FIX-6).
    """
    import requests as _req
    biz_id = biz["id"]

    # Gate 1: voice service must be deployed.
    if not VOICE_PUBLIC_URL:
        return None
    # Gate 2: per-business inbound AI answering must be enabled (default 0 = inert).
    if not biz.get("inbound_voice_enabled"):
        return None
    # Gate 3: monthly cap.
    if db.voice_spend_this_month(biz_id) >= config.VOICE_MONTHLY_CAP_CENTS:
        return None
    # Gate 4 (FIX-3): use the pre-computed verdict; don't re-screen (would charge again).
    if verdict is not None:
        mode = _effective_screen_mode(biz)
        if not verdict.get("engage") and mode == "enforce":
            return None

    # Q3 health probe: a tight preflight so we never hand the caller dead air when the
    # voice service is down. ConnectionError or Timeout -> fall through to text-back.
    try:
        _req.get(VOICE_PUBLIC_URL, timeout=0.4)
    except Exception:
        return None

    # Find or create the lead.
    lead = db.get_lead_by_phone(biz_id, caller)
    if not lead:
        lead = db.get_lead(db.create_lead(biz_id, "New Caller", caller))
    lead_id = lead["id"]

    # FIX-2: log the call so it appears in the call log + screening stats.
    biz_twilio_number = biz.get("twilio_number") or ""
    db.log_call(biz_id, call_sid, from_number=caller, to_number=biz_twilio_number,
                dial_status="ai-answered", missed=0, lead_id=lead_id, engaged=1)
    # Open the voice_calls metering row (closed by /webhooks/twilio/voice/status).
    db.insert_voice_call(biz_id, lead_id, call_sid)

    # FIX-6: inbound greeting -- AI disclosed, no recording claim.
    biz_name = biz.get("name") or "us"
    inbound_greeting = (
        f"Hi, you've reached {biz_name}. I'm an AI scheduling assistant"
        " -- I can get you booked for a free estimate right now."
        " What can we help you with?"
    )

    # FIX-5: URL-encode name AND greeting so special chars don't break the query string.
    # Twilio will GET this URL and execute the returned ConversationRelay TwiML (reusing
    # voice_service.build_twiml with greeting= so the inbound greeting is used).
    twiml_url = (
        VOICE_PUBLIC_URL.rstrip("/") + "/twiml"
        + "?biz=" + quote(str(biz_id))
        + "&lead=" + quote(str(lead_id))
        + "&name=" + quote(biz_name)
        + "&greeting=" + quote(inbound_greeting)
    )
    # Return a <Redirect> so Twilio fetches the voice service's /twiml endpoint and
    # executes the ConversationRelay TwiML it returns. This is the same mechanism used
    # by the outbound callback path (place_call uses twiml_url the same way).
    return _twiml(
        f'<Response><Redirect method="GET">{_xesc(twiml_url)}</Redirect></Response>'
    )


@app.route("/webhooks/twilio/voice/inbound", methods=["POST"])
@require_twilio_signature
def twilio_voice_inbound():
    """Inbound call to a FirstBack number: ring the contractor's cell; if there is
    no cell on file, treat it as missed right away and text the caller back.

    SF-7: if this call's SID matches the stored forwarding sentinel SID, confirm
    forwarding, clear the sentinel, record the probe, and hang up quietly. This is
    THE ONLY place forwarding_confirmed is set True (honesty rule [DECIDED])."""
    biz = db.get_business_by_twilio_number(request.form.get("To", ""))
    if not biz:
        return _twiml("<Response><Reject/></Response>")
    call_sid = request.form.get("CallSid", "")
    # SF-7 sentinel match: this call is the forwarding probe returning home.
    sentinel_sid = biz.get("forwarding_sentinel_sid")
    if sentinel_sid and call_sid and call_sid == sentinel_sid:
        db.set_forwarding_confirmed(biz["id"], True)
        db.set_forwarding_sentinel(biz["id"], None, None)   # clear sentinel
        db.set_forwarding_probe(biz["id"])                  # record probe time
        return _twiml("<Response><Hangup/></Response>")
    forward = biz.get("forward_to")
    if forward:
        action = _public_base() + "/webhooks/twilio/voice/dial-status"
        return _twiml(
            f'<Response><Dial answerOnBridge="true" timeout="18" action="{action}" '
            f'method="POST"><Number>{_xesc(forward)}</Number></Dial></Response>')
    # Hook B (plan 17): no forward_to set — try AI answering before text-back.
    # Sentinel is already handled above (returns Hangup); sentinel never reaches here.
    _hb_caller = request.form.get("From", "")
    _hb_verdict = (_screen_missed_caller(biz, _hb_caller)
                   if _effective_screen_mode(biz) != "off" else None)
    _hb_ai = _connect_inbound_to_ai(biz, _hb_caller, call_sid, _hb_verdict)
    if _hb_ai is not None:
        return _hb_ai
    # AI answering off/gated/failed — fall through to existing text-back.
    engaged = _missed_call_textback(biz, _hb_caller, call_sid, "no-forward")
    if engaged:
        return _twiml("<Response><Say>Sorry we missed you. We just sent you a text "
                      "message. Goodbye.</Say><Hangup/></Response>")
    # No text went out (screened, opted out, or A2P not yet live) -- stay honest + warm.
    return _twiml("<Response><Say>Sorry we missed you. We will be in touch "
                  "soon. Goodbye.</Say><Hangup/></Response>")


@app.route("/webhooks/twilio/voice/dial-status", methods=["POST"])
@require_twilio_signature
def twilio_voice_dial_status():
    """Fires when the dial leg ends. A no-answer/busy/failed means the contractor
    missed it -> fire the instant text-back. Otherwise the call was answered."""
    biz = db.get_business_by_twilio_number(request.form.get("To", ""))
    status = (request.form.get("DialCallStatus") or "").lower()
    if biz and status in _MISSED_DIAL:
        _from = request.form.get("From", "")
        _csid = request.form.get("CallSid", "")
        # Hook A (plan 17, FIX-1): only when the caller is still on the line.
        # 'canceled' means the caller hung up before we answered — do NOT open a
        # ConversationRelay session for a dead line; fall straight to text-back.
        if status != "canceled":
            _verdict = _screen_missed_caller(biz, _from) if _effective_screen_mode(biz) != "off" else None
            _ai_twiml = _connect_inbound_to_ai(biz, _from, _csid, _verdict)
            if _ai_twiml is not None:
                return _ai_twiml
        # If AI answering is off/gated/failed (or status==canceled), fall through to text-back.
        # _missed_call_textback re-screens internally; no double-screening risk here because
        # either we never screened (canceled / gate failed before verdict) or the verdict
        # was computed but the helper returned None (spam/cap) so text-back handles it fresh.
        engaged = _missed_call_textback(biz, _from, _csid, status)
        if engaged:
            # 10-1: opt-in voicemail capture. Offer the caller (single-party, standard
            # voicemail -- NOT a recorded live conversation) to leave a message that
            # FirstBack transcribes into the lead thread. Inert unless voicemail_enabled.
            if biz.get("voicemail_enabled"):
                _rec_cb = _public_base() + "/webhooks/twilio/voice/recording"
                return _twiml(
                    '<Response><Say>Sorry we missed you. We just sent you a text. You can '
                    'also leave a brief message after the tone.</Say>'
                    f'<Record maxLength="120" playBeep="true" transcribe="true" '
                    f'transcribeCallback="{_rec_cb}"/>'
                    '<Say>Thanks, we will be in touch. Goodbye.</Say></Response>')
            return _twiml("<Response><Say>Sorry we missed you. We just sent you a text "
                          "message. Goodbye.</Say><Hangup/></Response>")
        return _twiml("<Response><Say>Sorry we missed you. We will be in touch "
                      "soon. Goodbye.</Say><Hangup/></Response>")
    return _twiml("<Response><Hangup/></Response>")


@app.route("/webhooks/twilio/voice/recording", methods=["POST"])
@require_twilio_signature
def twilio_voice_recording():
    """10-1: Twilio posts here when a voicemail recording + transcription is ready. Creates
    a voicemail lead if needed, injects the transcript as an inbound message (recording URL
    on the SAME row -- no fake direction), and greets ONLY an empty thread (no double-greeting
    when the missed-call text-back already fired). Inert until voicemail is enabled + wired."""
    biz = db.get_business_by_twilio_number(request.form.get("To", ""))
    if not biz or not biz.get("voicemail_enabled"):
        return _twiml("<Response/>")   # inert unless the owner opted in
    caller = request.form.get("From", "")
    if not caller:
        return _twiml("<Response/>")
    transcript = (request.form.get("TranscriptionText") or "").strip()
    recording_url = request.form.get("RecordingUrl", "")
    lead = db.get_lead_by_phone(biz["id"], caller)
    if not lead:
        lead = db.get_lead(db.create_lead(biz["id"], "Voicemail", caller, source="voicemail"))
    if transcript:
        db.add_message(lead["id"], "in", f"[Voicemail] {transcript}", recording_url=recording_url)
    # B1: get_messages has no direction kwarg -> inline-filter for an existing outbound.
    has_outbound = any(m.get("direction") == "out" for m in db.get_messages(lead["id"]))
    if not has_outbound:
        reply = open_conversation(biz, lead)        # records the thread + alerts the owner
        messaging.send_sms(biz, caller, reply)      # transmit only (already recorded)
    return _twiml("<Response/>")


# ---- 10-2: Web-chat "Text us" widget (public lead intake; anti-abuse; A2P + opt-in gated) ----
_WIDGET_RATE: dict = collections.defaultdict(list)
_WIDGET_MAX = 5
_WIDGET_WINDOW = 3600


def _widget_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _widget_blocked(slug):
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
    key = f"{slug}:{ip}"
    cutoff = _time.monotonic() - _WIDGET_WINDOW
    _WIDGET_RATE[key] = [t for t in _WIDGET_RATE[key] if t > cutoff]
    if len(_WIDGET_RATE[key]) >= _WIDGET_MAX:
        return True
    _WIDGET_RATE[key].append(_time.monotonic())
    return False


def _biz_id_by_widget_slug(slug):
    """Business id for an ENABLED widget slug, else None (so the widget is inert when the
    owner hasn't opted in or the slug is unknown)."""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT id FROM businesses WHERE micro_site_slug=? AND widget_enabled=1",
        (slug,)).fetchone()
    conn.close()
    return row["id"] if row else None


@app.route("/widget.js")
def widget_js():
    """Serve the embeddable widget loader at the root path (the ?slug= is read client-side
    from the script's own src). Lets contractors paste a single firstback.app/widget.js tag."""
    return app.send_static_file("widget.js")


@app.route("/api/widget/<slug>/config.js")
def widget_config(slug):
    """Per-tenant widget config as a JS assignment (CORS-open, cached). Empty config when the
    slug is unknown or the widget isn't enabled, so the bubble simply never renders."""
    bid = _biz_id_by_widget_slug(slug)
    if not bid:
        return _widget_cors(app.response_class("window.__fb={};", mimetype="application/javascript"))
    biz = db.get_business(bid)
    cfg = {"slug": slug, "biz": biz.get("name") or "", "endpoint": "/webhooks/widget/lead"}
    resp = app.response_class(f"window.__fb={json.dumps(cfg)};", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return _widget_cors(resp)


@app.route("/webhooks/widget/lead", methods=["POST", "OPTIONS"])
def widget_lead():
    """Public lead intake from the embedded widget. No auth -- anti-abuse: rate-limited per
    (slug, IP), phone validated to E.164, de-duped by phone. Fires open_conversation + send_sms
    exactly like a missed call. The send is A2P-gated in messaging.send_sms (inert until live)."""
    if request.method == "OPTIONS":
        return _widget_cors(app.response_class("", status=204))
    data = request.get_json(silent=True) or {}
    slug = (data.get("slug") or "").strip()
    phone_raw = (data.get("phone") or "").strip()
    name = (data.get("name") or "Web Visitor").strip()[:80] or "Web Visitor"
    if not slug or not phone_raw:
        return _widget_cors(jsonify(error="Missing slug or phone.")), 400
    phone = messaging.to_e164(phone_raw)
    if not phone:
        return _widget_cors(jsonify(error="Invalid phone number.")), 400
    # Resolve the slug BEFORE the rate counter so probing fake slugs can't burn a real
    # visitor's window on a shared IP (the rate limit only governs real-slug submissions).
    bid = _biz_id_by_widget_slug(slug)
    if not bid:
        return _widget_cors(jsonify(error="Business not found.")), 404
    if _widget_blocked(slug):
        return _widget_cors(jsonify(error="Too many submissions. Try again later.")), 429
    biz = db.get_business(bid)
    lead = db.get_lead_by_phone(biz["id"], phone)
    if not lead:
        lead = db.get_lead(db.create_lead(biz["id"], name, phone, source="web_widget"))
    if not db.get_messages(lead["id"]):
        reply = open_conversation(biz, lead)        # records the thread + alerts the owner
        messaging.send_sms(biz, phone, reply)       # transmit only (already recorded)
    return _widget_cors(jsonify(ok=True))


@app.route("/webhooks/twilio/sms/inbound", methods=["POST"])
@require_twilio_signature
def twilio_sms_inbound():
    """An inbound text from a customer. Handles STOP/HELP, then runs the shared
    conversation engine and texts the AI's reply back."""
    biz = db.get_business_by_twilio_number(request.form.get("To", ""))
    if not biz:
        return _twiml("<Response/>")
    caller = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    norm = body.lower().strip(" .!")
    if norm == "cancel":
        # Ambiguous on purpose: cancel their estimate if they have one (and confirm);
        # only fall back to an opt-out when there's nothing to cancel.
        if _cancel_estimate_for(biz, caller):
            return _twiml("<Response><Message>Your estimate has been canceled. Reply "
                          "here any time to rebook, or STOP to unsubscribe.</Message></Response>")
        db.set_opt_out(biz["id"], caller, source="sms-cancel")
        db.set_voice_consent(biz["id"], caller, False)   # R1: revoke AI-voice consent on cancel->opt-out
        return _twiml("<Response><Message>You are unsubscribed and will not receive "
                      "more messages. Reply HELP for help.</Message></Response>")
    if norm in _STOP_WORDS:
        db.set_opt_out(biz["id"], caller, source="sms-stop")
        db.set_voice_consent(biz["id"], caller, False)   # R1: revoke AI-voice consent on STOP
        return _twiml("<Response><Message>You are unsubscribed and will not receive "
                      "more messages. Reply HELP for help.</Message></Response>")
    if compliance.detect_revocation(body):
        # Plain-language opt-out, not the exact keyword (2025 FCC any-reasonable-
        # means rule) -> honor it across SMS and voice.
        db.set_opt_out(biz["id"], caller, source="sms-nlu")
        db.set_voice_consent(biz["id"], caller, False)   # R1: revoke AI-voice consent on NLU revocation
        return _twiml("<Response><Message>Understood, we will stop messaging you. "
                      "Reply HELP for help.</Message></Response>")
    if norm in _HELP_WORDS:
        return _twiml(f"<Response><Message>{_xesc(biz.get('name') or 'FirstBack')}: "
                      "reply here about your free estimate. Reply STOP to "
                      "unsubscribe.</Message></Response>")
    # Phase 1 C — SF-6: START / re-subscribe branch. CTIA requires that a prior STOP
    # be reversible: if the caller sends START (or any recognized re-subscribe keyword),
    # clear the opt-out flag so they can receive messages again. This check runs BEFORE
    # the is_suppressed silent-drop so a suppressed user can actually re-opt-in.
    # consent.opt_in_nlu covers the exact START keyword plus common rephrasings.
    if consent.opt_in_nlu(body):
        db.set_opt_in(biz["id"], caller, source="sms-start")
        return _twiml(f"<Response><Message>You have been re-subscribed to messages from "
                      f"{_xesc(biz.get('name') or 'FirstBack')}. Reply STOP to "
                      f"unsubscribe again.</Message></Response>")
    # Growth tray reply: GO / SKIP / SKIP N from the business owner's cell only.
    # Runs BEFORE the normal handler so the owner's command is processed immediately.
    # R2 guard: if owner_cell is empty or unnormalized, is_owner stays False.
    owner_cell = messaging.to_e164((biz.get("alert_sms") or "").strip())
    is_owner = bool(owner_cell and caller and messaging.to_e164(caller) == owner_cell)
    if not owner_cell:
        is_owner = False
    if is_owner:
        tray_cmd = _parse_tray_reply(body)
        if tray_cmd:
            return _handle_tray_reply(biz, tray_cmd)
    contact = db.get_contact(biz["id"], caller)
    if (contact and contact.get("category") == "blocked") or \
            db.is_suppressed(biz["id"], caller) or not body:
        return _twiml("<Response/>")   # blocked, opted out, or nothing to act on -> silent
    lead = db.get_lead_by_phone(biz["id"], caller)
    if not lead:
        lead = db.get_lead(db.create_lead(biz["id"], "New Caller", caller))
    # Affirmative voice consent ("call me") -> place the AI voice callback, but only
    # when a voice service is deployed. The FCC treats AI voice as a robocall, so we
    # record the opt-in and only call AFTER the customer asks. If voice is off, fall
    # through and just answer by text.
    # Also honor the per-tenant voice_callback_enabled toggle (saved in Settings).
    # Default to ON (1) when the column is absent/None so an existing single tenant
    # is never silently disabled by a missing explicit toggle value.
    _voice_toggle_on = biz.get("voice_callback_enabled", 1) not in (0, False)
    if norm in _CALL_WORDS and VOICE_PUBLIC_URL and _voice_toggle_on:
        db.set_voice_consent(biz["id"], caller, True)
        # R2 pre-call guard (ordered -- any fail -> text reply, never call).
        # (i) Re-read consent after writing: concurrent STOP could have cleared voice_ok.
        _consent_row = db.get_consent(biz["id"], caller)
        if not _consent_row or not _consent_row.get("voice_ok"):
            messaging.send_sms(biz, caller, "Got it! We will follow up by text.")
            return _twiml("<Response/>")
        # (ii) Quiet hours: after-hours gate (existing).
        if not compliance.voice_allowed_now():
            return _twiml("<Response><Message>Thanks. It is currently after hours, so "
                          "we will call you during business hours. You can also keep "
                          "texting here any time.</Message></Response>")
        # (iii) Spam score gate: do not call spammers.
        _spam_signals = _caller_behavior(biz["id"], caller)
        _spam_score, _ = triage.spam_score(_spam_signals)
        if _spam_score >= SCREEN_SCORE_HARD:
            messaging.send_sms(biz, caller, "Got it! We will follow up by text.")
            return _twiml("<Response/>")
        # (iv) 60-minute de-dupe: avoid calling the same number twice in an hour.
        _last_call_ts = db.last_voice_call_at(biz["id"], caller)
        if _last_call_ts is not None:
            try:
                from datetime import timezone as _tz_vc, timedelta as _td_vc
                _last_dt = datetime.fromisoformat(_last_call_ts)
                if _last_dt.tzinfo is None:
                    _last_dt = _last_dt.replace(tzinfo=_tz_vc.utc)
                _age_min = (datetime.now(_tz_vc.utc) - _last_dt).total_seconds() / 60
                if _age_min < 60:
                    messaging.send_sms(biz, caller, "We already tried calling you -- "
                                       "we will try again shortly.")
                    return _twiml("<Response/>")
            except (TypeError, ValueError):
                pass  # malformed timestamp -> allow the call
        # (v) Monthly cost cap: alert Dave and skip if cap exceeded.
        _voice_spend = db.voice_spend_this_month(biz["id"])
        if _voice_spend >= config.VOICE_MONTHLY_CAP_CENTS:
            messaging.send_sms(biz, caller, "Got it! We will follow up by text.")
            alerts.notify_async(biz, "voice_cap", {
                "spend_cents": _voice_spend,
                "cap_cents": config.VOICE_MONTHLY_CAP_CENTS,
            })
            return _twiml("<Response/>")
        twiml_url = (VOICE_PUBLIC_URL.rstrip("/")
                     + f"/twiml?biz={biz['id']}&lead={lead['id']}&name={quote(biz.get('name') or '')}")
        # Pass add_amd=True to enable AMD/voicemail detection (Slice 3).
        # Use inspect to stay compatible with older test stubs that pre-date add_amd.
        import inspect as _inspect_vc
        _pc_params = _inspect_vc.signature(messaging.place_call).parameters
        if "add_amd" in _pc_params:
            res = messaging.place_call(biz, caller, twiml_url, add_amd=True)
        else:
            res = messaging.place_call(biz, caller, twiml_url)
        # Honesty: only claim "calling you now" when a real call was actually placed.
        # Simulated/error must not promise a call that isn't happening.
        if res.get("status") == "placed":
            _twilio_sid = res.get("sid", "")
            db.insert_voice_call(biz["id"], lead["id"], _twilio_sid)
            return _twiml("<Response><Message>Calling you now.</Message></Response>")
    # Tier 3 content screen (gated; off by default): on the caller's FIRST reply,
    # classify whether this is a real homeowner or noise (a sales pitch / survey /
    # wrong number / robocall). On a CONFIDENT non-prospect we bail mid-conversation
    # -- record what they said, propose 'blocked' in the review inbox for the owner to
    # confirm (we never auto-block on a guess), and stay silent rather than keep
    # cold-pitching. Fails open: the demo brain / any error classifies as prospect.
    if SCREEN_AI_CONTENT and not any(m["direction"] == "in" for m in db.get_messages(lead["id"])):
        intent = ai.classify_intent(biz, db.get_messages(lead["id"])
                                    + [{"direction": "in", "body": body}])
        if not intent["is_prospect"] and intent["confidence"] >= 0.7:
            db.add_message(lead["id"], "in", body)   # keep their words on the thread
            db.upsert_suggestion(biz["id"], caller, lead.get("name"), "blocked",
                                 f"AI screen: looks like {intent['label']}", "ai-content")
            return _twiml("<Response/>")             # bail: no cold-pitch reply
    reply, _booked, _urgent = handle_inbound(biz, lead, body)  # records + books + alerts
    messaging.send_sms(biz, caller, reply)  # transmit (already recorded)
    return _twiml("<Response/>")


@app.route("/webhooks/twilio/sms/status", methods=["POST"])
@require_twilio_signature
def twilio_sms_status():
    """Twilio delivery receipts -> reconcile the stored message's status.
    SF-4: on failed/undelivered, schedule an async retry (never sync-retry)."""
    msg_sid = request.form.get("MessageSid", "")
    msg_status = request.form.get("MessageStatus", "")
    db.set_message_delivery(msg_sid, msg_status)
    if msg_status in ("failed", "undelivered") and msg_sid:
        try:
            # The lookup JOINs the lead so business_id + the destination phone are
            # present (the messages table stores neither). Attempt count comes from
            # how many retries already exist for this lead in the recent window --
            # the messages row can't tell us retry depth -- which enforces the cap.
            row = db.get_message_by_provider_sid(msg_sid)
            if row and row.get("business_id") and row.get("lead_id"):
                attempt = db.count_sms_retries(row["lead_id"]) + 1
                biz = db.get_business(row["business_id"])
                if attempt <= 3:
                    from datetime import timezone as _tz, timedelta as _td
                    delay_s = {1: 30, 2: 120, 3: 600}.get(attempt, 600)
                    send_at = (datetime.now(_tz.utc) + _td(seconds=delay_s)).isoformat()
                    db.queue_sms_retry(
                        row["business_id"],
                        row["lead_id"],
                        row.get("lead_phone", ""),
                        row.get("body", ""),
                        attempt,
                        send_at,
                    )
                elif biz:
                    # Cap reached: stop retrying and tell the owner the text never landed.
                    alerts.notify_async(biz, "sms_fail", {
                        "lead_id": row.get("lead_id"),
                        "message_id": row.get("id"),
                    })
        except Exception as _e:
            print(f"[firstback] SF-4 retry enqueue failed ({msg_sid}): {_e}",
                  file=sys.stderr, flush=True)
    return _twiml("<Response/>")


# ---- Scheduler trigger for production (external cron) ----
# The in-process ticker dies with the process; behind a real web server you can
# instead (or also) hit this every minute from cron with the shared secret:
#   curl -fsS -X POST -H "X-Tasks-Secret: $FIRSTBACK_TASKS_SECRET" URL/tasks/run-due
# Disabled (always 403) until FIRSTBACK_TASKS_SECRET is set. Not login-required by
# design, so it's locked behind the secret header and constant-time compared.
@app.route("/tasks/run-due", methods=["POST"])
def tasks_run_due():
    sent = request.headers.get("X-Tasks-Secret", "")
    if not TASKS_SECRET or not secrets.compare_digest(sent, TASKS_SECRET):
        return jsonify(error="Forbidden."), 403
    out = reminders.tick_once()
    # Also refresh A2P registration status from Twilio so an approved campaign flips
    # tenants live without anyone clicking. Defensive: never breaks the reminder tick.
    try:
        out["a2p_synced"] = connections.a2p_sync_all()
    except Exception as e:
        print(f"[firstback] a2p_sync_all failed: {e}", file=sys.stderr, flush=True)
    return jsonify(out)


# ── Phase 1 A: Stripe billing routes ─────────────────────────────────────────
# /webhooks/stripe  — raw body required for signature verification; no auth.
# /billing/checkout — auth-gated; creates a Checkout session for a plan.
# /billing/portal   — auth-gated; creates a Billing Portal session.
import billing as _billing


@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """Stripe webhook endpoint.  Must receive the RAW request body so the HMAC
    signature can be verified.  Flask's request.data gives the bytes as-is when
    we read it before request.form is touched — which is always the case here
    (no form parsing on this route).  Auth-free: protected by the HMAC instead."""
    payload    = request.get_data()   # raw bytes — never parse as form first
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        msg, code = _billing.handle_webhook(payload, sig_header)
        return jsonify(status=msg), code
    except Exception as exc:
        # Let Stripe know the payload was bad so it retries (or stops).
        import stripe as _stripe_mod
        if isinstance(exc, _stripe_mod.error.SignatureVerificationError):
            return jsonify(error="Invalid signature"), 400
        # Unexpected errors: 500 so Stripe retries.
        return jsonify(error=str(exc)), 500


@app.route("/billing/checkout", methods=["POST"])
@login_required
def billing_checkout():
    """Create a Stripe Checkout session and redirect the owner to it."""
    # Pre-deploy A2: guard the money POST. NOTE: when the subscribe/upgrade button is wired
    # into the UI, its form MUST include {{ csrf_token }} as `_csrf` (no UI caller today).
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    u   = current_user()
    biz = db.get_business(u["business_id"])
    plan = request.form.get("plan", "starter").lower().strip()
    if plan not in ("starter", "pro", "crew"):
        return jsonify(error="Invalid plan"), 400
    interval = request.form.get("interval", "month").lower().strip()  # 'month' | 'year' (annual, 20% off)
    try:
        session = _billing.create_checkout_session(biz["id"], plan, interval=interval)
        # session is a dict-like object with a .url attribute.
        checkout_url = session.get("url") if isinstance(session, dict) else session.url
        return redirect(checkout_url)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/billing/portal", methods=["POST"])
@login_required
def billing_portal():
    """Redirect the authenticated owner to the Stripe Billing Portal."""
    # Pre-deploy A2: guard the money POST (wire {{ csrf_token }} into its button when built).
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    u   = current_user()
    biz = db.get_business(u["business_id"])
    try:
        session = _billing.create_portal_session(biz["id"])
        portal_url = session.get("url") if isinstance(session, dict) else session.url
        return redirect(portal_url)
    except ValueError as exc:
        # No Stripe customer yet — merchant hasn't subscribed.
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 500


# ---- SF-3: Ticker heartbeat health endpoint ----
# No auth: this is a liveness/health probe for ops monitoring.
# Returns only platform-level metadata; zero tenant data is exposed.
@app.route("/health/ticker")
def health_ticker():
    from datetime import timezone as _tz
    raw = db.get_meta("last_tick_utc")
    fresh = not reminders.ticker_is_stale()
    age_s = None
    if raw:
        try:
            last = datetime.fromisoformat(raw)
            age_s = int((datetime.now(_tz.utc) - last).total_seconds())
        except (TypeError, ValueError):
            age_s = None
    return jsonify(fresh=fresh, last_tick_utc=raw, age_s=age_s)


# ---- Internal seam for the separate voice service (Phase 3 production split) ----
# voice_service.py runs as its own process and cannot share this app's SQLite disk,
# so it relays each spoken turn here. The web app owns the DB and runs the SAME
# handle_inbound the SMS/simulator paths use, so a voice turn books + alerts + queues
# reminders identically and booking writes stay single-writer. Locked behind a shared
# secret (constant-time compared); disabled (always 403) until FIRSTBACK_INTERNAL_SECRET
# is set on both services.
@app.route("/internal/voice/turn", methods=["POST"])
def internal_voice_turn():
    sent = request.headers.get("X-Internal-Secret", "")
    if not INTERNAL_SECRET or not secrets.compare_digest(sent, INTERNAL_SECRET):
        return jsonify(error="Forbidden."), 403
    data = request.get_json(silent=True) or {}
    try:
        biz_id, lead_id = int(data.get("biz")), int(data.get("lead"))
    except (TypeError, ValueError):
        return jsonify(error="biz and lead must be integers."), 400
    biz = db.get_business(biz_id)
    lead = db.get_lead(lead_id, biz["id"]) if biz else None
    if not biz or not lead:
        return jsonify(error="Unknown business or lead."), 404
    text = (data.get("text") or "").strip()
    # M-5 recovery-SMS relay (5g P1): the voice service has no Twilio creds, so it
    # relays a post-call recovery text through here with a sentinel prefix. Detect it
    # and send DIRECTLY via messaging -- never feed the sentinel into the booking brain
    # (handle_inbound), which would record it as a customer utterance + burn an LLM call.
    if text.startswith("__RECOVERY_SMS__:"):
        body = text[len("__RECOVERY_SMS__:"):].strip()
        if body and lead.get("phone"):
            messaging.send_sms(biz, lead["phone"], body)
        return jsonify(reply="", booked=False, urgent=False, recovery_sms=True)
    reply, booked, urgent = handle_inbound(biz, lead, text)
    return jsonify(reply=reply, booked=booked, urgent=urgent)


# R3: SSE streaming endpoint for the voice service.
# UX-ONLY: streams Haiku tokens so the voice service gets fast first-word latency.
# Does NOT run the booking write (P0-2 from PHASE5G-SPEC.md): that happens ONCE
# at stream END when voice_service.py POSTs the full reply to /internal/voice/turn.
# Always uses CLAUDE_MODEL_VOICE (Haiku) -- never Sonnet (latency + cost).
@app.route("/internal/voice/stream", methods=["POST"])
def internal_voice_stream():
    sent = request.headers.get("X-Internal-Secret", "")
    if not INTERNAL_SECRET or not secrets.compare_digest(sent, INTERNAL_SECRET):
        return jsonify(error="Forbidden."), 403
    data = request.get_json(silent=True) or {}
    try:
        biz_id, lead_id = int(data.get("biz")), int(data.get("lead"))
    except (TypeError, ValueError):
        return jsonify(error="biz and lead must be integers."), 400
    biz = db.get_business(biz_id)
    lead = db.get_lead(lead_id, biz["id"]) if biz else None
    if not biz or not lead:
        return jsonify(error="Unknown business or lead."), 404
    import llm as _llm
    history = data.get("history") or []
    user_text = (data.get("text") or "").strip()
    messages = list(history) + ([{"role": "user", "content": user_text}]
                                  if user_text else [])
    system = _llm.VOICE_CONFIRM_BOOKING_PROMPT

    def _gen():
        full_text = []
        for tok in _llm.complete_stream_voice(system, messages):
            full_text.append(tok)
            yield "data: " + json.dumps({"delta": tok}) + "\n\n"
        yield "data: " + json.dumps({"done": True, "full": "".join(full_text)}) + "\n\n"

    return Response(stream_with_context(_gen()), mimetype="text/event-stream")


# R4: Transcript log endpoint.
# The voice service POSTs the accumulated turn_log on disconnect so we store
# the full call transcript as direction='system' messages on the lead thread.
# PII rule: NO raw phone numbers may appear in the body (spec PI-4).
@app.route("/internal/voice/turn_log", methods=["POST"])
def internal_voice_turn_log():
    sent = request.headers.get("X-Internal-Secret", "")
    if not INTERNAL_SECRET or not secrets.compare_digest(sent, INTERNAL_SECRET):
        return jsonify(error="Forbidden."), 403
    data = request.get_json(silent=True) or {}
    try:
        biz_id, lead_id = int(data.get("biz")), int(data.get("lead"))
    except (TypeError, ValueError):
        return jsonify(error="biz and lead must be integers."), 400
    biz = db.get_business(biz_id)
    lead = db.get_lead(lead_id, biz["id"]) if biz else None
    if not biz or not lead:
        return jsonify(error="Unknown business or lead."), 404
    turns = data.get("turns") or []
    import re as _re_log
    _phone_re = _re_log.compile(r"\+?1?\d[\d\s\-().]{8,}\d")
    written = 0
    for turn in turns:
        caller_text = (turn.get("in") or "").strip()
        ai_text = (turn.get("out") or "").strip()
        if caller_text:
            safe_in = _phone_re.sub("[number]", caller_text)
            db.add_message(lead["id"], "system", f"[VOICE] caller: {safe_in}")
            written += 1
        if ai_text:
            safe_out = _phone_re.sub("[number]", ai_text)
            db.add_message(lead["id"], "system", f"[VOICE] ai: {safe_out}")
            written += 1
    return jsonify(ok=True, written=written)


# R5: Twilio voice call status callback.
# AMD / voicemail detection + cost metering. Twilio posts AnsweredBy on async AMD.
# machine_* -> voicemail: update outcome, send recovery SMS, no spoken pitch.
# no-answer / busy -> no_answer. completed -> keep existing outcome (set by /ws).
# Cost: real Twilio CallDuration (seconds), ceil to 30-second blocks.
@app.route("/webhooks/twilio/voice/status", methods=["POST"])
@require_twilio_signature
def twilio_voice_status():
    import math as _math
    sid = request.form.get("CallSid", "")
    status = request.form.get("CallStatus", "")
    answered_by = request.form.get("AnsweredBy", "")
    duration = 0
    try:
        duration = int(request.form.get("CallDuration") or 0)
    except (TypeError, ValueError):
        duration = 0
    blocks = _math.ceil(duration / 30) if duration > 0 else 0
    cost = blocks * config.VOICE_CREDIT_RATE_CENTS
    # AMD voicemail values include machine_end_beep (the most common reached-the-beep
    # result) -- omitting it (5g P1) dropped voicemail calls into the "error"/no-recovery
    # path. The AMD callback and the final completed callback share this URL, so on
    # `completed` we must NOT clobber a voicemail/no_answer already classified by the AMD
    # callback (5g P2): meter-only + bump in_progress->completed via update_voice_call_metering.
    if answered_by in ("machine_start", "machine_end_silence",
                       "machine_end_other", "machine_end_beep"):
        outcome = "voicemail"
        db.update_voice_call_outcome(sid, outcome, duration, cost)
    elif status in ("no-answer", "busy", "failed", "canceled"):
        outcome = "no_answer"
        db.update_voice_call_outcome(sid, outcome, duration, cost)
    else:
        # completed, or an intermediate/human AMD callback: meter without clobbering a
        # prior terminal classification. A clean finished call (still in_progress) -> completed.
        outcome = "completed" if status == "completed" else ""
        db.update_voice_call_metering(sid, duration, cost)
    # Voicemail: send recovery SMS to lead so no booking pitch goes into voicemail.
    if outcome == "voicemail" and sid:
        import sqlite3 as _sqlite3_vs
        _conn_vs = db.get_conn()
        _row_vs = _conn_vs.execute(
            "SELECT lead_id, biz_id FROM voice_calls WHERE twilio_sid=?", (sid,)
        ).fetchone()
        _conn_vs.close()
        if _row_vs:
            _biz_vs = db.get_business(_row_vs["biz_id"])
            _lead_vs = db.get_lead(_row_vs["lead_id"])
            if _biz_vs and _lead_vs and _lead_vs.get("phone"):
                messaging.send_sms(
                    _biz_vs, _lead_vs["phone"],
                    "We tried to reach you by phone -- happy to keep chatting "
                    "here. What are you looking to get painted?"
                )
    return jsonify(ok=True)


# ---- Reliability: error handlers (Phase 2) ----
# JSON for API/webhook paths; on-brand HTML for everything else.
# 500 uses the existing print-to-stderr convention (no logging.basicConfig).

def _is_api_path(path):
    return (path or "").startswith(("/api/", "/webhooks/"))


@app.errorhandler(404)
def not_found(e):
    if _is_api_path(request.path):
        return jsonify(error="Not found.", status=404), 404
    return render_template("errors/404.html"), 404


@app.errorhandler(500)
def server_error(e):
    print(f"[firstback] 500: {e}", file=sys.stderr, flush=True)
    if _is_api_path(request.path):
        return jsonify(error="Internal server error.", status=500), 500
    return render_template("errors/500.html"), 500


# Under a production WSGI server (gunicorn) the __main__ block below never runs, so
# start the reminders/follow-ups scheduler here when FIRSTBACK_RUN_TICKER is set. Run
# the web service with a SINGLE worker so exactly one ticker runs (sends are
# idempotent regardless).
if os.environ.get("FIRSTBACK_RUN_TICKER", "").strip().lower() in ("1", "true", "yes", "on"):
    reminders.start_ticker()


if __name__ == "__main__":
    # use_reloader=False keeps it to a single process (simpler to manage).
    # debug defaults OFF (no Werkzeug debugger in prod); set FIRSTBACK_DEBUG=1 to enable.
    reminders.start_ticker()  # background reminders/follow-ups scheduler
    app.run(debug=DEBUG, port=8800, use_reloader=False)
