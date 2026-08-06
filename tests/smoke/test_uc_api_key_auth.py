"""Smoke tests for UC API key (uc_*) auth on the MCP surface.

Unit-level: looks_like_uc_key, resolve_uc_api_key mapping with httpx mocked.
Does not hit live Ops-Center or the live stack.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.security.uc_api_key import (
    APP_MCP_PERMISSION,
    looks_like_uc_key,
    resolve_uc_api_key,
)


def test_looks_like_uc_key_accepts_wire_format():
    assert looks_like_uc_key("uc_" + "a" * 64)
    assert not looks_like_uc_key("aops_pat_xxx")
    assert not looks_like_uc_key("uc_")
    assert not looks_like_uc_key("")
    assert not looks_like_uc_key("Bearer uc_abc")


@pytest.mark.asyncio
async def test_resolve_uc_api_key_valid():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "valid": True,
        "user": {
            "id": "user-sub-123",
            "permissions": ["llm:inference", "mcp:accounting-ops"],
            "key_id": "key-uuid",
            "subscription_tier": "starter",
            "organization": {"id": "org-1"},
        },
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=fake_resp)

    with patch("app.security.uc_api_key.httpx.AsyncClient", return_value=mock_client):
        with patch("app.security.uc_api_key.get_settings") as gs:
            gs.return_value.ops_center_base_url = "https://unicorncommander.ai"
            ident = await resolve_uc_api_key("uc_" + "b" * 64)

    assert ident is not None
    assert ident.uc_uid == "user-sub-123"
    assert APP_MCP_PERMISSION in ident.permissions
    assert ident.key_id == "key-uuid"
    # required_permission must be sent
    args, kwargs = mock_client.post.call_args
    assert kwargs["json"]["required_permission"] == APP_MCP_PERMISSION


@pytest.mark.asyncio
async def test_resolve_uc_api_key_missing_permission():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "valid": False,
        "reason": "missing_permission",
        "permissions": ["llm:inference", "llm:models"],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=fake_resp)

    with patch("app.security.uc_api_key.httpx.AsyncClient", return_value=mock_client):
        with patch("app.security.uc_api_key.get_settings") as gs:
            gs.return_value.ops_center_base_url = "https://unicorncommander.ai"
            ident = await resolve_uc_api_key("uc_" + "c" * 64)

    assert ident is None


@pytest.mark.asyncio
async def test_resolve_uc_api_key_invalid():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"valid": False}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=fake_resp)

    with patch("app.security.uc_api_key.httpx.AsyncClient", return_value=mock_client):
        with patch("app.security.uc_api_key.get_settings") as gs:
            gs.return_value.ops_center_base_url = "https://unicorncommander.ai"
            ident = await resolve_uc_api_key("uc_" + "d" * 64)

    assert ident is None
