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
    """Only allow same-site relative redirects; fall back to /dashboard for anything else.
    Rejects off-site targets in every form we've seen bypass a naive startswith('//') check:
      //evil.com          (protocol-relative)
      /\\evil.com          (BACKSLASH bypass — browsers normalize \\ to /, so this becomes
                            //evil.com after the browser parses it; the old guard let it through)
      https:evil.com      (embedded scheme)
      whitespace/control-char smuggling (\\n \\r \\t) used to split or obscure the value
    We normalize backslashes to forward slashes FIRST, then require a single leading slash."""
    t = (target or "").strip()
    if not t:
        return "/dashboard"
    if any(ch in t for ch in ("\n", "\r", "\t", "\x00")):
        return "/dashboard"
    norm = t.replace("\\", "/")                 # browsers treat \\ as / in the authority
    if norm.startswith("/") and not norm.startswith("//") and "://" not in norm:
        return t
    return "/dashboard"
