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
from datetime import datetime
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
from config import (APP_NAME, TAGLINE, DEBUG, SECRET_KEY, TASKS_SECRET,
                    SESSION_COOKIE_SECURE, SEED_OWNER_EMAIL, SEED_OWNER_PASSWORD,
                    app_tz, VOICE_PUBLIC_URL, INTERNAL_SECRET,
                    SCREEN_MODE, SCREEN_AI_CONTENT, SCREEN_SCORE_MID)

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
        f"You are the assistant for {name}, a {trade} business, replying by text to "
        "a caller we just missed. Use a professional, clear, and courteous tone with "
        "complete sentences and correct grammar. Be personable but not casual: no "
        "slang, no filler, and no emoji. Keep each reply concise, about one to three "
        "sentences. First find out what they need, then ask for their address to "
        "confirm they are in your service area, then offer two available estimate "
        "windows and book the one they choose. Never quote prices or give a dollar "
        "range; let them know you will provide a quote at the free in-person estimate."
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


def _csrf_token():
    """Get-or-create this session's CSRF token (double-submit). Rendered into the command
    center and echoed back by the JS as `_csrf`; validated on every assistant POST."""
    tok = session.get("csrf_token")
    if not tok:
        tok = secrets.token_hex(32)
        session["csrf_token"] = tok
    return tok


def _csrf_ok():
    """The form's `_csrf` matches the session token (constant-time). Defense in depth on top
    of the SameSite session cookie."""
    tok = session.get("csrf_token")
    return bool(tok) and secrets.compare_digest(tok, request.form.get("_csrf", ""))


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
    return render_template("onboarding.html")


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
    return render_template("product.html")


@app.route("/solutions")
def solutions():
    return render_template("solutions.html")


@app.route("/resources")
def resources():
    return render_template("resources.html")


@app.route("/company")
def company():
    return render_template("company.html")


@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


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


@app.route("/customers")
def customers():
    return render_template("customers.html")


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


@app.route("/dashboard")
@login_required
def dashboard():
    """The signed-in home is now the conversational command center. The cockpit (leads,
    booked estimates, alerts) still lives at /pipeline for working by hand."""
    hour = datetime.now(app_tz()).hour
    part = "Morning" if hour < 12 else ("Afternoon" if hour < 17 else "Evening")
    biz = current_business()
    owner = (biz.get("owner_name") or "").strip() if biz else ""
    hello = f"{part}, {owner.split()[0]}." if owner else f"{part}."
    brief, chips, feed_sig = _command_feed(biz)
    return render_template("command.html", hello=hello,
                           briefing=brief, feed_sig=feed_sig,
                           digest=convos.digest(biz["id"]),
                           golive=connections.golive_summary(biz),
                           suggestions=chips)


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
    return render_template("dashboard.html", leads=leads, appointments=appts, stats=stats,
                           alert_feed=db.recent_alerts(biz["id"], 8),
                           reminder_state=db.reminders_by_appointment(biz["id"]),
                           screened=db.recent_screened_calls(biz["id"], 8),
                           screen_stats=db.screening_stats(biz["id"]),
                           screen_mode=_effective_screen_mode(biz),
                           review_count=db.count_pending_suggestions(biz["id"]))


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
    """Run a gated action the owner just approved (e.g. texting a lead). The send still
    flows through the gated messaging.send_sms seam (opt-outs + simulated-vs-live honored)."""
    biz = current_business()
    if not _csrf_ok():
        return jsonify({"error": "bad_csrf"}), 403
    tool = (request.form.get("tool") or "").strip()
    try:
        args = json.loads(request.form.get("args") or "{}")
        if not isinstance(args, dict):
            args = {}
    except (ValueError, TypeError):
        args = {}
    out = assistant.execute(biz, tool, args)
    # Audit the confirmed action (no raw phone numbers; the body is the owner's own words).
    db.add_audit(biz["id"], f"confirm:{tool}", str(args.get("message") or "")[:120])
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
    biz = current_business()
    flag_id = request.form.get("flag_id")
    if flag_id and flag_id.isdigit():
        db.resolve_flag(biz["id"], int(flag_id))
    return redirect("/training")


@app.route("/digest/send", methods=["POST"])
@login_required
def digest_send():
    """Email this owner their weekly digest now (gated/simulated until SMTP is set)."""
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


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    biz = current_business()
    if request.method == "POST":
        fields = {k: request.form.get(k, "") for k in
                  ["name", "trade", "service_area", "hours", "owner_name",
                   "phone", "ai_instructions"]}
        db.update_business(biz["id"], fields)
        db.update_alert_prefs(biz["id"], {
            "alert_email": request.form.get("alert_email", "").strip(),
            "alert_sms": request.form.get("alert_sms", "").strip(),
            "alert_on_lead": 1 if request.form.get("alert_on_lead") else 0,
            "alert_on_booking": 1 if request.form.get("alert_on_booking") else 0,
            "alert_on_urgent": 1 if request.form.get("alert_on_urgent") else 0,
        })
        try:
            lead_hours = int(float(request.form.get("reminder_lead_hours") or 24))
        except (TypeError, ValueError):
            lead_hours = 24
        db.update_reminder_prefs(biz["id"], {
            "reminders_enabled": 1 if request.form.get("reminders_enabled") else 0,
            "followups_enabled": 1 if request.form.get("followups_enabled") else 0,
            "reminder_lead_hours": max(0, min(168, lead_hours)),
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
            voice_callback_enabled=1 if request.form.get("voice_callback_enabled") else 0)
        # Per-business screening mode: blank/"default" -> NULL (inherit the app default).
        db.set_screen_mode(biz["id"], (request.form.get("screen_mode") or "").strip())
        return redirect("/settings?saved=1")
    return render_template("settings.html", business=biz,
                           sched=db.scheduling_prefs(biz["id"]),
                           integrations=db.list_integrations(biz["id"]),
                           saved=request.args.get("saved"),
                           google_configured=google_cal.configured(),
                           google_connected=google_cal.is_connected(biz["id"]),
                           gconnected=request.args.get("gconnected"),
                           gerror=request.args.get("gerror"),
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
                           owner_email=db.owner_email(biz["id"]))


@app.route("/settings/password", methods=["POST"])
@login_required
def settings_password():
    user = current_user()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    if not check_password_hash(user["password_hash"], current):
        return redirect("/settings?pwerror=current")
    if len(new) < 8:
        return redirect("/settings?pwerror=short")
    db.update_user_password(user["id"], generate_password_hash(new))
    return redirect("/settings?pw=1")


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
@app.route("/twiml/dispatcher/<int:lead_id>", methods=["POST"])
@require_twilio_signature
def dispatcher_twiml(lead_id):
    """TwiML served to the owner's phone when FirstBack places an urgent dispatcher
    call. Reads the caller's exact last inbound message, then offers press-1 to
    connect. The words come from db.get_last_inbound_message — always synchronous,
    never relies on async-enriched summary which may not have landed yet."""
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
    lead = db.get_lead(lead_id)
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
    exclude = google_cal.busy_slot_ids(biz["id"])  # empty unless Google connected
    reply, booking = ai.generate_reply(biz, [], exclude_slot_ids=exclude)
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
                reminders.enqueue_reminder(biz, lead, gday, gtime)
                reminders.enqueue_morning_reminder(biz, lead, gday, gtime)
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
            try:
                import roi as _roi_mod
                _milestone = _roi_mod.check_roi_milestone(biz["id"])
                if _milestone:
                    from datetime import timezone as _tzu
                    alerts.notify_async(biz, "roi_milestone",
                                        {"body": _milestone.get("body", ""),
                                         "multiple": _milestone.get("multiple"),
                                         "revenue": _milestone.get("revenue")})
                    db.set_roi_milestone_sent(biz["id"],
                                              datetime.now(_tzu.utc).isoformat())
            except Exception as _me:
                print(f"[firstback] milestone hook error (biz {biz['id']}): {_me}",
                      file=sys.stderr, flush=True)
    return reply


def handle_inbound(biz, lead, body):
    """Run one inbound customer turn: record it, detect urgency, get the AI reply,
    and book if a slot was accepted (mirroring to Google Calendar + precomputing
    notes off the hot path). Returns (reply, booked, urgent)."""
    lead_id = lead["id"]
    db.add_message(lead_id, "in", body)
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
            _disp_base = VOICE_PUBLIC_URL.rstrip("/") if VOICE_PUBLIC_URL else None
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
    exclude = google_cal.busy_slot_ids(biz["id"])  # Google conflicts, empty if not connected
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
            # milestone threshold; fire once per tenant (idempotent via roi.py).
            try:
                import roi as _roi_mod
                _milestone = _roi_mod.check_roi_milestone(biz["id"])
                if _milestone:
                    from datetime import timezone as _mtz
                    _mts = datetime.now(_mtz.utc).isoformat()
                    alerts.notify_async(biz, "roi_milestone",
                                        {"body": _milestone.get("body", ""),
                                         "multiple": _milestone.get("multiple"),
                                         "revenue": _milestone.get("revenue")})
                    db.set_roi_milestone_sent(biz["id"], _mts)
            except Exception as _me:
                import sys as _sys
                print(f"[firstback] milestone hook error (biz {biz['id']}): {_me}",
                      file=_sys.stderr, flush=True)
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
                reminders.enqueue_reminder(biz, lead, gday, gtime)
                reminders.enqueue_morning_reminder(biz, lead, gday, gtime)
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
    data, err = _get_json("date")
    if err:
        return err
    busy = bool(data.get("busy"))
    db.set_day_busy(current_business()["id"], data["date"], busy)
    return jsonify(date=data["date"], busy=busy)


@app.route("/api/integrations", methods=["POST"])
@login_required
def api_integrations():
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
    google_cal.disconnect(current_business()["id"])
    return jsonify(connected=False)


@app.route("/api/appointments/<int:appt_id>/cancel", methods=["POST"])
@login_required
def api_cancel_appointment(appt_id):
    """Owner-initiated cancel from the dashboard. Frees the slot + cancels its
    reminders (db.cancel_appointment), then texts the customer a heads-up (simulated
    until Twilio, recorded on the thread). F04: also cancels the Google Calendar event
    asynchronously when one was created. Scoped to the owner's business."""
    biz = current_business()
    appt = db.cancel_appointment(biz["id"], appt_id)
    if not appt:
        return jsonify(error="Appointment not found."), 404
    # F04: cancel the Google Calendar event if one was created for this appointment.
    google_event_id = appt.get("google_event_id")
    if google_event_id and google_cal.is_connected(biz["id"]):
        google_cal.cancel_event_async(biz["id"], google_event_id)
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
    db.mark_call_engaged(call_id, lead["id"])
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
    lead = db.get_lead(lead_id, biz["id"])   # ownership-scoped
    if not lead:
        return jsonify(error="Lead not found."), 404
    number = (lead.get("phone") or "").strip()
    if not number:
        return jsonify(error="No phone number on file for this lead."), 400
    _mark_number_spam(biz["id"], number)
    return jsonify(ok=True)


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
    google_contacts.disconnect(current_business()["id"])
    return jsonify(connected=False)


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


def _screen_missed_caller(biz, caller):
    """Run the full 'phone screen' for a missed caller: gather the layered signals
    (STIR/SHAKEN attestation off the request, neighbor-spoof, behavior, crowdsource,
    and -- only if the free tiers leave it ambiguous -- a paid reputation lookup), then
    return triage.screen_caller's verdict. Reputation is consulted lazily so the common
    (clean or obviously-known) case never pays for or waits on a network call."""
    attestation = request.form.get("StirVerstat") if request else None
    nspoof = triage.neighbor_spoof(biz.get("twilio_number") or biz.get("phone"), caller)
    behavior = _caller_behavior(biz["id"], caller)
    verdict = triage.screen_caller(biz["id"], caller, attestation=attestation,
                                   neighbor_spoof=nspoof, behavior=behavior)
    # Only an UNKNOWN caller in the ambiguous band is worth a paid lookup: identity
    # tiers already settled the rest, and a clean/known caller shouldn't cost anything.
    if (reputation.configured() and verdict["status"] in ("prospect", "review")
            and verdict["score"] >= SCREEN_SCORE_MID - 20):
        rep = reputation.lookup(caller)
        if rep:
            verdict = triage.screen_caller(biz["id"], caller, attestation=attestation,
                                           neighbor_spoof=nspoof, behavior=behavior,
                                           reputation=rep)
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
        return False
    lead = db.get_lead_by_phone(biz["id"], caller)
    if not lead:
        lead = db.get_lead(db.create_lead(biz["id"], "New Caller", caller))
    db.log_call(biz["id"], call_sid, lead_id=lead["id"], engaged=1, **common)
    # Greet only an empty thread, so a repeat missed call mid-conversation does not
    # re-introduce us (the owner is still alerted via the 'lead' alert + call log).
    if not db.get_messages(lead["id"]):
        reply = open_conversation(biz, lead)    # records the thread + alerts the owner
        messaging.send_sms(biz, caller, reply)  # transmit (already recorded)
    return True


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
    engaged = _missed_call_textback(biz, request.form.get("From", ""),
                                    call_sid, "no-forward")
    if engaged:
        return _twiml("<Response><Say>Sorry we missed you. We just sent you a text "
                      "message. Goodbye.</Say><Hangup/></Response>")
    return _twiml("<Response><Hangup/></Response>")


@app.route("/webhooks/twilio/voice/dial-status", methods=["POST"])
@require_twilio_signature
def twilio_voice_dial_status():
    """Fires when the dial leg ends. A no-answer/busy/failed means the contractor
    missed it -> fire the instant text-back. Otherwise the call was answered."""
    biz = db.get_business_by_twilio_number(request.form.get("To", ""))
    status = (request.form.get("DialCallStatus") or "").lower()
    if biz and status in _MISSED_DIAL:
        engaged = _missed_call_textback(biz, request.form.get("From", ""),
                                        request.form.get("CallSid", ""), status)
        if engaged:
            return _twiml("<Response><Say>Sorry we missed you. We just sent you a text "
                          "message. Goodbye.</Say><Hangup/></Response>")
    return _twiml("<Response><Hangup/></Response>")


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
        return _twiml("<Response><Message>You are unsubscribed and will not receive "
                      "more messages. Reply HELP for help.</Message></Response>")
    if norm in _STOP_WORDS:
        db.set_opt_out(biz["id"], caller, source="sms-stop")
        return _twiml("<Response><Message>You are unsubscribed and will not receive "
                      "more messages. Reply HELP for help.</Message></Response>")
    if compliance.detect_revocation(body):
        # Plain-language opt-out, not the exact keyword (2025 FCC any-reasonable-
        # means rule) -> honor it across SMS and voice.
        db.set_opt_out(biz["id"], caller, source="sms-nlu")
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
    if norm in _CALL_WORDS and VOICE_PUBLIC_URL:
        db.set_voice_consent(biz["id"], caller, True)
        if not compliance.voice_allowed_now():
            return _twiml("<Response><Message>Thanks. It is currently after hours, so "
                          "we will call you during business hours. You can also keep "
                          "texting here any time.</Message></Response>")
        twiml_url = (VOICE_PUBLIC_URL.rstrip("/")
                     + f"/twiml?biz={biz['id']}&lead={lead['id']}&name={quote(biz.get('name') or '')}")
        res = messaging.place_call(biz, caller, twiml_url)
        # Honesty: only claim "calling you now" when a real call was actually placed.
        # Simulated/error must not promise a call that isn't happening.
        if res.get("status") == "placed":
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
    reply, booked, urgent = handle_inbound(biz, lead, (data.get("text") or "").strip())
    return jsonify(reply=reply, booked=booked, urgent=urgent)


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
