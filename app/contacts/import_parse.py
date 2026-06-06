"""Pure parsers: uploaded vCard / CSV → normalized contact preview rows.

No DB, no I/O — these are deterministic functions over the raw file text so
they're trivially unit-testable and run identically on the server or in a test.
The web layer sends the file's text + name; we return normalized rows the UI
previews. The user tags customer/vendor/skip, then the *import* path writes.

Supported sources (all of which the user's address books export to):
  * vCard 3.0/4.0 (.vcf)  — macOS Contacts, iPhone, Google, Outlook
  * CSV                   — Google Contacts, Outlook, any spreadsheet

A normalized row (``ParsedContact``) is intentionally small:
  display_name (required), person_name, company, email, phone, address,
  suggested_kind ("customer" by default), source ("vcard"|"csv").
"""
from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ParsedContact:
    display_name: str
    person_name: str | None = None
    company: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    suggested_kind: str = "customer"
    source: str = "csv"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def parse_contacts(filename: str, raw_text: str) -> list[ParsedContact]:
    """Dispatch on content/extension and return de-duplicated preview rows.

    vCard is detected by its ``BEGIN:VCARD`` marker (most reliable) or a
    ``.vcf`` extension; everything else is treated as CSV.
    """
    text = raw_text or ""
    name = (filename or "").lower()
    if "BEGIN:VCARD" in text.upper() or name.endswith(".vcf"):
        rows = _parse_vcard(text)
    else:
        rows = _parse_csv(text)
    return _dedupe(rows)


# ---------------------------------------------------------------------------
# vCard
# ---------------------------------------------------------------------------


def _vcard_unescape(value: str) -> str:
    # RFC 6350 escaping: \\ \, \; \n
    return (
        value.replace("\\n", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _unfold(text: str) -> list[str]:
    """vCard line folding: a line beginning with space/tab continues the prior."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _parse_vcard(text: str) -> list[ParsedContact]:
    rows: list[ParsedContact] = []
    card: dict[str, str] = {}
    in_card = False

    for line in _unfold(text):
        upper = line.upper().strip()
        if upper == "BEGIN:VCARD":
            card, in_card = {}, True
            continue
        if upper == "END:VCARD":
            parsed = _vcard_to_contact(card)
            if parsed is not None:
                rows.append(parsed)
            in_card = False
            continue
        if not in_card or ":" not in line:
            continue
        prop, value = line.split(":", 1)
        key = prop.split(";", 1)[0].strip().upper()
        value = _vcard_unescape(value)
        if not value:
            continue
        # First occurrence of each property wins (e.g. EMAIL;TYPE=HOME before WORK).
        card.setdefault(key, value)
        # ADR components are ;-delimited: pobox;ext;street;city;region;postal;country
        if key == "ADR":
            parts = [p.strip() for p in value.split(";") if p.strip()]
            card["_ADR_JOINED"] = ", ".join(parts)

    return rows


def _vcard_to_contact(card: dict[str, str]) -> ParsedContact | None:
    fn = card.get("FN")
    org = card.get("ORG")
    if org:
        org = org.split(";", 1)[0].strip()  # ORG can be "Company;Dept"
    person = fn
    if not person and card.get("N"):
        # N is Family;Given;Additional;Prefix;Suffix
        n = [p.strip() for p in card["N"].split(";")]
        given = n[1] if len(n) > 1 else ""
        family = n[0] if n else ""
        person = " ".join(p for p in (given, family) if p).strip() or None
    # A business contact's display name prefers the company; else the person.
    display = (org or person or "").strip()
    if not display:
        return None
    return ParsedContact(
        display_name=display,
        person_name=person,
        company=org,
        email=card.get("EMAIL"),
        phone=card.get("TEL"),
        address=card.get("_ADR_JOINED"),
        suggested_kind="customer",
        source="vcard",
    )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

# Field → ordered list of header substrings to match (case-insensitive). The
# FIRST column whose header contains any of these wins — so "E-mail 1 - Value"
# (Google) and "E-mail Address" (Outlook) both map to ``email``.
_CSV_FIELD_PATTERNS: dict[str, list[str]] = {
    "name": ["display name", "full name", "contact name", "name"],
    "first": ["first name", "given name"],
    "last": ["last name", "family name", "surname"],
    "company": ["organization name", "organization", "company", "org"],
    "email": ["e-mail", "email"],
    "phone": ["mobile", "phone", "tel"],
    "address": ["street", "address"],
}


def _build_header_map(fieldnames: list[str]) -> dict[str, str]:
    """Map each logical field → the first CSV column header that matches it."""
    mapping: dict[str, str] = {}
    lowered = [(h, (h or "").strip().lower()) for h in fieldnames]
    for fieldname, patterns in _CSV_FIELD_PATTERNS.items():
        for pat in patterns:
            # The generic "name" pattern must not swallow "First Name" /
            # "Last Name" columns — those are handled by the first/last fields.
            if fieldname == "name":
                hit = next(
                    (
                        h
                        for h, low in lowered
                        if pat in low and "first" not in low and "last" not in low
                    ),
                    None,
                )
            else:
                hit = next((h for h, low in lowered if pat in low), None)
            if hit is not None:
                mapping[fieldname] = hit
                break
    return mapping


def _parse_csv(text: str) -> list[ParsedContact]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    hmap = _build_header_map(list(reader.fieldnames))
    rows: list[ParsedContact] = []

    def cell(row: dict[str, str], fieldname: str) -> str:
        col = hmap.get(fieldname)
        return (row.get(col, "") or "").strip() if col else ""

    for row in reader:
        name = cell(row, "name")
        first = cell(row, "first")
        last = cell(row, "last")
        company = cell(row, "company")
        email = cell(row, "email")
        person = name or " ".join(p for p in (first, last) if p).strip()
        display = (company or person or email).strip()
        if not display:
            continue
        rows.append(
            ParsedContact(
                display_name=display,
                person_name=person or None,
                company=company or None,
                email=email or None,
                phone=cell(row, "phone") or None,
                address=cell(row, "address") or None,
                suggested_kind="customer",
                source="csv",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Dedupe (within a single upload)
# ---------------------------------------------------------------------------


def _dedupe(rows: list[ParsedContact]) -> list[ParsedContact]:
    seen: set[str] = set()
    out: list[ParsedContact] = []
    for r in rows:
        key = (r.email or "").strip().lower() or r.display_name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


__all__ = ("ParsedContact", "parse_contacts")
