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
        print(f"[firstback] reputation lookup failed ({REPUTATION_PROVIDER}): {e}",
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


# ---- E4: Google Places review count polling ----------------------------------
# INERT when GOOGLE_PLACES_API_KEY is unset; never raises.
import re as _re

_PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
_PLACES_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"


def _extract_place_id(review_link):
    """Pull a Place ID from a review_link URL (place_id= param or a ChIJ path segment)."""
    if not review_link:
        return None
    m = _re.search(r"[?&]place_id=([A-Za-z0-9_\-]+)", review_link)
    if m:
        return m.group(1)
    m = _re.search(r"(ChIJ[A-Za-z0-9_\-]+)", review_link)
    if m:
        return m.group(1)
    return None


def poll_google_reputation(business_id):
    """Fetch current Google review count + star rating and persist via
    db.set_google_reputation. Returns {"review_count","star_rating"} or None. INERT
    (no network) when config.GOOGLE_PLACES_API_KEY is unset. Never raises."""
    import config as _cfg
    if not _cfg.GOOGLE_PLACES_API_KEY:
        return None
    try:
        import requests
        biz = db.get_business(business_id)
        if not biz:
            return None
        api_key = _cfg.GOOGLE_PLACES_API_KEY
        place_id = _extract_place_id(biz.get("review_link"))
        if not place_id:
            name = (biz.get("name") or "").strip()
            area = (biz.get("service_area") or "").strip()
            query = f"{name} {area}".strip()
            if not query:
                return None
            sr = requests.get(_PLACES_SEARCH_URL, params={
                "input": query, "inputtype": "textquery",
                "fields": "place_id", "key": api_key}, timeout=5)
            sr.raise_for_status()
            candidates = (sr.json() or {}).get("candidates") or []
            if not candidates:
                return None
            place_id = (candidates[0] or {}).get("place_id")
            if not place_id:
                return None
        dr = requests.get(_PLACES_DETAILS_URL, params={
            "place_id": place_id, "fields": "user_ratings_total,rating",
            "key": api_key}, timeout=5)
        dr.raise_for_status()
        ddata = (dr.json() or {}).get("result") or {}
        review_count = ddata.get("user_ratings_total")
        star_rating = ddata.get("rating")
        if review_count is None:
            return None
        db.set_google_reputation(business_id, int(review_count),
                                 float(star_rating) if star_rating is not None else None)
        return {"review_count": int(review_count),
                "star_rating": float(star_rating) if star_rating is not None else None}
    except Exception as exc:
        print(f"[firstback] poll_google_reputation failed (biz {business_id}): {exc}",
              file=sys.stderr, flush=True)
        return None
