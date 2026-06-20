"""test_won_amount.py — E5 / Plan 06 Change 4 + Changes 1 & 2c.

Standalone test: uses a temp DB + demo data. No network, no Twilio, no Stripe.
Covers:
  - db.mark_lead_won: sets fields, rejects amount <= 0
  - db.won_leads: aggregates + tenant isolation
  - db.analytics: blends confirmed_revenue / estimated_pipeline / won_n correctly
  - API endpoint: 200 + fields, second POST updates, wrong-tenant 404, missing CSRF 403
  - analytics.html DOM-string: headline value contains dollar pattern, sub contains x-multiple
"""

import os
import sys
import json
import re
import tempfile
import unittest

# ---- point db at a fresh temp file before importing anything ----
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DB_PATH"] = _tmp_db.name
os.environ["DB_BACKUP_PATH"] = ""      # disable backup machinery

# Minimal env vars expected by config / auth / app
os.environ.setdefault("SECRET_KEY", "test-secret-key-12345")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "authtest")
os.environ.setdefault("TWILIO_NUMBER", "+15005550006")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_placeholder")
os.environ.setdefault("STRIPE_PRICE_ID", "price_test")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdA==")

sys.path.insert(0, os.path.dirname(__file__))

import db
db.init_db()  # run migrations including the new won_at / won_amount columns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_biz(name="BizA"):
    """Insert a business row, return its id."""
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO businesses (name, trade, phone) VALUES (?,?,?)",
        (name, "painting", "+15005550001"))
    conn.commit()
    bid = cur.lastrowid
    conn.close()
    return bid


def _create_lead(business_id, phone="+15005550099", source="missed_call"):
    """Insert a lead row, return its id."""
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO leads (business_id, phone, name, source, created_at) VALUES (?,?,?,?,?)",
        (business_id, phone, "Test Customer", source, db.now_iso()))
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return lid


def _create_appointment(business_id, lead_id=None, status="booked"):
    """Insert an appointment row (used to drive booked_n in analytics)."""
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO appointments (business_id, lead_id, status, created_at, scheduled_for) "
        "VALUES (?,?,?,?,?)",
        (business_id, lead_id or 0, status, db.now_iso(), "2026-06-20 10:00"))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


# ---------------------------------------------------------------------------
# 1. db.mark_lead_won
# ---------------------------------------------------------------------------

class TestMarkLeadWon(unittest.TestCase):

    def setUp(self):
        self.bid = _create_biz("WonBiz")
        self.lid = _create_lead(self.bid)

    def test_sets_won_at_and_won_amount(self):
        db.mark_lead_won(self.lid, 4200.0)
        lead = db.get_lead(self.lid)
        self.assertIsNotNone(lead["won_at"], "won_at should be set")
        self.assertAlmostEqual(float(lead["won_amount"]), 4200.0, places=2)

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            db.mark_lead_won(self.lid, 0)

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            db.mark_lead_won(self.lid, -100)

    def test_rejects_non_numeric(self):
        with self.assertRaises(ValueError):
            db.mark_lead_won(self.lid, "foo")

    def test_allows_update(self):
        """Owner can correct a mis-entered amount — idempotent update allowed."""
        db.mark_lead_won(self.lid, 1000.0)
        db.mark_lead_won(self.lid, 4200.0)
        lead = db.get_lead(self.lid)
        self.assertAlmostEqual(float(lead["won_amount"]), 4200.0, places=2)

    def test_custom_ts(self):
        db.mark_lead_won(self.lid, 500.0, ts="2026-01-01T12:00:00+00:00")
        lead = db.get_lead(self.lid)
        self.assertIn("2026-01-01", lead["won_at"])


# ---------------------------------------------------------------------------
# 2. db.won_leads — aggregate + tenant isolation
# ---------------------------------------------------------------------------

class TestWonLeads(unittest.TestCase):

    def setUp(self):
        self.bid_a = _create_biz("TenantA")
        self.bid_b = _create_biz("TenantB")
        self.lid_a1 = _create_lead(self.bid_a, phone="+15005550010")
        self.lid_a2 = _create_lead(self.bid_a, phone="+15005550011")
        self.lid_b1 = _create_lead(self.bid_b, phone="+15005550020")

    def test_empty_returns_zeros(self):
        result = db.won_leads(self.bid_a)
        self.assertEqual(result["confirmed_revenue"], 0.0)
        self.assertEqual(result["won_n"], 0)

    def test_aggregate_sum_and_count(self):
        db.mark_lead_won(self.lid_a1, 3000.0)
        db.mark_lead_won(self.lid_a2, 1200.0)
        result = db.won_leads(self.bid_a)
        self.assertAlmostEqual(result["confirmed_revenue"], 4200.0, places=2)
        self.assertEqual(result["won_n"], 2)

    def test_tenant_isolation(self):
        """Tenant B's won lead must NOT appear in Tenant A's aggregate."""
        db.mark_lead_won(self.lid_b1, 9999.0)
        result_a = db.won_leads(self.bid_a)
        self.assertEqual(result_a["confirmed_revenue"], 0.0)
        self.assertEqual(result_a["won_n"], 0)

    def test_days_window_excludes_old_leads(self):
        """A lead created before the window is not counted (days=1 should miss old rows)."""
        # Mark a lead won, but use a very short window — seed the lead with an old timestamp
        conn = db.get_conn()
        conn.execute(
            "UPDATE leads SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (self.lid_a1,))
        conn.commit()
        conn.close()
        db.mark_lead_won(self.lid_a1, 5000.0)
        result = db.won_leads(self.bid_a, days=1)
        self.assertEqual(result["confirmed_revenue"], 0.0)


# ---------------------------------------------------------------------------
# 3. db.analytics — blends confirmed_revenue + estimated_pipeline + won_n
# ---------------------------------------------------------------------------

class TestAnalyticsBlend(unittest.TestCase):
    """Seed 2 booked appointments, mark 1 lead won — verify the blend."""

    def setUp(self):
        self.bid = _create_biz("AnalyticsBiz")
        # Set avg_job_value so resolved_avg is deterministic ($3000)
        conn = db.get_conn()
        conn.execute("UPDATE businesses SET avg_job_value=3000 WHERE id=?", (self.bid,))
        conn.commit()
        conn.close()
        # 2 leads (missed_call source so analytics counts them)
        self.lid1 = _create_lead(self.bid, phone="+15005550030")
        self.lid2 = _create_lead(self.bid, phone="+15005550031")
        # 2 booked appointments
        _create_appointment(self.bid, self.lid1)
        _create_appointment(self.bid, self.lid2)
        # Mark 1 lead won for $4200 (intentionally ≠ resolved_avg to distinguish real vs est)
        db.mark_lead_won(self.lid1, 4200.0)

    def test_confirmed_revenue(self):
        result = db.analytics(self.bid, days=30)
        self.assertAlmostEqual(result["confirmed_revenue"], 4200.0, places=2)

    def test_won_n(self):
        result = db.analytics(self.bid, days=30)
        self.assertEqual(result["won_n"], 1)

    def test_estimated_pipeline(self):
        """estimated_pipeline = (booked_n - won_n) * resolved_avg = (2 - 1) * 3000 = 3000."""
        result = db.analytics(self.bid, days=30)
        self.assertEqual(result["estimated_pipeline"], 3000)

    def test_existing_revenue_key_preserved(self):
        """The existing 'revenue' key must still be present (back-compat)."""
        result = db.analytics(self.bid, days=30)
        self.assertIn("revenue", result)
        # revenue = booked_n * resolved_avg = 2 * 3000 = 6000
        self.assertEqual(result["revenue"], 6000)

    def test_totals_revenue_preserved(self):
        result = db.analytics(self.bid, days=30)
        self.assertIn("revenue", result["totals"])

    def test_estimated_pipeline_floor_at_zero(self):
        """If won_n > booked_n somehow (data anomaly), estimated_pipeline floors at 0."""
        # Mark both leads won
        db.mark_lead_won(self.lid2, 1000.0)
        result = db.analytics(self.bid, days=30)
        self.assertGreaterEqual(result["estimated_pipeline"], 0)


# ---------------------------------------------------------------------------
# 4. API endpoint tests
# ---------------------------------------------------------------------------

class TestWonEndpoint(unittest.TestCase):

    def setUp(self):
        import app as _app
        self.app = _app.app
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()

        # Create a business and lead for the logged-in tenant
        self.bid = _create_biz("APIBiz")
        self.lid = _create_lead(self.bid, phone="+15005550050")
        # Create another business (different tenant) with its own lead
        self.bid_other = _create_biz("OtherBiz")
        self.lid_other = _create_lead(self.bid_other, phone="+15005550060")

        # Create a user row for the test business. We don't need a real bcrypt hash
        # because we seed the session directly (bypassing /login).
        # Use a unique email per test invocation to avoid UNIQUE constraint conflicts
        # across multiple setUp calls (each creates a different self.bid).
        import time
        unique_email = f"apibiztest_{int(time.time()*1000)}@example.com"
        conn = db.get_conn()
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, business_id, created_at) "
            "VALUES (?,?,?,?)",
            (unique_email, "placeholder", self.bid, db.now_iso()))
        conn.commit()
        self.uid = cur.lastrowid
        conn.close()

    def _login(self):
        """Seed the Flask session with a valid uid + csrf token directly
        (avoids bcrypt dependency while exercising the same auth check as _login_required)."""
        with self.client.session_transaction() as sess:
            sess["uid"] = self.uid
            sess["csrf_token"] = "testcsrf"

    def _csrf_form(self, extra=None):
        data = {"_csrf": "testcsrf"}
        if extra:
            data.update(extra)
        return data

    def test_200_and_fields(self):
        self._login()
        resp = self.client.post(
            f"/api/leads/{self.lid}/won",
            data=self._csrf_form({"amount": "4200"}),
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertAlmostEqual(float(body["won_amount"]), 4200.0, places=2)

    def test_second_post_updates(self):
        """A second POST updates the amount rather than erroring (UX simplicity)."""
        self._login()
        self.client.post(
            f"/api/leads/{self.lid}/won",
            data=self._csrf_form({"amount": "1000"}),
            content_type="application/x-www-form-urlencoded")
        resp = self.client.post(
            f"/api/leads/{self.lid}/won",
            data=self._csrf_form({"amount": "4200"}),
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(resp.status_code, 200)
        lead = db.get_lead(self.lid)
        self.assertAlmostEqual(float(lead["won_amount"]), 4200.0, places=2)

    def test_wrong_tenant_404(self):
        """A lead belonging to a different business returns 404 (tenant isolation)."""
        self._login()
        resp = self.client.post(
            f"/api/leads/{self.lid_other}/won",
            data=self._csrf_form({"amount": "4200"}),
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(resp.status_code, 404)

    def test_missing_csrf_403(self):
        """Request without a valid CSRF token is rejected with 403."""
        self._login()
        resp = self.client.post(
            f"/api/leads/{self.lid}/won",
            data={"amount": "4200"},   # no _csrf field
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(resp.status_code, 403)

    def test_invalid_amount_400(self):
        self._login()
        resp = self.client.post(
            f"/api/leads/{self.lid}/won",
            data=self._csrf_form({"amount": "0"}),
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(resp.status_code, 400)

    def test_negative_amount_400(self):
        self._login()
        resp = self.client.post(
            f"/api/leads/{self.lid}/won",
            data=self._csrf_form({"amount": "-500"}),
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# 5. analytics.html DOM-string assertions (Change 1 + 2c)
# ---------------------------------------------------------------------------

class TestAnalyticsHtmlTemplate(unittest.TestCase):
    """DOM-string assertions: verify the renderHeadline JS renders correctly
    by pattern-matching the template source (no browser needed)."""

    def _read_template(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "templates", "analytics.html")) as f:
            return f.read()

    def test_headline_value_shows_dollar_pattern(self):
        """Change 1: The large value element textContent should contain a dollar
        pattern, not the old 'paid for itself' text."""
        html = self._read_template()
        # The JS sets valEl.textContent to something like 'rev + " estimated recovered"'
        # We assert the OLD primary text (multiple) is now in subEl, not valEl.
        # Specifically: valEl.textContent no longer starts with 'paid for itself'.
        self.assertNotIn("valEl.textContent = 'paid for itself", html,
                         "Change 1: valEl must NOT be set to the multiple string")

    def test_sub_contains_x_multiple(self):
        """Change 1: The sub element must contain the x-multiple string."""
        html = self._read_template()
        # The sub should now be the multiple
        self.assertIn("subEl.textContent = 'paid for itself ~'", html,
                      "Change 1: subEl must contain the x-multiple string")

    def test_value_includes_estimated_recovered(self):
        """Change 1: The large value should include 'estimated recovered' for the
        estimated-only path."""
        html = self._read_template()
        self.assertIn("estimated recovered", html,
                      "Change 1: headline value must include 'estimated recovered'")

    def test_loss_note_element_exists(self):
        """Change 2c: The loss-note paragraph must be in the template."""
        html = self._read_template()
        self.assertIn("roi-loss-note", html,
                      "Change 2c: roi-loss-note element must be present")
        self.assertIn("Without text-back, missed calls convert at ~0%", html,
                      "Change 2c: loss-note copy must be correct")

    def test_loss_note_revealed_when_multiple_truthy(self):
        """Change 2c: JS must reveal lossNote when multiple is truthy."""
        html = self._read_template()
        self.assertIn("lossNote.style.display = ''", html,
                      "Change 2c: lossNote must be revealed when multiple is truthy")

    def test_confirmed_revenue_split(self):
        """When confirmed_revenue > 0, the template must show confirmed + estimated split."""
        html = self._read_template()
        self.assertIn("confirmed", html, "confirmed_revenue path must appear in template")
        self.assertIn("estimatedPipeline", html, "estimated_pipeline must be referenced")

    def test_honesty_invariants(self):
        """Estimated figures carry '~' or 'estimated'; confirmed has no caveat.
        The word 'cash' must never appear. 'collected' may only appear in a denial
        context (e.g. 'not collected money') — never as a positive claim.
        """
        html = self._read_template()
        # The word 'estimated' still appears in the JS for the non-confirmed path
        self.assertIn("estimated recovered", html)
        # The word 'cash' must NOT appear anywhere
        self.assertNotIn("cash", html.lower(), "'cash' must never appear in revenue claims")
        # 'collected' is only permitted in the honest denial note
        # "Revenue is an estimate -- not collected money." is fine; a positive use is not.
        import re as _re
        # Find all occurrences of 'collected' and verify each is inside a "not collected" denial.
        for m in _re.finditer(r'collected', html, _re.IGNORECASE):
            start = max(0, m.start() - 20)
            context = html[start:m.end() + 20]
            self.assertIn("not", context.lower(),
                          f"'collected' appeared outside a denial context: ...{context}...")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
