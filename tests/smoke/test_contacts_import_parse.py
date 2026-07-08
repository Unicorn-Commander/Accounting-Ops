"""Pure-parser smoke tests for vCard / CSV contact import.

No DB — exercises app.contacts.import_parse over representative exports from
macOS Contacts / iPhone (vCard), Google Contacts (CSV) and Outlook (CSV).
"""
from app.contacts.import_parse import parse_contacts


def test_vcard_basic_org_and_person():
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:Jane Roe\r\n"
        "N:Roe;Jane;;;\r\n"
        "ORG:Lavora Remodeling LLC\r\n"
        "EMAIL;TYPE=INTERNET:jane@lavora.com\r\n"
        "TEL;TYPE=CELL:+1-555-200-3000\r\n"
        "ADR;TYPE=WORK:;;123 Main St;Greenville;SC;29601;USA\r\n"
        "END:VCARD\r\n"
    )
    rows = parse_contacts("contacts.vcf", vcf)
    assert len(rows) == 1
    r = rows[0]
    # Company is preferred as the display name for a business contact.
    assert r.display_name == "Lavora Remodeling LLC"
    assert r.person_name == "Jane Roe"
    assert r.company == "Lavora Remodeling LLC"
    assert r.email == "jane@lavora.com"
    assert r.phone == "+1-555-200-3000"
    assert "123 Main St" in (r.address or "")
    assert r.source == "vcard"
    assert r.suggested_kind == "customer"


def test_vcard_person_only_falls_back_to_fn():
    vcf = "BEGIN:VCARD\nVERSION:3.0\nFN:Bob Smith\nEMAIL:bob@x.com\nEND:VCARD\n"
    rows = parse_contacts("c.vcf", vcf)
    assert len(rows) == 1
    assert rows[0].display_name == "Bob Smith"
    assert rows[0].company is None


def test_vcard_multiple_cards_and_dedupe():
    vcf = (
        "BEGIN:VCARD\nFN:A Co\nEMAIL:dup@x.com\nEND:VCARD\n"
        "BEGIN:VCARD\nFN:A Co Again\nEMAIL:dup@x.com\nEND:VCARD\n"  # same email → deduped
        "BEGIN:VCARD\nFN:Other\nEMAIL:other@x.com\nEND:VCARD\n"
    )
    rows = parse_contacts("c.vcf", vcf)
    assert len(rows) == 2


def test_csv_google_headers():
    csv_text = (
        "Name,Given Name,Family Name,Organization Name,"
        "E-mail 1 - Value,Phone 1 - Value\n"
        "Acme Co,,,Acme Co,billing@acme.com,555-1212\n"
    )
    rows = parse_contacts("google.csv", csv_text)
    assert len(rows) == 1
    assert rows[0].display_name == "Acme Co"
    assert rows[0].email == "billing@acme.com"
    assert rows[0].phone == "555-1212"
    assert rows[0].source == "csv"


def test_csv_outlook_first_last_no_company():
    csv_text = (
        "First Name,Last Name,E-mail Address,Company\n"
        "John,Doe,john@doe.com,\n"
    )
    rows = parse_contacts("outlook.csv", csv_text)
    assert len(rows) == 1
    # No company → display name from person.
    assert rows[0].display_name == "John Doe"
    assert rows[0].person_name == "John Doe"
    assert rows[0].email == "john@doe.com"


def test_csv_skips_empty_rows():
    csv_text = "Name,E-mail Address\n,\nReal Co,real@x.com\n"
    rows = parse_contacts("x.csv", csv_text)
    assert len(rows) == 1
    assert rows[0].display_name == "Real Co"


def test_detects_vcard_by_content_even_with_csv_name():
    vcf = "BEGIN:VCARD\nFN:Content Detected\nEND:VCARD\n"
    rows = parse_contacts("mislabeled.csv", vcf)
    assert len(rows) == 1
    assert rows[0].source == "vcard"
