"""Contact import: bulk-load an owner's address book into the caller-triage review
queue as PRE-SORTED suggestions, instead of typing every number by hand.

Two entry points feed ONE path. A vCard (.vcf) or CSV file the owner exports from
any phone / Google Contacts, and the Google People API (see google_contacts.py),
both produce [{name, org, phones[]}] records that ingest() pre-sorts and drops into
the same `contact_suggestions` review inbox the behavioral scanner already uses.

Design (decided with Jonathan, 2026-06-14):
  * Imports NEVER blanket-screen. Suppressing a whole address book would silence a
    past client who happens to be in the owner's phone, breaking the core promise.
    Every imported contact becomes a PENDING suggestion the owner confirms (in bulk
    if they like); nothing auto-applies.
  * Pre-sort is PURE and conservative, mirroring triage.suggest_category:
      - a number that matches a past booked lead -> suggest 'customer' (a client),
      - a contact carrying a company/org       -> suggest 'vendor'  (a supplier),
      - anyone else is LEFT UNCLASSIFIED (no suggestion) so they stay an engaged
        prospect. Booking is the strongest signal, so it wins over a company name.
  * Off the hot path, multi-tenant by business_id, dependency-light (stdlib only:
    we parse vCard by hand and CSV with `csv`; no vobject/pandas).
  * Honest + privacy-minded: we keep only name, number, and company. Nothing else
    from the file is read or stored.

Parsers are tolerant: a malformed card/row is skipped, never fatal.
"""
import csv
import io
import re

import db


def _digits10(s):
    """Last 10 digits of a phone string -- the per-business directory key, so +1 and
    formatting never matter (mirrors db._digits10)."""
    return re.sub(r"\D", "", str(s or ""))[-10:]


# --------------------------------------------------------------------------
# Pre-sort (pure, testable)
# --------------------------------------------------------------------------
def presort(contact, booked_keys):
    """Pure: recommend (category, reason) for one imported contact, or None to leave
    it an engaged prospect (the safe default that protects the core promise).

    `booked_keys` is the set of last-10-digit strings that have booked an estimate.
    A past client who is also a company is still your client, so booking wins."""
    keys = {k for k in (_digits10(p) for p in (contact.get("phones") or [])) if len(k) >= 10}
    if keys & (booked_keys or set()):
        return ("customer", "In your phone and has booked an estimate with you.")
    org = (contact.get("org") or "").strip()
    if org:
        return ("vendor", "Listed under a company (" + org + ").")
    return None


# --------------------------------------------------------------------------
# vCard (.vcf) parser  --  stdlib only
# --------------------------------------------------------------------------
def parse_vcard(text):
    """Parse vCard text -> [{name, org, phones[]}] (only contacts with a phone).
    Tolerant of vCard 2.1/3.0/4.0, line folding, property groups (item1.TEL), TYPE
    params, and the 4.0 'tel:' URI form. A malformed card is skipped, never fatal."""
    out = []
    cur = None                               # the card being parsed (vCards never nest)
    for line in _unfold(text):
        u = line.strip()
        if not u:
            continue
        up = u.upper()
        if up == "BEGIN:VCARD":
            cur = {"fn": "", "n": "", "org": "", "phones": []}
            continue
        if up == "END:VCARD":
            if cur is not None and cur["phones"]:
                out.append({"name": cur["fn"] or _name_from_n(cur["n"]),
                            "org": cur["org"], "phones": cur["phones"]})
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        value = _unescape_vcard(value.strip())
        prop = head.split(";", 1)[0].strip()
        if "." in prop:                      # strip a "group." prefix (item1.TEL)
            prop = prop.rsplit(".", 1)[-1]
        prop = prop.upper()
        if prop == "FN":
            cur["fn"] = value
        elif prop == "N" and not cur["n"]:
            cur["n"] = value
        elif prop == "ORG" and not cur["org"]:
            cur["org"] = value.split(";")[0].strip()   # ORG:Company;Department
        elif prop == "TEL":
            tel = value[4:].strip() if value.lower().startswith("tel:") else value
            if tel:
                cur["phones"].append(tel)
    return out


def _unfold(text):
    """RFC 6350 line unfolding: a line starting with a space/tab continues the
    previous one. Returns the unfolded logical lines."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = []
    for ln in raw:
        if ln[:1] in (" ", "\t") and lines:
            lines[-1] += ln[1:]
        else:
            lines.append(ln)
    return lines


def _name_from_n(n):
    """Build a display name from a structured N value (Family;Given;...)."""
    if not n:
        return ""
    parts = [p.strip() for p in n.split(";")]
    family = parts[0] if len(parts) > 0 else ""
    given = parts[1] if len(parts) > 1 else ""
    return " ".join(p for p in (given, family) if p)


def _unescape_vcard(v):
    """Unescape vCard text-value backslash escapes (\\n -> space, \\, \\; \\\\)."""
    out, i, n = [], 0, len(v)
    while i < n:
        ch = v[i]
        if ch == "\\" and i + 1 < n:
            out.append({"n": " ", "N": " ", ",": ",", ";": ";", "\\": "\\"}.get(v[i + 1], v[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# CSV parser  --  Google Contacts / Outlook / generic, header-driven
# --------------------------------------------------------------------------
def parse_csv(text):
    """Parse a contacts CSV -> [{name, org, phones[]}] (only contacts with a phone).
    Header-driven and tolerant: detects the name, organization, and EVERY phone
    column by header keywords, so Google's 'Phone 1 - Value'..'Phone N - Value'
    (with ' ::: ' multi-values) and Outlook's 'Mobile Phone'/'Home Phone' both work."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration:
        return []
    low = [(h or "").strip().lower() for h in header]

    def first_exact(*names):
        for i, h in enumerate(low):
            if h in names:
                return i
        return -1

    name_i = first_exact("name", "display name", "full name")
    first_i = first_exact("first name", "given name")
    last_i = first_exact("last name", "family name")
    # Prefer an "Organization Name"/"Organization 1 - Name", then Company, then any.
    org_order = ([i for i, h in enumerate(low) if "organization" in h and "name" in h]
                 + [i for i, h in enumerate(low) if h in ("company", "company name")]
                 + [i for i, h in enumerate(low) if "organization" in h])
    org_idx = list(dict.fromkeys(org_order))
    # Any phone-VALUE column: has "phone" but is not the paired Type/Label column.
    phone_idx = [i for i, h in enumerate(low)
                 if "phone" in h and "type" not in h and "label" not in h]

    out = []
    for row in reader:
        if not any((c or "").strip() for c in row):
            continue

        def cell(i):
            return row[i].strip() if 0 <= i < len(row) and row[i] else ""

        name = cell(name_i) if name_i >= 0 else ""
        if not name:
            name = " ".join(p for p in (cell(first_i), cell(last_i)) if p).strip()
        org = next((cell(i) for i in org_idx if cell(i)), "")
        phones = []
        for i in phone_idx:
            for piece in _split_multi(cell(i)):
                if piece.strip():
                    phones.append(piece.strip())
        if phones:
            out.append({"name": name, "org": org, "phones": phones})
    return out


def _split_multi(v):
    """Google packs several numbers into one cell joined by ' ::: '; also split on
    newlines. (We never split on commas -- a formatted number may contain one.)"""
    if not v:
        return []
    parts = []
    for chunk in v.replace("\r", "\n").split("\n"):
        parts.extend(chunk.split(":::"))
    return parts


def parse_file(filename, data):
    """Dispatch by extension, falling back to content sniffing. `data` is bytes or
    str. Returns [{name, org, phones[]}]."""
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else (data or "")
    name = (filename or "").lower()
    looks_vcard = "BEGIN:VCARD" in text[:512].upper()
    if name.endswith(".vcf") or (not name.endswith(".csv") and looks_vcard):
        return parse_vcard(text)
    return parse_csv(text)


# --------------------------------------------------------------------------
# Ingest: pre-sort + drop into the review queue
# --------------------------------------------------------------------------
def ingest(business_id, contacts, source):
    """Pre-sort parsed contacts and queue them as PENDING suggestions in the review
    inbox. Like triage.scan_suggestions, this never touches a number the owner has
    already put in the directory, nor re-raises one they already accepted/dismissed.
    Returns an honest summary dict for the UI.

    `source` is 'import-file', 'import-google', or 'import-jobber'.

    NOTE: Jobber sync does NOT use this function — it calls db.upsert_suggestion
    directly (see fsm_sync.sync_clients) because presort() drops all new customers
    who haven't previously booked and have no org field, which is 100% of a first
    Jobber sync."""
    classified = {c["number"] for c in db.list_contacts(business_id)}
    decided = set()
    for st in ("accepted", "dismissed"):
        decided |= {s["number"] for s in db.list_suggestions(business_id, st)}
    skip = classified | decided
    booked_keys = {s["number"] for s in db.caller_signals(business_id) if s.get("booked")}

    suggested = customers = vendors = unclassified = skipped = 0
    seen = set()
    for contact in contacts:
        rec = presort(contact, booked_keys)
        if not rec:
            unclassified += 1
            continue
        category, reason = rec
        name = (contact.get("name") or "").strip() or None
        for phone in (contact.get("phones") or []):
            key = _digits10(phone)
            if len(key) < 10 or key in seen:
                continue
            seen.add(key)
            if key in skip:
                skipped += 1
                continue
            db.upsert_suggestion(business_id, key, name, category, reason, source)
            suggested += 1
            if category == "customer":
                customers += 1
            else:
                vendors += 1
    return {"contacts": len(contacts), "suggested": suggested,
            "customers": customers, "vendors": vendors,
            "unclassified": unclassified, "skipped": skipped}
