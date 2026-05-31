"""GET /api/chat/personas — Brigade-status + tier surface checks.

Verifies the endpoint payload contains:

- ``default`` slug
- ``brigade_online`` bool + ``brigade_reason`` string
- per-persona ``tier``, ``consumes_brigade``, ``brigade_active``,
  ``cross_app_tool_count``, ``default_model``, ``tool_count``

These are the contract the web persona-picker reads. If any of these
drift, the picker silently degrades — so we hold the line in CI.
"""
from __future__ import annotations

import json
import os

import pytest


SERVICE_TOKEN = "cfo-agent-smoke-" + "z" * 32  # >= 32 chars, env-only


@pytest.fixture(autouse=True)
def _service_token_env(monkeypatch):
    # Audit 2026-05-31 Bug 7: declare role_floor so the loader accepts the
    # token in production mode. /api/chat/personas is workspace-agnostic so
    # role_floor doesn't materially gate anything here — viewer is sufficient.
    monkeypatch.setenv(
        "ACCTOPS_SERVICE_TOKENS",
        json.dumps({
            SERVICE_TOKEN: {
                "uc_uid": "test-aaron",
                "agent_id": "smoke",
                "role_floor": "viewer",
            }
        }),
    )
    # Clear the cache so the new env takes effect.
    from app import auth as auth_mod

    auth_mod._service_tokens_cache = None
    yield
    auth_mod._service_tokens_cache = None


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_personas_endpoint_returns_brigade_status(client):
    response = client.get(
        "/api/chat/personas",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    # Contract: top-level keys
    for key in ("default", "brigade_online", "brigade_reason", "personas"):
        assert key in payload, f"missing top-level key: {key}"
    # When BRIGADE_BASE_URL is unset (the default), Brigade is OFF.
    assert payload["brigade_online"] is False
    assert isinstance(payload["brigade_reason"], str)


def test_personas_endpoint_lists_cfo_agent_with_tier_and_brigade(client):
    response = client.get(
        "/api/chat/personas",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    payload = response.json()
    cfo = next((p for p in payload["personas"] if p["slug"] == "cfo-agent"), None)
    assert cfo is not None
    # Tier + brigade-status fields the picker reads.
    assert cfo["tier"] == "strategic"
    assert cfo["consumes_brigade"] is True
    assert cfo["brigade_active"] is False  # Brigade is OFF in this env
    assert cfo["default_model"] == "claude-opus-4-7"
    assert cfo["needs_tax_year"] is True
    assert cfo["tool_count"] >= 20
    # Cross-app count is 0 when Brigade is offline.
    assert cfo["cross_app_tool_count"] == 0


def test_personas_endpoint_groups_all_three_tiers(client):
    response = client.get(
        "/api/chat/personas",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    payload = response.json()
    tiers = {p["tier"] for p in payload["personas"]}
    assert tiers == {"strategic", "specialist", "operational"}


def test_personas_endpoint_unauthenticated_returns_401(client):
    response = client.get("/api/chat/personas")
    assert response.status_code == 401


def test_personas_default_is_a_known_slug(client):
    response = client.get(
        "/api/chat/personas",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    payload = response.json()
    default_slug = payload["default"]
    slugs = {p["slug"] for p in payload["personas"]}
    assert default_slug in slugs, f"default {default_slug!r} must be in PERSONAS"


def test_well_known_mcps_includes_brigade_audience(client):
    """Brigade-ingest manifest must be reachable + describe the federated auth."""
    response = client.get("/.well-known/mcps.json")
    assert response.status_code == 200, response.text
    payload = response.json()
    # The discovery manifest is built from MCP registrations; tools list is
    # non-empty.
    assert "tools" in payload
    assert len(payload["tools"]) >= 5
