"""Shared session-auth helpers for the trades_core kernel (vendored into each product).

Identity + access control used byte-for-byte identically by JobMagnet and FirstBack.
Depends only on Flask request/session globals and the product's own `db` module — each
repo supplies its own db.py exposing get_user()/get_business(), which live in the shared
data-core. Do NOT edit the vendored copies; edit trades_core/auth.py and run
`python3 trades_core/sync.py`.
"""
import re
from functools import wraps

from flask import session, request, redirect, url_for

import db

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


def _safe_next(target):
    """Only allow same-site relative redirects (never //evil.com)."""
    return (target if (target and target.startswith("/")
                       and not target.startswith("//")) else "/dashboard")
