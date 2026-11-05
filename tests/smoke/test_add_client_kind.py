"""client_kind on POST /api/me/clients — no database.

Absent or null means business, and the first seeded entity stays kind
``other``. ``individual`` is stored on the workspace profile and the first
entity kind becomes ``personal``. Fixtures are fictional.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.clients import (
    AddClientRequest,
    client_kind_from_profile,
    client_kind_profile_patch,
    first_entity_kind_for,
    individual_invite_bodies,
    invitation_subject,
)

_PERSONA = (
    Path(__file__).resolve().parents[2]
    / "agents"
    / "cpa_team"
    / "setup_assistant"
    / "persona.md"
)


def test_client_kind_defaults_to_business():
    body = AddClientRequest.model_validate({"business_name": "Northwind Books LLC"})
    assert body.client_kind == "business"
    assert first_entity_kind_for(body.client_kind) == "other"
    assert client_kind_profile_patch(body.client_kind) == {"client_kind": "business"}


def test_request_without_client_kind_leaves_existing_fields_unchanged():
    """A caller that still posts only business_name + owner_email is business."""
    raw = {
        "business_name": "Northwind Books LLC",
        "owner_email": "owner@example.com",
        "hipaa_protected": False,
        "isolation_tier": "shared",
    }
    body = AddClientRequest.model_validate(raw)
    assert body.client_kind == "business"
    assert body.business_name == "Northwind Books LLC"
    assert body.owner_email == "owner@example.com"
    assert body.hipaa_protected is False
    assert body.isolation_tier == "shared"
    assert first_entity_kind_for(body.client_kind) == "other"
    # Null and blank are the same as omitting the field.
    assert (
        AddClientRequest.model_validate(
            {"business_name": "Northwind Books LLC", "client_kind": None}
        ).client_kind
        == "business"
    )
    assert (
        AddClientRequest.model_validate(
            {"business_name": "Northwind Books LLC", "client_kind": ""}
        ).client_kind
        == "business"
    )


def test_individual_defaults_first_entity_to_personal():
    body = AddClientRequest.model_validate(
        {
            "business_name": "Jane Doe",
            "owner_email": "jane.doe@example.com",
            "client_kind": "individual",
        }
    )
    assert body.client_kind == "individual"
    assert body.business_name == "Jane Doe"
    assert first_entity_kind_for(body.client_kind) == "personal"
    patch = client_kind_profile_patch(body.client_kind)
    assert patch == {"client_kind": "individual"}
    # The checklist document keeps its own keys; we only read ours back.
    stored = {"version": 1, "steps": {"entity": {"status": "not_started"}}, **patch}
    assert client_kind_from_profile(stored) == "individual"
    assert first_entity_kind_for(client_kind_from_profile(stored)) == "personal"


def test_missing_profile_stays_on_the_business_entity_default():
    assert client_kind_from_profile(None) == "business"
    assert client_kind_from_profile({}) == "business"
    assert client_kind_from_profile({"version": 1, "steps": {}}) == "business"
    assert client_kind_from_profile('{"client_kind": "individual"}') == "individual"
    assert client_kind_from_profile("not-json") == "business"
    assert first_entity_kind_for(None) == "other"
    assert first_entity_kind_for("business") == "other"
    assert first_entity_kind_for("nope") == "other"


def test_unknown_client_kind_is_rejected():
    with pytest.raises(ValidationError):
        AddClientRequest.model_validate(
            {"business_name": "Jane Doe", "client_kind": "personal"}
        )


def test_individual_invitation_says_invited_to_name_without_business():
    name = "Jane Doe"
    subject = invitation_subject(name)
    assert subject == f"You've been invited to {name} on Accounting-Ops"
    assert "business" not in subject.lower()
    text_body, html_body = individual_invite_bodies(
        name,
        "cpa@example.com",
        "https://example.com/invite/accept?token=fixture",
        datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc),
    )
    assert f"You've been invited by cpa@example.com to {name}" in text_body
    assert "business" not in text_body.lower()
    assert "business" not in html_body.lower()
    assert name in html_body
    # A business invitation keeps the same subject shape (no added word).
    assert "business" not in invitation_subject("Northwind Books LLC").lower()


def test_setup_assistant_asks_for_client_name():
    """The DBA prompt is now the client name. Skip-lists are not data here,
    so the question flow itself is otherwise untouched."""
    text = _PERSONA.read_text(encoding="utf-8")
    assert "Client name (business or person)?" in text
    assert "Business name (DBA)?" not in text
    # The sole-prop branch is still prose, not a skippable question table.
    assert "skip_if" not in text
    assert "#### Sole Proprietorship" in text
    assert "#### Personal household" in text
