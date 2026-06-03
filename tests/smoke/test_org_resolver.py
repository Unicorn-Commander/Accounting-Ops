"""UC org-resolver tests — the workspace→org bridge behind per-org inference
metering + entitlement. Pure-python, mocked; no network/DB.

Guarantees locked here:
  * **Claim-first**: a token org claim wins over the DB column (the long-term
    mechanism — zero rework when Keycloak emits the claim).
  * **Column fallback**: no claim ⇒ read ``public.workspace.uc_org_id``.
  * **Fail-safe**: any error (bad id, DB hiccup) ⇒ ``None``, never raises — a
    chat turn or request must never die on org resolution.
  * The billing seam (``resolve_billing_customer_id``) IS the org, and is
    ``None`` (decline-to-mint) when the org is unknown.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import app.platform.org_resolver as orr


# ---- claim parsing (pure) -------------------------------------------------

def test_claim_string_org_id():
    assert orr._org_from_claims({"org_id": "org_abc"}) == "org_abc"


def test_claim_precedence_order():
    # org_id beats organization beats uc_org_id when several are present.
    claims = {"uc_org_id": "org_z", "organization": "org_y", "org_id": "org_x"}
    assert orr._org_from_claims(claims) == "org_x"


def test_claim_keycloak_dict_organization():
    # Keycloak's native `organization` claim is org-name → attrs.
    assert orr._org_from_claims({"organization": {"org_xyz": {"role": "x"}}}) == "org_xyz"


def test_claim_list_form():
    assert orr._org_from_claims({"uc_org_id": ["org_1", "org_2"]}) == "org_1"


def test_claim_blank_and_missing_are_none():
    assert orr._org_from_claims({"org_id": "   "}) is None
    assert orr._org_from_claims({"org_id": ""}) is None
    assert orr._org_from_claims({"unrelated": "x"}) is None
    assert orr._org_from_claims({}) is None
    assert orr._org_from_claims(None) is None


def test_claim_non_string_scalar_ignored():
    # A numeric/None value under a known key must not crash or be coerced.
    assert orr._org_from_claims({"org_id": 12345}) is None
    assert orr._org_from_claims({"org_id": None}) is None


# ---- resolve_org_id (precedence + fail-safe) ------------------------------

@pytest.mark.asyncio
async def test_resolve_claim_short_circuits_column(monkeypatch):
    # When the token carries an org, the DB is never touched.
    col = AsyncMock(return_value="org_from_db")
    monkeypatch.setattr(orr, "_org_from_column", col)
    got = await orr.resolve_org_id("ws-1", claims={"org_id": "org_from_token"})
    assert got == "org_from_token"
    col.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_falls_back_to_column(monkeypatch):
    monkeypatch.setattr(orr, "_org_from_column", AsyncMock(return_value="org_col"))
    assert await orr.resolve_org_id("ws-1", claims=None) == "org_col"
    assert await orr.resolve_org_id("ws-1", claims={"unrelated": "x"}) == "org_col"


@pytest.mark.asyncio
async def test_resolve_none_when_nothing_known(monkeypatch):
    monkeypatch.setattr(orr, "_org_from_column", AsyncMock(return_value=None))
    assert await orr.resolve_org_id("ws-1", claims=None) is None


@pytest.mark.asyncio
async def test_resolve_is_failsafe_on_db_error(monkeypatch):
    # A DB explosion must degrade to None, not propagate.
    boom = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(orr, "_org_from_column", boom)
    assert await orr.resolve_org_id("ws-1", claims=None) is None


@pytest.mark.asyncio
async def test_column_read_rejects_non_uuid_without_db(monkeypatch):
    # Non-UUID workspace id ⇒ None before any session is opened.
    called = {"opened": False}

    class _Boom:
        def __call__(self, *a, **k):
            called["opened"] = True
            raise AssertionError("SessionLocal should not be opened for a bad id")

    monkeypatch.setattr(orr, "SessionLocal", _Boom())
    assert await orr._org_from_column("not-a-uuid") is None
    assert called["opened"] is False


# ---- billing seam ---------------------------------------------------------

@pytest.mark.asyncio
async def test_billing_customer_id_is_the_org(monkeypatch):
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(orr, "resolve_org_id", AsyncMock(return_value="org_paid"))
    assert await wk.resolve_billing_customer_id("ws-1") == "org_paid"


@pytest.mark.asyncio
async def test_billing_customer_id_none_when_org_unknown(monkeypatch):
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(orr, "resolve_org_id", AsyncMock(return_value=None))
    assert await wk.resolve_billing_customer_id("ws-1") is None
