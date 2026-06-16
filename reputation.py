"""Number reputation -- Tier 2 of the call screen (the paid, gated lookup seam).

When a missed caller is UNKNOWN and the free tiers (identity, STIR/SHAKEN,
neighbor-spoof, behavior, crowdsource) can't clear them, we can ask a paid provider
whether the number is a robocaller / what line type it is. This module is that seam,
built like messaging.py: dormant until configured, talks over plain `requests`, and
NEVER raises -- any error/timeout returns {} so the screen FAILS OPEN (a real
homeowner is never silenced because a vendor was slow or down).

Results are cached per number in db.number_reputation (reputation is sticky, and a
robocaller looks the same to every tenant, so one lookup serves all businesses).

Providers (config.REPUTATION_PROVIDER):
  "off"             -> never call out (default).
  "twilio_nomorobo" -> Twilio Lookup v2 line-type intelligence + the Nomorobo Spam
                       Score add-on (0/1). Reuses the Twilio account creds.
  "hiya"            -> Hiya number-reputation API (flagged/mixed/unflagged).

lookup(number) -> {} or {"line_type": str|None, "spam_score": 0..100|None,
                         "source": str, "from_cache": bool}
where spam_score is normalized to 0..100 (higher = more spam-like).
"""
import sys

import db
from config import (REPUTATION_PROVIDER, REPUTATION_TTL_HOURS,
                    REPUTATION_TIMEOUT_SECONDS, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                    HIYA_API_KEY, HIYA_BASE_URL)

_LOOKUP_BASE = "https://lookups.twilio.com/v2/PhoneNumbers"
# Line types that strongly correlate with robocallers (cheap VoIP, not a real cell).
SPAMMY_LINE_TYPES = {"nonFixedVoip", "nonfixedvoip"}


def configured():
    """True if a real reputation provider is selected AND its credentials are set.
    When False, lookup() is a no-op and the free screening tiers carry the load."""
    if REPUTATION_PROVIDER == "twilio_nomorobo":
        return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)
    if REPUTATION_PROVIDER == "hiya":
        return bool(HIYA_API_KEY)
    return False


def provider_label():
    """Human-readable provider name for the settings UI (honest about what's on)."""
    return {"twilio_nomorobo": "Twilio Lookup + Nomorobo Spam Score",
            "hiya": "Hiya number reputation"}.get(REPUTATION_PROVIDER, "Off")


def lookup(number, max_age_hours=None):
    """Reputation for a number, cache-first. Returns {} when not configured, on any
    error, or for an empty number -- the caller treats {} as 'no signal' (fail open).
    A fresh cache hit is returned without a network call."""
    if not configured():
        return {}
    key = db._digits10(number)
    if not key:
        return {}
    ttl = REPUTATION_TTL_HOURS if max_age_hours is None else max_age_hours
    cached = db.get_reputation(key, max_age_hours=ttl)
    if cached:
        return {"line_type": cached.get("line_type"),
                "spam_score": cached.get("spam_score"),
                "source": cached.get("source") or "cache", "from_cache": True}
    try:
        if REPUTATION_PROVIDER == "twilio_nomorobo":
            result = _twilio_nomorobo(key)
        elif REPUTATION_PROVIDER == "hiya":
            result = _hiya(key)
        else:
            return {}
    except Exception as e:   # network, timeout, parse -- fail open, never raise
        print(f"[ringback] reputation lookup failed ({REPUTATION_PROVIDER}): {e}",
              file=sys.stderr, flush=True)
        return {}
    # Cache the verdict (even a clean one) so we don't re-pay for the same number.
    db.set_reputation(key, line_type=result.get("line_type"),
                      spam_score=result.get("spam_score"), source=REPUTATION_PROVIDER)
    result["source"] = REPUTATION_PROVIDER
    result["from_cache"] = False
    return result


def _twilio_nomorobo(number):
    """Twilio Lookup v2: line-type intelligence + the Nomorobo Spam Score add-on.
    Nomorobo returns 1 (likely robocall) / 0 (likely not); we normalize 1 -> 100."""
    import requests
    r = requests.get(
        f"{_LOOKUP_BASE}/{number}",
        params={"Fields": "line_type_intelligence", "AddOns": "nomorobo_spam_score"},
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=REPUTATION_TIMEOUT_SECONDS)
    r.raise_for_status()
    data = r.json() or {}
    line_type = ((data.get("line_type_intelligence") or {}).get("type")) or None
    spam_score = None
    nomo = (((data.get("add_ons") or {}).get("results") or {})
            .get("nomorobo_spam_score") or {})
    raw = (nomo.get("result") or {}).get("score")
    if raw is not None:
        try:
            spam_score = 100 if int(raw) >= 1 else 0
        except (TypeError, ValueError):
            spam_score = None
    return {"line_type": line_type, "spam_score": spam_score}


# Hiya reputation status -> a 0..100 spam score.
_HIYA_SCORE = {"flagged": 100, "mixed_high": 70, "mixed_low": 40, "unflagged": 0}


def _hiya(number):
    """Hiya number-reputation API -> normalized score. Number is sent as +<digits>."""
    import requests
    r = requests.get(
        f"{HIYA_BASE_URL.rstrip('/')}/v1/reputation/{number}",
        headers={"Authorization": f"Bearer {HIYA_API_KEY}"},
        timeout=REPUTATION_TIMEOUT_SECONDS)
    r.raise_for_status()
    data = r.json() or {}
    status = str(data.get("status") or "").strip().lower()
    return {"line_type": data.get("line_type"),
            "spam_score": _HIYA_SCORE.get(status)}
