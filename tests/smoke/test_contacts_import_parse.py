"""Pure-parser smoke tests for vCard / CSV contact import.

No DB — exercises app.contacts.import_parse over representative exports from
macOS Contacts / iPhone (vCard), Google Contacts (CSV) and Outlook (CSV).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.contacts import ContactRow
from app.contacts.import_parse import parse_contacts
from app.main import app
from app.tenancy import WorkspaceContext, resolve_workspace

WS_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PARSE_URL = f"/api/workspaces/{WS_ID}/counterparties/parse"


@pytest.fixture(scope="module")
def accountant_client():
    # The app's MCP session manager only allows ONE TestClient lifespan per
    # process, so this module shares a single client for its two HTTP tests.
    app.dependency_overrides[resolve_workspace] = lambda: WorkspaceContext(
        workspace_id=WS_ID,
        workspace_slug="acme-books",
        role="accountant",
        user_uc_uid="user-1",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.pop(resolve_workspace, None)


def test_vcard_basic_org_and_person():
    vcf = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "FN:Jane Roe\r\n"
        "N:Roe;Jane;;;\r\n"
        "ORG:Northwind Remodeling LLC\r\n"
        "EMAIL;TYPE=INTERNET:jane@northwind.com\r\n"
        "TEL;TYPE=CELL:+1-555-200-3000\r\n"
        "ADR;TYPE=WORK:;;123 Main St;Greenville;SC;29601;USA\r\n"
        "END:VCARD\r\n"
    )
    rows = parse_contacts("contacts.vcf", vcf)
    assert len(rows) == 1
    r = rows[0]
    # Company is preferred as the display name for a business contact.
    assert r.display_name == "Northwind Remodeling LLC"
    assert r.person_name == "Jane Roe"
    assert r.company == "Northwind Remodeling LLC"
    assert r.email == "jane@northwind.com"
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


# --------------------------------------------------------------------------- #
# CSV-AO-2: size caps on the import parse request
# --------------------------------------------------------------------------- #
def test_parse_request_over_two_million_chars_is_rejected_422(accountant_client):
    resp = accountant_client.post(
        PARSE_URL, json={"filename": "big.csv", "content": "A,B\n" + "x" * 2_000_000}
    )
    assert resp.status_code == 422
    assert any("content" in str(e.get("loc", "")) for e in resp.json()["detail"])


def test_parse_request_just_under_the_cap_is_accepted(accountant_client):
    # A valid payload just under the 2,000,000-char cap still parses.
    rows = "Name\n" + "".join(f"Person {i}\n" for i in range(10_000))
    assert len(rows) < 2_000_000
    resp = accountant_client.post(PARSE_URL, json={"filename": "ok.csv", "content": rows})
    assert resp.status_code == 200
    assert resp.json()["count"] == 10_000


def test_contact_row_over_long_field_is_rejected_422():
    with pytest.raises(ValueError):
        ContactRow(display_name="x" * 501)


def test_contact_row_normal_values_still_validate():
    row = ContactRow(
        display_name="Acme Co",
        email="billing@acme.com",
        phone="555-1212",
        address="123 Main St, Greenville, SC",
    )
    assert row.display_name == "Acme Co"
    assert row.email == "billing@acme.com"
