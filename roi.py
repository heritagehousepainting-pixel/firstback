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


def check_roi_milestone(business_id):
    """Check whether the ROI milestone SMS is due for this business.

    Returns a dict {"multiple": float, "revenue": int, "avg_source": str, "body": str}
    when the milestone should fire, None otherwise. Never raises.
    """
    try:
        biz = db.get_business(business_id)
        if not biz:
            return None

        # Gate 1: A2P must be approved (texting reached customers).
        if not compliance.a2p_ready(biz):
            return None

        # Gate 2: milestone not already sent.
        if biz.get("roi_milestone_sent_at"):
            return None

        # Load all-time analytics (source='missed_call' filtered — honest numbers).
        stats = db.analytics(business_id, days=None)
        booked_n = stats["totals"]["booked"]
        roi_multiple = stats.get("roi_multiple")
        revenue = stats.get("revenue", 0)
        avg_source = stats.get("avg_source", "industry_default")

        # Gate 3: at least one booking.
        if booked_n < 1:
            return None

        # Gate 4: roi_multiple >= 2.0 (absorbs industry-default variance).
        if roi_multiple is None or roi_multiple < 2.0:
            return None

        # Build an honest body — estimate language required; no "actual"/"cash"/"collected".
        if avg_source == "owner":
            avg_label = "your average job value"
        else:
            avg_label = "an industry-average job value"

        body = (
            f"FirstBack has booked an estimated ${revenue:,} in jobs for you so far "
            f"-- about {roi_multiple}x what it costs. "
            f"(Estimate based on {avg_label}.)"
        )

        return {
            "multiple": roi_multiple,
            "revenue": revenue,
            "avg_source": avg_source,
            "body": body,
        }

    except Exception:
        return None
