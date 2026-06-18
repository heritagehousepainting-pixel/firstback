"""SF-8 A2P write-API tests. Run: python test_sf8_write_api.py

Covers:
  - trust_hub_configured() True/False logic
  - create_a2p_brand: simulated when trust_hub_configured False
  - create_a2p_brand: correct payload on mocked 200 (CUSTOMER_CARE not in brand, but UseCase in campaign)
  - create_a2p_brand: error dict on mocked 4xx
  - create_a2p_brand: EIN/address value NOT in any captured stderr
  - create_a2p_messaging_service: simulated; success; error
  - create_a2p_campaign: UseCase=CUSTOMER_CARE always present
  - create_a2p_campaign: IsrId present only when TWILIO_A2P_RESELLER_SID is set
  - create_a2p_campaign: simulated when trust_hub_configured False
  - EIN and business_address values never appear in any stderr log line

No real Twilio calls. Standalone; exit non-zero on failure.
"""
import io
import os
import sys
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest_sf8")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_sf8")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+15550000088")

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.DB_BACKUP_PATH = ""
db.init_db()

# Patch messaging module vars to known test values BEFORE importing
config.TWILIO_TRUST_PRODUCT_SID = ""   # start unconfigured
config.TWILIO_A2P_RESELLER_SID = ""

import messaging

_pass = _fail = 0
_captured_logs = []  # track all stderr output to check EIN/address never logged


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- stderr capture harness ----
class _StderrCapture(io.StringIO):
    """Intercept stderr writes and accumulate them."""
    def write(self, s):
        _captured_logs.append(s)
        return super().write(s)


_stderr_capture = _StderrCapture()
sys.stderr = _stderr_capture


# ---- Helper: produce a mock requests.post that returns a given status/json ----
import requests as _rq

_real_post = _rq.post
_last_call = {}


def _mock_post(status_code, body):
    """Returns a patch function for requests.post that records the call."""
    class _Resp:
        def __init__(self):
            self.status_code = status_code
        def raise_for_status(self):
            if self.status_code >= 400:
                import requests as _r
                raise _r.exceptions.HTTPError(f"HTTP {self.status_code}")
        def json(self):
            return body
    def _patched(url, auth=None, data=None, timeout=None, **kw):
        _last_call.update({"url": url, "data": dict(data or {})})
        return _Resp()
    return _patched


# ============================================================
# 1. trust_hub_configured() when creds present but no product SID
# ============================================================
messaging.TWILIO_TRUST_PRODUCT_SID = ""
messaging.TWILIO_A2P_RESELLER_SID = ""
check("trust_hub_configured() False when TWILIO_TRUST_PRODUCT_SID empty",
      not messaging.trust_hub_configured())

# With product SID set
messaging.TWILIO_TRUST_PRODUCT_SID = "BU_test_product"
check("trust_hub_configured() True when creds + product SID present",
      messaging.trust_hub_configured())

# Without Twilio creds
messaging.TWILIO_ACCOUNT_SID = ""
check("trust_hub_configured() False when account SID missing",
      not messaging.trust_hub_configured())
messaging.TWILIO_ACCOUNT_SID = "ACtest_sf8"


# ============================================================
# 2. create_a2p_brand: simulated when trust_hub_configured False
# ============================================================
messaging.TWILIO_TRUST_PRODUCT_SID = ""
result = messaging.create_a2p_brand({"id": 1, "name": "Test Biz"})
check("create_a2p_brand: simulated when trust_hub unconfigured",
      result == {"status": "simulated"})
# restore
messaging.TWILIO_TRUST_PRODUCT_SID = "BU_test_product"


# ============================================================
# 3. create_a2p_brand: correct payload on mocked 200 (LLC / unknown path)
# ============================================================
_last_call.clear()
_rq.post = _mock_post(200, {"sid": "BN_test_brand001"})
biz_llc = {
    "id": 42,
    "name": "ACME Painting",
    "legal_business_name": "ACME Painting LLC",
    "business_type": "llc",
    "ein": "SECRET_EIN_VALUE_12345",         # value that must never appear in logs
    "business_address": "SECRET_ADDR_999 Main St",  # value that must never appear in logs
    "website": "https://acme.example.com",
    "micro_site_slug": "acme-painting-42",
    "phone": "+15551234567",
}
result = messaging.create_a2p_brand(biz_llc)
check("create_a2p_brand: returns created status on 200",
      result.get("status") == "created")
check("create_a2p_brand: returns brand_sid on 200",
      result.get("brand_sid") == "BN_test_brand001")
check("create_a2p_brand: hit Trust Hub /CustomerProfiles endpoint",
      "CustomerProfiles" in _last_call.get("url", ""))
check("create_a2p_brand: legal_business_name in payload",
      _last_call.get("data", {}).get("FriendlyName") == "ACME Painting LLC")
check("create_a2p_brand LLC: EIN present in payload (BusinessIdentity)",
      "BusinessIdentity" in _last_call.get("data", {}))
check("create_a2p_brand LLC: BusinessType is LLC",
      "Limited Liability Company" in _last_call.get("data", {}).get("BusinessType", ""))


# ============================================================
# 4. create_a2p_brand: error dict on mocked 4xx
# ============================================================
_last_call.clear()
_rq.post = _mock_post(400, {"message": "bad request"})
result_err = messaging.create_a2p_brand(biz_llc)
check("create_a2p_brand: status=error on 4xx",
      result_err.get("status") == "error")
check("create_a2p_brand: error key present on 4xx",
      "error" in result_err)


# ============================================================
# 5. sole_prop brand: NO EIN in payload
# ============================================================
_last_call.clear()
_rq.post = _mock_post(200, {"sid": "BN_sole_prop001"})
biz_sole = {
    "id": 43,
    "name": "Dave Plumbing",
    "legal_business_name": "Dave Plumbing",
    "business_type": "sole_prop",
    "ein": "SECRET_EIN_SHOULD_NOT_BE_IN_PAYLOAD",
    "business_address": "SECRET_ADDR_456 Elm St",
    "phone": "+15559998888",
}
result_sp = messaging.create_a2p_brand(biz_sole)
check("create_a2p_brand sole_prop: returns created",
      result_sp.get("status") == "created")
check("create_a2p_brand sole_prop: EIN absent from payload (BusinessIdentity not sent)",
      "BusinessIdentity" not in _last_call.get("data", {}))
check("create_a2p_brand sole_prop: BusinessType is Sole Proprietorship",
      "Sole Proprietorship" in _last_call.get("data", {}).get("BusinessType", ""))


# ============================================================
# 6. create_a2p_messaging_service: simulated / success / error
# ============================================================
messaging.TWILIO_TRUST_PRODUCT_SID = ""
result_sim = messaging.create_a2p_messaging_service({"id": 1, "name": "X"})
check("create_a2p_messaging_service: simulated when unconfigured",
      result_sim == {"status": "simulated"})
messaging.TWILIO_TRUST_PRODUCT_SID = "BU_test_product"

_last_call.clear()
_rq.post = _mock_post(200, {"sid": "MG_test_svc001"})
result_svc = messaging.create_a2p_messaging_service({"id": 42, "name": "ACME", "legal_business_name": "ACME LLC"})
check("create_a2p_messaging_service: created on 200",
      result_svc.get("status") == "created")
check("create_a2p_messaging_service: messaging_service_sid returned",
      result_svc.get("messaging_service_sid") == "MG_test_svc001")
check("create_a2p_messaging_service: hit /Services endpoint",
      "/Services" in _last_call.get("url", ""))

_rq.post = _mock_post(422, {})
result_svc_err = messaging.create_a2p_messaging_service({"id": 42, "name": "ACME"})
check("create_a2p_messaging_service: error on 4xx",
      result_svc_err.get("status") == "error")


# ============================================================
# 7. create_a2p_campaign: UseCase=CUSTOMER_CARE always present
# ============================================================
messaging.TWILIO_TRUST_PRODUCT_SID = ""
result_camp_sim = messaging.create_a2p_campaign({"id": 1}, "MG_test", "BN_test")
check("create_a2p_campaign: simulated when trust_hub unconfigured",
      result_camp_sim == {"status": "simulated"})
messaging.TWILIO_TRUST_PRODUCT_SID = "BU_test_product"

_last_call.clear()
_rq.post = _mock_post(200, {"sid": "QE_test_campaign001"})
messaging.TWILIO_A2P_RESELLER_SID = ""
result_camp = messaging.create_a2p_campaign(
    {"id": 42, "micro_site_slug": "acme-42"},
    "MG_test_svc001", "BN_test_brand001")
check("create_a2p_campaign: created on 200",
      result_camp.get("status") == "created")
check("create_a2p_campaign: campaign_sid returned",
      result_camp.get("campaign_sid") == "QE_test_campaign001")
check("create_a2p_campaign: UseCase=CUSTOMER_CARE in payload",
      _last_call.get("data", {}).get("UseCase") == "CUSTOMER_CARE")
check("create_a2p_campaign: BrandRegistrationSid in payload",
      _last_call.get("data", {}).get("BrandRegistrationSid") == "BN_test_brand001")
check("create_a2p_campaign: endpoint includes messaging service SID",
      "MG_test_svc001" in _last_call.get("url", ""))
check("create_a2p_campaign: endpoint includes Usa2p",
      "Usa2p" in _last_call.get("url", ""))


# ============================================================
# 8. IsrId present ONLY when TWILIO_A2P_RESELLER_SID is set
# ============================================================
_last_call.clear()
messaging.TWILIO_A2P_RESELLER_SID = ""
_rq.post = _mock_post(200, {"sid": "QE_no_isr"})
messaging.create_a2p_campaign({"id": 42}, "MG_svc", "BN_brand")
check("create_a2p_campaign: IsrId absent when reseller SID not set",
      "IsrId" not in _last_call.get("data", {}))

_last_call.clear()
messaging.TWILIO_A2P_RESELLER_SID = "RES_test_isr001"
_rq.post = _mock_post(200, {"sid": "QE_with_isr"})
messaging.create_a2p_campaign({"id": 42}, "MG_svc", "BN_brand")
check("create_a2p_campaign: IsrId present when TWILIO_A2P_RESELLER_SID set",
      _last_call.get("data", {}).get("IsrId") == "RES_test_isr001")
messaging.TWILIO_A2P_RESELLER_SID = ""


# ============================================================
# 9. EIN value and address value NEVER in captured stderr
# ============================================================
# Flush any remaining stderr output
sys.stderr.flush()
# Restore stderr before printing results
sys.stderr = sys.__stderr__

all_log = "\n".join(_captured_logs)

check("EIN value 'SECRET_EIN_VALUE_12345' NOT in any stderr log",
      "SECRET_EIN_VALUE_12345" not in all_log)
check("EIN value 'SECRET_EIN_SHOULD_NOT_BE_IN_PAYLOAD' NOT in any stderr log",
      "SECRET_EIN_SHOULD_NOT_BE_IN_PAYLOAD" not in all_log)
check("Address value 'SECRET_ADDR_999' NOT in any stderr log",
      "SECRET_ADDR_999" not in all_log)
check("Address value 'SECRET_ADDR_456' NOT in any stderr log",
      "SECRET_ADDR_456" not in all_log)


# ============================================================
# Cleanup
# ============================================================
_rq.post = _real_post
import os as _os
_os.unlink(_TMP.name)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
