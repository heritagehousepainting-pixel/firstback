"""RingBack -- Flask app.

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
                   url_for)
from werkzeug.security import generate_password_hash, check_password_hash

import db
import ai
import google_cal
import messaging
import mail
import alerts
import reminders
import compliance
import triage
import contact_import
import google_contacts
from config import (APP_NAME, TAGLINE, DEBUG, SECRET_KEY, TASKS_SECRET,
                    SESSION_COOKIE_SECURE, SEED_OWNER_EMAIL, SEED_OWNER_PASSWORD,
                    app_tz, VOICE_PUBLIC_URL, INTERNAL_SECRET)

app = Flask(__name__)
app.secret_key = SECRET_KEY
# Reload edited templates without a restart, even though the debugger is off by
# default (keeps the dev workflow; negligible overhead for this app).
app.config["TEMPLATES_AUTO_RELOAD"] = True
# Cookie hardening: HttpOnly (Flask default) + SameSite=Lax stop the session cookie
# from riding cross-site POSTs (CSRF on /settings, /login, etc.). Secure (HTTPS-only)
# is gated on RINGBACK_HTTPS so local http dev / the preview keep working.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE
db.init_db()

# Seed an owner login for "client zero" (business 1) on first run so the existing
# demo data is reachable immediately. Change the password after first login.
if db.count_users() == 0:
    db.create_user(SEED_OWNER_EMAIL, generate_password_hash(SEED_OWNER_PASSWORD), 1)


# ---- Auth ----
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def current_user():
    uid = session.get("uid")
    return db.get_user(uid) if uid else None


def current_business():
    u = current_user()
    return db.get_business(u["business_id"]) if u else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("uid"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


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


def _safe_next(target):
    """Only allow same-site relative redirects (never //evil.com)."""
    return (target if (target and target.startswith("/")
                       and not target.startswith("//")) else "/dashboard")


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
    return {"app_name": APP_NAME, "tagline": TAGLINE, "brain": ai.brain_mode(),
            "business": biz, "current_user": u}


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


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("uid"):
        return redirect("/dashboard")
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
        session.clear()
        session["uid"] = uid
        return redirect("/dashboard")
    return render_template("auth.html", mode="signup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("uid"):
        return redirect("/dashboard")
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = db.get_user_by_email(email)
        if not user or not check_password_hash(user["password_hash"], password):
            return render_template("auth.html", mode="login",
                                   error="Email or password is incorrect."), 401
        session.clear()
        session["uid"] = user["id"]
        return redirect(_safe_next(request.args.get("next")))
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


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


@app.route("/dashboard")
@login_required
def dashboard():
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
                           review_count=db.count_pending_suggestions(biz["id"]))


@app.route("/analytics")
@login_required
def analytics_page():
    return render_template("analytics.html")


@app.route("/callers")
@login_required
def callers_page():
    """The caller-triage inbox: review RingBack's suggestions (To review / Sorted /
    Dismissed), import an address book, and manage the screened-numbers directory.
    JS-driven via /api/*."""
    return render_template("callers.html",
                           gc_configured=google_contacts.configured(),
                           gc_connected=google_contacts.is_connected(current_business()["id"]))


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
        raw_avg = (request.form.get("avg_job_value") or "").strip().lstrip("$").replace(",", "")
        try:
            db.set_avg_job_value(biz["id"], float(raw_avg) if raw_avg else None)
        except ValueError:
            pass  # leave it unchanged on a non-numeric entry
        db.update_phone_voice(
            biz["id"],
            forward_to=(request.form.get("forward_to") or "").strip(),
            voice_callback_enabled=1 if request.form.get("voice_callback_enabled") else 0)
        return redirect("/settings?saved=1")
    return render_template("settings.html", business=biz,
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
            print(f"[ringback] notes precompute failed for lead {lead_id}: {e}",
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
        db.book_appointment(biz["id"], lead["id"], booking)
    return reply


def handle_inbound(biz, lead, body):
    """Run one inbound customer turn: record it, detect urgency, get the AI reply,
    and book if a slot was accepted (mirroring to Google Calendar + precomputing
    notes off the hot path). Returns (reply, booked, urgent)."""
    lead_id = lead["id"]
    db.add_message(lead_id, "in", body)
    urgent = ai.detect_urgency(body)
    if urgent:
        db.mark_lead_urgent(lead_id)
        alerts.notify_async(biz, "urgent", {"lead_id": lead_id,
                                            "name": lead.get("name"),
                                            "phone": lead.get("phone")})
    history = db.get_messages(lead_id)
    exclude = google_cal.busy_slot_ids(biz["id"])  # Google conflicts, empty if not connected
    reply, booking = ai.generate_reply(biz, history, exclude_slot_ids=exclude)
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
            # Reschedule: now that the new slot is held, release the lead's old
            # estimate(s) so a re-book never double-books or orphans a slot.
            for a in prior:
                db.cancel_appointment(biz["id"], a["id"])
            alerts.notify_async(biz, "booking", {"lead_id": lead_id, "name": lead.get("name"),
                                                 "phone": lead.get("phone"), "when": booking})
            # Mirror onto Google Calendar + queue the pre-estimate reminder (both
            # best-effort, off the hot path; no-ops unless configured).
            if gday and gtime:
                google_cal.create_event_async(
                    biz["id"], f"Estimate: {lead['name']}",
                    f"RingBack booked a free estimate for {lead['name']} ({lead['phone']}).",
                    gday, gtime)
                reminders.enqueue_reminder(biz, lead, gday, gtime)
    # Precompute notes off the hot path (after booking, so a booked lead is summed
    # as 'scheduled'); never blocks this turn.
    _schedule_notes(lead_id)
    return reply, booked, urgent


@app.route("/api/sim/incoming", methods=["POST"])
@login_required
def sim_incoming():
    data = request.get_json(silent=True) or {}  # name/phone optional
    biz = current_business()
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
        print(f"[ringback] google connect failed: {e}", file=sys.stderr, flush=True)
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
    until Twilio, recorded on the thread). Scoped to the owner's business."""
    biz = current_business()
    appt = db.cancel_appointment(biz["id"], appt_id)
    if not appt:
        return jsonify(error="Appointment not found."), 404
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
    """Tag a number so RingBack never cold-texts it. Only the owner-set categories
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
    db.set_suggestion_status(sug_id, "accepted")
    return jsonify(ok=True, category=category)


@app.route("/api/suggestions/<int:sug_id>/dismiss", methods=["POST"])
@login_required
def api_suggestion_dismiss(sug_id):
    """Dismiss a suggestion -- it won't be raised again for this number."""
    biz = current_business()
    if not db.get_suggestion(sug_id, biz["id"]):
        return jsonify(error="Suggestion not found."), 404
    db.set_suggestion_status(sug_id, "dismissed")
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
    db.set_suggestion_status(sug_id, "pending")
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
            db.set_suggestion_status(sid, "accepted")
        elif action == "dismiss":
            db.set_suggestion_status(sid, "dismissed")
        else:  # reopen
            if sug["status"] == "accepted":
                db.delete_contact(biz["id"], sug["number"])
            db.set_suggestion_status(sid, "pending")
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
        print(f"[ringback] contact import parse failed: {e}", file=sys.stderr, flush=True)
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
        print(f"[ringback] google contacts connect failed: {e}", file=sys.stderr, flush=True)
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
        print(f"[ringback] google contacts sync failed: {e}", file=sys.stderr, flush=True)
        return jsonify(error="Google Contacts sync failed. Please try again."), 502
    return jsonify(ok=True, **summary)


@app.route("/api/contacts/google/disconnect", methods=["POST"])
@login_required
def google_contacts_disconnect():
    google_contacts.disconnect(current_business()["id"])
    return jsonify(connected=False)


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


def _missed_call_textback(biz, caller, call_sid="", dial_status=""):
    """Shared missed-call handling: triage the caller, then (if they're worth
    engaging) find or create their lead, log the call, and -- only when the thread
    is empty -- generate + send the instant text-back. Returns True if we engaged,
    False if the caller was screened out (so the voice prompt stays honest about
    whether a text actually went out)."""
    # Triage FIRST: a known non-prospect (the owner's mom, the power company, a
    # blocked number) or an opted-out caller is logged but never cold-pitched. An
    # unknown caller is treated as a potential customer and engaged.
    verdict = triage.screen_caller(biz["id"], caller)
    if not verdict["engage"]:
        db.log_call(biz["id"], call_sid, from_number=caller,
                    to_number=biz.get("twilio_number") or "", dial_status=dial_status,
                    missed=1, category=verdict["category"], engaged=0)
        return False
    lead = db.get_lead_by_phone(biz["id"], caller)
    if not lead:
        lead = db.get_lead(db.create_lead(biz["id"], "New Caller", caller))
    db.log_call(biz["id"], call_sid, from_number=caller,
                to_number=biz.get("twilio_number") or "", dial_status=dial_status,
                missed=1, lead_id=lead["id"], category=verdict["category"], engaged=1)
    # Greet only an empty thread, so a repeat missed call mid-conversation does not
    # re-introduce us (the owner is still alerted via the 'lead' alert + call log).
    if not db.get_messages(lead["id"]):
        reply = open_conversation(biz, lead)    # records the thread + alerts the owner
        messaging.send_sms(biz, caller, reply)  # transmit (already recorded)
    return True


@app.route("/webhooks/twilio/voice/inbound", methods=["POST"])
@require_twilio_signature
def twilio_voice_inbound():
    """Inbound call to a RingBack number: ring the contractor's cell; if there is
    no cell on file, treat it as missed right away and text the caller back."""
    biz = db.get_business_by_twilio_number(request.form.get("To", ""))
    if not biz:
        return _twiml("<Response><Reject/></Response>")
    forward = biz.get("forward_to")
    if forward:
        action = _public_base() + "/webhooks/twilio/voice/dial-status"
        return _twiml(
            f'<Response><Dial answerOnBridge="true" timeout="18" action="{action}" '
            f'method="POST"><Number>{_xesc(forward)}</Number></Dial></Response>')
    engaged = _missed_call_textback(biz, request.form.get("From", ""),
                                    request.form.get("CallSid", ""), "no-forward")
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
        return _twiml(f"<Response><Message>{_xesc(biz.get('name') or 'RingBack')}: "
                      "reply here about your free estimate. Reply STOP to "
                      "unsubscribe.</Message></Response>")
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
        if res.get("status") in ("placed", "simulated"):
            return _twiml("<Response><Message>Calling you now.</Message></Response>")
    reply, _booked, _urgent = handle_inbound(biz, lead, body)  # records + books + alerts
    messaging.send_sms(biz, caller, reply)  # transmit (already recorded)
    return _twiml("<Response/>")


@app.route("/webhooks/twilio/sms/status", methods=["POST"])
@require_twilio_signature
def twilio_sms_status():
    """Twilio delivery receipts -> reconcile the stored message's status."""
    db.set_message_delivery(request.form.get("MessageSid", ""),
                            request.form.get("MessageStatus", ""))
    return _twiml("<Response/>")


# ---- Scheduler trigger for production (external cron) ----
# The in-process ticker dies with the process; behind a real web server you can
# instead (or also) hit this every minute from cron with the shared secret:
#   curl -fsS -X POST -H "X-Tasks-Secret: $RINGBACK_TASKS_SECRET" URL/tasks/run-due
# Disabled (always 403) until RINGBACK_TASKS_SECRET is set. Not login-required by
# design, so it's locked behind the secret header and constant-time compared.
@app.route("/tasks/run-due", methods=["POST"])
def tasks_run_due():
    sent = request.headers.get("X-Tasks-Secret", "")
    if not TASKS_SECRET or not secrets.compare_digest(sent, TASKS_SECRET):
        return jsonify(error="Forbidden."), 403
    return jsonify(reminders.tick_once())


# ---- Internal seam for the separate voice service (Phase 3 production split) ----
# voice_service.py runs as its own process and cannot share this app's SQLite disk,
# so it relays each spoken turn here. The web app owns the DB and runs the SAME
# handle_inbound the SMS/simulator paths use, so a voice turn books + alerts + queues
# reminders identically and booking writes stay single-writer. Locked behind a shared
# secret (constant-time compared); disabled (always 403) until RINGBACK_INTERNAL_SECRET
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


# Under a production WSGI server (gunicorn) the __main__ block below never runs, so
# start the reminders/follow-ups scheduler here when RINGBACK_RUN_TICKER is set. Run
# the web service with a SINGLE worker so exactly one ticker runs (sends are
# idempotent regardless).
if os.environ.get("RINGBACK_RUN_TICKER", "").strip().lower() in ("1", "true", "yes", "on"):
    reminders.start_ticker()


if __name__ == "__main__":
    # use_reloader=False keeps it to a single process (simpler to manage).
    # debug defaults OFF (no Werkzeug debugger in prod); set RINGBACK_DEBUG=1 to enable.
    reminders.start_ticker()  # background reminders/follow-ups scheduler
    app.run(debug=DEBUG, port=8800, use_reloader=False)
