"""Integration tests for the 1120 Preparer specialist MCP tools (live stack).

Proves each new tool end-to-end against the real docker-compose stack:

  (1) get_period_journal — authorized call returns a list of entries (may be
      empty on a fresh workspace); cross-workspace denied.
  (2) get_1099_payees — authorized call returns a well-formed payees list;
      cross-workspace denied.
  (3) draft_memo — authorized call inserts a memo row and returns the item_id;
      viewer denied (403 → isError); cross-workspace denied.
  (4) flag_for_cpa_review — authorized call inserts a flagged_position row;
      viewer denied; cross-workspace denied.
  (5) get_form_1120_data — authorized call makes the HTTP request to the CPA
      export sidecar; when the sidecar is not configured, the tool raises a
      runtime error (isError=True) rather than a 5xx on the main service.

All DB-effect assertions (for write tools) read back through the owner pool.
Role denial tests flip seeded membership via _membership_as (same pattern as
test_mcp_write_tools.py), then restore.

Skips cleanly when the stack isn't reachable.
"""
from __future__ import annotations

import json
import uuid

import asyncpg
import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_WORKSPACE_B_ENTITY_ID,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.test_mcp_tools import _call_tool, _result_payload
from tests.integration.test_mcp_write_tools import _membership_as

pytestmark = pytest.mark.integration

_SCHEMA = f"accounting_ops_{str(TEST_WORKSPACE_ID).replace('-', '_')}"


# ---------------------------------------------------------------------------
# (1) get_period_journal — authorized + denied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_period_journal_authorized(seeded_workspace: StackConfig) -> None:
    """get_period_journal executes its SQL and returns a well-formed response."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_period_journal",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "limit": 10,
        },
    )
    assert result.isError is False, result.content
    data = _result_payload(result)
    # Structure check: required fields present, entries is a list.
    assert "entries" in data, f"Missing 'entries' key in: {data}"
    assert "entry_count" in data
    assert "entity_id" in data
    assert "_draft_notice" in data
    assert isinstance(data["entries"], list)


@pytest.mark.asyncio
async def test_get_period_journal_cross_workspace_denied(
    seeded_workspace: StackConfig,
) -> None:
    """get_period_journal for workspace B (no membership) must be denied."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_period_journal",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
        },
    )
    assert result.isError is True, "Cross-workspace call should error"
    text = " ".join(getattr(c, "text", "") for c in result.content).lower()
    assert "membership" in text or "permission" in text or "no active" in text


# ---------------------------------------------------------------------------
# (2) get_1099_payees — authorized + denied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_1099_payees_authorized(seeded_workspace: StackConfig) -> None:
    """get_1099_payees returns a well-formed payees response."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_1099_payees",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "tax_year": 2023,
            "threshold_cents": 60000,
        },
    )
    assert result.isError is False, result.content
    data = _result_payload(result)
    assert "payees" in data, f"Missing 'payees' key in: {data}"
    assert "entity_id" in data
    assert "tax_year" in data
    assert "threshold_cents" in data
    assert "categories_checked" in data
    assert "_draft_notice" in data
    assert isinstance(data["payees"], list)


@pytest.mark.asyncio
async def test_get_1099_payees_cross_workspace_denied(
    seeded_workspace: StackConfig,
) -> None:
    """get_1099_payees for workspace B is denied."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_1099_payees",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "tax_year": 2023,
        },
    )
    assert result.isError is True, "Cross-workspace call should error"


# ---------------------------------------------------------------------------
# (3) draft_memo — authorized + DB effect + viewer denied + cross-workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_memo_authorized_and_db_effect(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """draft_memo inserts a memo row into tax_review_queue and returns item_id."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "draft_memo",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "tax_year": 2023,
            "topic": "Banking interest classification",
            "content": "Entity has $2,400 in Banking · Interest income. Verify line 5.",
            "citations": [{"type": "section", "value": "IRC §61"}],
            "confidence": "medium",
        },
    )
    assert result.isError is False, result.content
    data = _result_payload(result)
    assert data.get("ok") is True
    assert "item_id" in data
    assert data.get("item_type") == "memo"
    assert "_draft_notice" in data

    item_id = data["item_id"]

    # Verify the row was actually inserted in the DB.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT item_type, tax_year, status, submitter_kind "
                "FROM tax_review_queue WHERE id::text = $1",
                item_id,
            )
    assert row is not None, f"Row {item_id} not found in DB"
    assert row["item_type"] == "memo"
    assert row["tax_year"] == 2023
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_draft_memo_viewer_denied(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """draft_memo requires accountant role; a viewer is denied."""
    async with _membership_as(db, seeded_workspace.uc_uid, role="viewer", entity_scope=None):
        result = await _call_tool(
            seeded_workspace.base_url,
            seeded_workspace.token,
            "draft_memo",
            {
                "workspace_id": str(TEST_WORKSPACE_ID),
                "entity_id": str(TEST_ENTITY_ID),
                "tax_year": 2023,
                "topic": "Should be denied",
                "content": "This should not insert.",
            },
        )
    assert result.isError is True, "Viewer should be denied (isError=True)"
    text = " ".join(getattr(c, "text", "") for c in result.content).lower()
    assert any(
        keyword in text
        for keyword in ("permission", "role", "accountant", "403", "forbidden")
    ), f"Expected permission denial in error text, got: {text!r}"


@pytest.mark.asyncio
async def test_draft_memo_cross_workspace_denied(
    seeded_workspace: StackConfig,
) -> None:
    """draft_memo for workspace B (no membership) is denied."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "draft_memo",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "tax_year": 2023,
            "topic": "Cross-workspace attempt",
            "content": "Should be denied.",
        },
    )
    assert result.isError is True, "Cross-workspace call should error"


# ---------------------------------------------------------------------------
# (4) flag_for_cpa_review — authorized + DB effect + viewer denied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_for_cpa_review_authorized_and_db_effect(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """flag_for_cpa_review inserts a flagged_position row and returns item_id."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "flag_for_cpa_review",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "tax_year": 2023,
            "form_reference": "1120-line-26",
            "content": {
                "issue": "SaaS · AI costs may qualify as COGS for a product entity.",
                "affected_line": "26",
                "context": "Entity has $48,000 in SaaS · AI for 2023.",
                "recommendation": "CPA to confirm COGS vs. Other deductions split.",
            },
            "confidence": "medium",
            "citations": [
                {"type": "section", "value": "IRC §263(a)"},
                {"type": "form_line", "value": "1125-A"},
            ],
        },
    )
    assert result.isError is False, result.content
    data = _result_payload(result)
    assert data.get("ok") is True
    assert "item_id" in data
    assert data.get("item_type") == "flagged_position"
    assert data.get("form_reference") == "1120-line-26"
    assert "_draft_notice" in data

    item_id = data["item_id"]

    # Verify DB insertion
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT item_type, form_reference, confidence, status "
                "FROM tax_review_queue WHERE id::text = $1",
                item_id,
            )
    assert row is not None, f"Row {item_id} not found in DB"
    assert row["item_type"] == "flagged_position"
    assert row["form_reference"] == "1120-line-26"
    assert row["confidence"] == "medium"
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_flag_for_cpa_review_viewer_denied(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """flag_for_cpa_review requires accountant role; a viewer is denied."""
    async with _membership_as(db, seeded_workspace.uc_uid, role="viewer", entity_scope=None):
        result = await _call_tool(
            seeded_workspace.base_url,
            seeded_workspace.token,
            "flag_for_cpa_review",
            {
                "workspace_id": str(TEST_WORKSPACE_ID),
                "entity_id": str(TEST_ENTITY_ID),
                "tax_year": 2023,
                "form_reference": "1120-line-26",
                "content": {"issue": "Should be denied"},
                "confidence": "low",
            },
        )
    assert result.isError is True, "Viewer should be denied"
    text = " ".join(getattr(c, "text", "") for c in result.content).lower()
    assert any(
        keyword in text
        for keyword in ("permission", "role", "accountant", "403", "forbidden")
    ), f"Expected permission denial in error text, got: {text!r}"


@pytest.mark.asyncio
async def test_flag_for_cpa_review_cross_workspace_denied(
    seeded_workspace: StackConfig,
) -> None:
    """flag_for_cpa_review for workspace B is denied."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "flag_for_cpa_review",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "tax_year": 2023,
            "form_reference": "1120-line-26",
            "content": {"issue": "Cross-workspace attempt"},
            "confidence": "low",
        },
    )
    assert result.isError is True, "Cross-workspace call should error"


# ---------------------------------------------------------------------------
# (5) get_form_1120_data — expect isError when sidecar not configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_form_1120_data_auth_check(
    seeded_workspace: StackConfig,
) -> None:
    """get_form_1120_data passes auth + reaches the sidecar call (or fails cleanly).

    When the cpa-export sidecar is not running (the default in the itest stack),
    the tool returns isError=True with a runtime/connection error — NOT an auth
    error. This proves:
      - The auth check (viewer floor) PASSES (the caller is owner)
      - The sidecar HTTP call is attempted
      - A connection error or 404 is returned as a clean tool error (not a
        panic / 500 on the main service)

    If the CPA_EXPORT_URL is configured and the sidecar IS running (full CI
    environment), this test may also pass with isError=False and a data payload —
    in which case we assert the _draft_notice is present.
    """
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_form_1120_data",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "tax_year": 2023,
        },
    )
    # The tool should either succeed (sidecar up) or error (sidecar not up).
    # In both cases the error must not be an auth error — auth passed.
    if result.isError:
        text = " ".join(getattr(c, "text", "") for c in result.content).lower()
        # Must NOT be an auth/scope denial:
        assert "membership" not in text and "permission" not in text, (
            f"get_form_1120_data should fail at sidecar, not auth. "
            f"Got: {text!r}"
        )
        # Should be a connection/not-found error from the sidecar attempt:
        assert any(
            keyword in text
            for keyword in ("connect", "http", "export", "cpa", "runtime", "1120")
        ), f"Expected sidecar error, got: {text!r}"
    else:
        # Sidecar responded successfully — verify draft notice
        data = _result_payload(result)
        assert "_draft_notice" in data, "Successful response must include _draft_notice"


@pytest.mark.asyncio
async def test_get_form_1120_data_cross_workspace_denied(
    seeded_workspace: StackConfig,
) -> None:
    """get_form_1120_data for workspace B (no membership) is denied at auth."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_form_1120_data",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "tax_year": 2023,
        },
    )
    assert result.isError is True, "Cross-workspace call should error"
    text = " ".join(getattr(c, "text", "") for c in result.content).lower()
    assert "membership" in text or "permission" in text or "no active" in text


# ---------------------------------------------------------------------------
# New tools are discoverable via /mcp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tax_tools_discovered_in_mcp_tool_list(
    seeded_workspace: StackConfig,
) -> None:
    """The 5 new tax tools are listed when the MCP session is initialized."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {seeded_workspace.token}"}
    async with streamablehttp_client(
        f"{seeded_workspace.base_url}/mcp", headers=headers
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}

    expected_new_tools = {
        "get_form_1120_data",
        "get_period_journal",
        "get_1099_payees",
        "draft_memo",
        "flag_for_cpa_review",
    }
    missing = expected_new_tools - tool_names
    assert not missing, (
        f"Tax tools missing from MCP tool list: {missing}. "
        f"Available tools: {sorted(tool_names)}"
    )
