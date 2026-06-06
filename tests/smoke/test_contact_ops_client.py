"""Pure-function tests for the Contact-Ops federation client.

No network — exercises the JSON-RPC unwrap + person→preview-row mapping over
representative Contact-Ops payloads (summary AND full person shapes).
"""
import json

import pytest

from app.contacts.contact_ops_client import (
    ContactOpsError,
    map_person_to_row,
    parse_jsonrpc_result,
)


def _mcp_envelope(obj):
    return {
        "jsonrpc": "2.0",
        "id": "x",
        "result": {
            "content": [{"type": "text", "text": json.dumps(obj)}],
            "isError": False,
        },
    }


def test_parse_jsonrpc_unwraps_text_content():
    out = parse_jsonrpc_result(_mcp_envelope({"items": [{"id": "1"}], "next_cursor": None}))
    assert out["items"] == [{"id": "1"}]
    assert out["next_cursor"] is None


def test_parse_jsonrpc_structured_content():
    payload = {"jsonrpc": "2.0", "result": {"structuredContent": {"items": []}}}
    assert parse_jsonrpc_result(payload) == {"items": []}


def test_parse_jsonrpc_error_envelope_raises():
    with pytest.raises(ContactOpsError):
        parse_jsonrpc_result({"error": {"code": -32000, "message": "nope"}})


def test_parse_jsonrpc_is_error_result_raises():
    with pytest.raises(ContactOpsError):
        parse_jsonrpc_result({"result": {"content": [], "isError": True}})


def test_map_summary_person():
    row = map_person_to_row(
        {"id": "p1", "display_name": "Acme Co", "primary_email": "ar@acme.com"}
    )
    assert row is not None
    assert row["display_name"] == "Acme Co"
    assert row["email"] == "ar@acme.com"
    assert row["contact_id"] == "p1"
    assert row["source"] == "contact-ops"
    assert row["suggested_kind"] == "customer"


def test_map_full_person_with_emails_phones_employment():
    person = {
        "id": "p2",
        "display_name": "Jane Doe",
        "emails": [
            {"address": "old@x.com", "is_primary": False},
            {"address": "jane@x.com", "is_primary": True},
        ],
        "phones": [{"e164": "+15555551234", "is_primary": True}],
        "current_employments": [{"organization_name": "Doe LLC"}],
    }
    row = map_person_to_row(person)
    assert row["email"] == "jane@x.com"  # primary wins over first
    assert row["phone"] == "+15555551234"
    assert row["company"] == "Doe LLC"
    assert row["contact_id"] == "p2"


def test_map_person_falls_back_to_given_family():
    row = map_person_to_row({"id": "p3", "given_name": "Bob", "family_name": "Smith"})
    assert row["display_name"] == "Bob Smith"


def test_map_person_no_name_returns_none():
    assert map_person_to_row({"id": "p4", "emails": []}) is None
