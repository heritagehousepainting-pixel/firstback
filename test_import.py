"""Contact import checks. Run: python3 test_import.py

Proves the bulk address-book import: the pure vCard/CSV parsers, the pre-sort
(booked -> client, company -> vendor, otherwise leave unclassified), and that the
route + ingest drop PENDING suggestions into the same review inbox while skipping
numbers already in the directory and never resurrecting a dismissed one. Also covers
the gated Google Contacts connection (a no-op until configured). No framework, no
network: a throwaway temp DB + the demo brain, so the real firstback.db is untouched.
Exits non-zero on any failure.
"""
import os
import tempfile
from io import BytesIO

os.environ["FIRSTBACK_PROVIDER"] = "demo"          # deterministic, no network
# Make sure no stray Google creds leak in from the environment -- import must stay a
# gated no-op in this test.
os.environ.pop("GOOGLE_CLIENT_ID", None)
os.environ.pop("GOOGLE_CLIENT_SECRET", None)

import config
config.GOOGLE_CLIENT_ID = ""
config.GOOGLE_CLIENT_SECRET = ""
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import contact_import
import google_contacts
import app
client = app.app.test_client()

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ===========================================================================
# Fixtures
# ===========================================================================
VCARD = """BEGIN:VCARD
VERSION:3.0
FN:Jane Homeowner
TEL;TYPE=CELL:+1 (415) 555-0143
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Acme Paint Supply
ORG:Acme Paint Supply Co;Sales
TEL;TYPE=WORK:(415) 555-0188
TEL;TYPE=FAX:415-555-0190
END:VCARD
BEGIN:VCARD
VERSION:4.0
N:Doe;John;;;
ORG:
item1.TEL;type=cell:tel:+14155550111
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Folded Vendor
ORG:Westside Hardware That Is
 Folded Across Lines
TEL:5105550123
END:VCARD
BEGIN:VCARD
VERSION:2.1
FN:No Number Person
END:VCARD
"""

CSV_GOOGLE = (
    "First Name,Last Name,Organization Name,Phone 1 - Value,Phone 1 - Type,Phone 2 - Value\n"
    "Sam,Client,,(415) 555-0143,Mobile,\n"
    "Bulk,Supplier,Westside Hardware,415-555-0200 ::: 415-555-0201,Work,\n"
    "Nokia,Brick,,,,\n"
)

CSV_OUTLOOK = (
    "First Name,Last Name,Company,Mobile Phone,Home Phone\n"
    "Pat,Neighbor,,503-555-0170,\n"
    "Metro,Supply,Metro Paint Supply,,503-555-0171\n"
)


# ===========================================================================
# Pure parsers
# ===========================================================================
cards = contact_import.parse_vcard(VCARD)
by_name = {c["name"]: c for c in cards}
check("vCard: only cards with a phone are returned (4 of 5)", len(cards) == 4)
check("vCard: reads FN as the name", "Jane Homeowner" in by_name)
check("vCard: keeps the raw phone value",
      by_name.get("Jane Homeowner", {}).get("phones") == ["+1 (415) 555-0143"])
check("vCard: multiple TEL lines -> multiple phones",
      len(by_name.get("Acme Paint Supply", {}).get("phones", [])) == 2)
check("vCard: ORG keeps only the company component (drops ;Sales)",
      by_name.get("Acme Paint Supply", {}).get("org") == "Acme Paint Supply Co")
check("vCard: builds a name from N when FN is absent", "John Doe" in by_name)
check("vCard: strips a group prefix + the tel: URI scheme",
      by_name.get("John Doe", {}).get("phones") == ["+14155550111"])
check("vCard: an empty ORG stays empty", by_name.get("John Doe", {}).get("org") == "")
check("vCard: unfolds a folded value",
      "Folded Across Lines" in by_name.get("Folded Vendor", {}).get("org", ""))

g = contact_import.parse_csv(CSV_GOOGLE)
gb = {c["name"]: c for c in g}
check("CSV(Google): skips a row with no phone (2 of 3)", len(g) == 2)
check("CSV(Google): joins First+Last into a name", "Sam Client" in gb)
check("CSV(Google): reads Organization Name",
      gb.get("Bulk Supplier", {}).get("org") == "Westside Hardware")
check("CSV(Google): splits a ' ::: ' multi-value phone cell",
      gb.get("Bulk Supplier", {}).get("phones") == ["415-555-0200", "415-555-0201"])

o = contact_import.parse_csv(CSV_OUTLOOK)
ob = {c["name"]: c for c in o}
check("CSV(Outlook): reads a 'Mobile Phone' column",
      ob.get("Pat Neighbor", {}).get("phones") == ["503-555-0170"])
check("CSV(Outlook): reads a 'Home Phone' column + Company org",
      ob.get("Metro Supply", {}).get("phones") == ["503-555-0171"]
      and ob.get("Metro Supply", {}).get("org") == "Metro Paint Supply")

check("parse_file: .vcf -> vCard parser", len(contact_import.parse_file("a.vcf", VCARD.encode())) == 4)
check("parse_file: .csv -> CSV parser", len(contact_import.parse_file("a.csv", CSV_GOOGLE.encode())) == 2)
check("parse_file: sniffs vCard content with no extension",
      len(contact_import.parse_file("noext", VCARD.encode())) == 4)


# ===========================================================================
# Pre-sort (pure)
# ===========================================================================
check("presort: a booked number -> 'customer'",
      contact_import.presort({"phones": ["+14155550143"], "org": ""},
                             {"4155550143"})[0] == "customer")
check("presort: a company contact -> 'vendor'",
      contact_import.presort({"phones": ["4155550188"], "org": "Acme"}, set())[0] == "vendor")
check("presort: a plain person -> None (left engaged)",
      contact_import.presort({"phones": ["4155550999"], "org": ""}, set()) is None)
check("presort: booking beats a company name",
      contact_import.presort({"phones": ["4155550143"], "org": "Acme"},
                             {"4155550143"})[0] == "customer")


# ===========================================================================
# Google People API mapping (pure)
# ===========================================================================
person = {"names": [{"displayName": "Grace Lee"}],
          "organizations": [{"name": "Lee Drywall"}],
          "phoneNumbers": [{"value": "+1 503-555-0190"}, {"value": "5035550191"}]}
pc = google_contacts._person_to_contact(person)
check("People API: maps displayName/org/phones",
      pc["name"] == "Grace Lee" and pc["org"] == "Lee Drywall" and len(pc["phones"]) == 2)
check("People API: an empty person yields no phones",
      google_contacts._person_to_contact({})["phones"] == [])


# ===========================================================================
# Gated Google Contacts connection (no creds -> a safe no-op)
# ===========================================================================
check("google_contacts.configured() is False without creds", google_contacts.configured() is False)
check("google_contacts.is_connected() is False initially", google_contacts.is_connected(1) is False)
# set_oauth_tokens round-trips, and disconnect truly clears the refresh token.
db.set_oauth_tokens(1, "google_contacts", "AT", "RT", "2030-01-01T00:00:00+00:00")
check("set_oauth_tokens stores the refresh token + marks connected",
      (db.get_integration(1, "google_contacts") or {}).get("refresh_token") == "RT"
      and google_contacts.is_connected(1) is True)
google_contacts.disconnect(1)
_intg = db.get_integration(1, "google_contacts") or {}
check("disconnect clears the tokens + marks disconnected",
      _intg.get("connected") == 0 and not _intg.get("refresh_token")
      and google_contacts.is_connected(1) is False)


# ===========================================================================
# Ingest: pre-sort parsed contacts into the review queue
# ===========================================================================
BOOKED = "+14155550143"
_bl = db.create_lead(1, "Jane Homeowner", BOOKED)
db.book_appointment(1, _bl, "Mon Jun 23 · 9:00 AM", day="2026-06-23", slot_time="09:00")
db.set_contact(1, "+14155550200", "vendor", name="Already screened")   # already in directory

contacts = [
    {"name": "Jane Homeowner", "org": "", "phones": [BOOKED]},                    # -> customer
    {"name": "Westside Hardware", "org": "Westside Hardware",
     "phones": ["415-555-0200", "415-555-0201"]},                                # 0200 skipped, 0201 vendor
    {"name": "Random Friend", "org": "", "phones": ["+14155550999"]},            # -> None
]
summary = contact_import.ingest(1, contacts, "import-file")
pending = {s["number"]: s for s in db.list_suggestions(1, "pending")}
check("ingest: counts every parsed contact", summary["contacts"] == 3)
check("ingest: a booked match becomes a pending 'customer' suggestion",
      pending.get("4155550143", {}).get("suggested_category") == "customer")
check("ingest: a company contact becomes a pending 'vendor' suggestion",
      pending.get("4155550201", {}).get("suggested_category") == "vendor")
check("ingest: a plain person is left unclassified (no suggestion)", "4155550999" not in pending)
check("ingest: skips a number already in the directory", "4155550200" not in pending)
check("ingest: summary tallies (1 client, 1 vendor, >=1 skipped, >=1 unclassified)",
      summary["customers"] == 1 and summary["vendors"] == 1
      and summary["skipped"] >= 1 and summary["unclassified"] >= 1)
check("ingest: records the import source",
      pending.get("4155550201", {}).get("source") == "import-file")

# A dismissed suggestion is not resurrected by re-importing the same file.
db.set_suggestion_status(pending["4155550201"]["id"], "dismissed")
contact_import.ingest(1, contacts, "import-file")
check("ingest: never re-raises a dismissed suggestion",
      all(s["number"] != "4155550201" for s in db.list_suggestions(1, "pending")))


# ===========================================================================
# Routes (signed in as the seeded owner)
# ===========================================================================
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
with client.session_transaction() as _s:
    _s["csrf_token"] = "test_csrf"
client.environ_base["HTTP_X_CSRF_TOKEN"] = "test_csrf"
page = client.get("/callers").get_data(as_text=True)
check("/callers renders the import card", "Import your contacts" in page)
check("/callers shows 'Coming soon' for Google when unconfigured", "Coming soon" in page)

r = client.post("/api/contacts/import",
                data={"file": (BytesIO(VCARD.encode()), "phone.vcf")},
                content_type="multipart/form-data")
j = r.get_json()
check("POST /api/contacts/import accepts a vCard upload", r.status_code == 200 and j.get("ok"))
check("import route reports the parsed contact count", j.get("contacts") == 4)
after = {s["number"]: s for s in db.list_suggestions(1, "pending")}
check("import route queues a company as a pending 'vendor' suggestion",
      after.get("4155550188", {}).get("suggested_category") == "vendor")

check("import route rejects a request with no file (400)",
      client.post("/api/contacts/import", data={}, content_type="multipart/form-data").status_code == 400)
check("import route rejects a file with no contacts (400)",
      client.post("/api/contacts/import",
                  data={"file": (BytesIO(b"hello world, not a contact"), "x.csv")},
                  content_type="multipart/form-data").status_code == 400)

rc = client.get("/api/contacts/google/connect")
check("connect route redirects to an 'unconfigured' notice when Google is off",
      rc.status_code in (301, 302, 303) and "gcerror=unconfigured" in rc.headers.get("Location", ""))
check("sync route refuses when not connected (400)",
      client.post("/api/contacts/google/sync").status_code == 400)


os.unlink(_TMP.name)
print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
