"""ROI milestone check for FirstBack.

check_roi_milestone(business_id) -> dict | None

Returns a milestone dict ONLY when ALL conditions hold:
  - compliance.a2p_ready(biz) is True (A2P approved; texting is real)
  - booked >= 1 (at least one booking has occurred)
  - roi_multiple >= 2.0 (the plan has paid for itself at least 2x)
  - roi_milestone_sent_at is empty (not already sent — one-time per tenant until reset)

Returns None otherwise (always — never raises).

The 'body' in the returned dict is an HONEST estimate. It never implies collected cash
or an "actual" state; it always says "estimated" and distinguishes owner vs industry-
default avg. The caller (Agent C / post-booking hook) is responsible for sending the
SMS and recording the sent timestamp via db.set_roi_milestone_sent.

SEAM: roi.check_roi_milestone(business_id) -> {"multiple", "revenue", "avg_source", "body"}
"""
import db
import compliance

# Plan 07-3: progressive milestones. check_roi_milestone returns the HIGHEST unfired level
# the business has crossed, so a tenant who jumps past several at once still only gets one
# (the biggest) per booking event, and never repeats a level.
_MILESTONE_LEVELS = [2, 5, 10, 25]

# Honesty invariant (06 §225): never "cash"/"collected"/"actual"; revenue is always an
# estimate with "~$" and the avg-value source labeled.
_LOSS_TAIL = " Without FirstBack, those calls go unanswered and the job likely goes to a competitor."


def _milestone_body(level, revenue, avg_source, multiple):
    """Level-specific, honest milestone copy (estimate language required). Closes with the
    loss-framing sentence (06-2a) outside the estimate parenthetical."""
    avg_label = "your average job value" if avg_source == "owner" else "an industry-average job value"
    if level == 2:
        core = (f"FirstBack has booked an estimated ${revenue:,} in jobs for you so far "
                f"-- about {multiple}x what it costs. (Estimate based on {avg_label}.)")
    elif level == 5:
        core = (f"Milestone: FirstBack has now recovered about 5x its cost -- an estimated "
                f"${revenue:,} in booked jobs since day one. (Estimate based on {avg_label}.)")
    elif level == 10:
        core = (f"Milestone: 10x its cost. That's an estimated ${revenue:,} in jobs that would "
                f"have gone to voicemail. (Estimate based on {avg_label}.)")
    else:  # 25
        core = (f"Milestone: 25x its cost -- an estimated ${revenue:,} in booked jobs since day "
                f"one. (Estimate based on {avg_label}.) At this point FirstBack costs you less "
                f"per booked job than a cup of coffee.")
    return core + _LOSS_TAIL


def check_roi_milestone(business_id):
    """Check whether a progressive ROI milestone is due for this business.

    Returns {"level": int, "multiple": float, "revenue": int, "avg_source": str, "body": str}
    for the highest newly-crossed milestone, or None. Never raises.
    """
    try:
        biz = db.get_business(business_id)
        if not biz:
            return None

        # Gate 1: A2P must be approved (texting reached customers).
        if not compliance.a2p_ready(biz):
            return None

        # Load all-time analytics (source='missed_call' filtered — honest numbers).
        stats = db.analytics(business_id, days=None)
        booked_n = stats["totals"]["booked"]
        roi_multiple = stats.get("roi_multiple")
        revenue = stats.get("revenue", 0)
        avg_source = stats.get("avg_source", "industry_default")

        # Gate 2: at least one booking.
        if booked_n < 1:
            return None

        # Gate 3: roi_multiple >= 2.0 (absorbs industry-default variance).
        if roi_multiple is None or roi_multiple < 2.0:
            return None

        fired = {r["level"] for r in db.get_roi_milestones(business_id)}
        # Back-compat: a tenant who fired the original single milestone (roi_milestone_sent_at
        # set) before this table existed has already had level 2 -- never re-send it.
        if biz.get("roi_milestone_sent_at"):
            fired.add(2)
        # Only ever move UP. Once a level fires, every lower level is satisfied, so a tenant
        # who jumps straight to 25x gets just the 25x message -- never 10x/5x/2x afterward.
        max_fired = max(fired) if fired else 0

        for level in reversed(_MILESTONE_LEVELS):
            if roi_multiple >= level and level > max_fired:
                return {
                    "level": level,
                    "multiple": roi_multiple,
                    "revenue": revenue,
                    "avg_source": avg_source,
                    "body": _milestone_body(level, revenue, avg_source, roi_multiple),
                }
        return None

    except Exception:
        return None
