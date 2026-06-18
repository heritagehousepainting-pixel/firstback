"""Shared helpers for FirstBack's Google OAuth connections.

google_cal.py (Calendar) and google_contacts.py (Contacts) are deliberately two
independent OAuth connections, but they share the same token lifecycle. This module
holds the one piece of that lifecycle that was previously copy-pasted into both and,
worse, written with an inverted boolean (`fresh` actually meant "stale"): the
access-token freshness check. Keeping a single, plainly-named helper here means both
modules refresh on exactly the same rule.
"""
from datetime import datetime, timezone, timedelta

# Refresh this many seconds BEFORE the real expiry so an in-flight API call never
# races the token going stale.
_EXPIRY_SKEW_SECONDS = 60


def access_is_fresh(expiry_iso, *, skew_seconds=_EXPIRY_SKEW_SECONDS, now=None):
    """True when a stored access token is still safe to use.

    "Fresh" means the token's expiry is strictly in the future by more than
    `skew_seconds`. A missing, blank, or unparseable expiry returns False so the
    caller falls through to a refresh -- the safe default. A naive (tz-less) expiry
    is treated as UTC, matching how we write it (_expiry_iso uses UTC).

    `now` is injectable for tests; it defaults to the current UTC time.
    """
    if not expiry_iso:
        return False
    try:
        exp = datetime.fromisoformat(expiry_iso)
    except (ValueError, TypeError):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return exp > now + timedelta(seconds=skew_seconds)
