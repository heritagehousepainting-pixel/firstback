"""Optional email sending for FirstBack (owner alerts), via stdlib smtplib.

Gated and defensive like google_cal/messaging: a safe no-op unless SMTP is
configured, and any send error is swallowed + logged with the "[firstback]"
prefix, never breaking an alert. No vendor SDK -- just smtplib + email.message.
"""
import sys

from config import (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM,
                    SMTP_USE_TLS)


def configured():
    """True if we have at least an SMTP host and a from-address to send with."""
    return bool(SMTP_HOST and SMTP_FROM)


def send_email(to, subject, body):
    """Send a plain-text email, or simulate it when SMTP isn't configured.

    Returns a status dict whose "status" is one of:
      "sent"      -- handed to the SMTP server
      "simulated" -- SMTP not configured; nothing sent (the caller still logs it)
      "skipped"   -- no recipient address
      "error"     -- configured but the send failed (logged; carries "error")
    """
    to = (to or "").strip()
    if not to:
        return {"status": "skipped", "reason": "no recipient"}
    if not configured():
        return {"status": "simulated"}

    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
    except Exception as e:
        print(f"[firstback] smtp send failed (-> {to}): {e}", file=sys.stderr, flush=True)
        return {"status": "error", "error": str(e)}
    return {"status": "sent"}
