"""Integration coverage for self-serve first-workspace provisioning.

Exercises auth + the duplicate-guard on a live stack. The CREATE path
(zero-membership caller -> new schema + owner membership) reuses the exact
``scripts.provision_workspace.provision_workspace`` machinery the admin
``create_workspace`` tool already uses; the single-service-token integration
harness cannot represent a *tokened* zero-membership identity (its fixtures bind
the token's uc_uid to the seeded workspace as owner), so the create path is
validated via the smoke model tests + a live dogfood smoke (call as an
already-onboarded user -> ``already_existed=true``). Here we assert the two
behaviours the harness CAN represent: auth-required, and the no-duplicate guard.
"""
from __future__ import annotations

import httpx
import pytest

_PATH = "/api/onboarding/provision-first-workspace"


@pytest.mark.integration
async def test_provision_first_workspace_requires_auth(live_stack) -> None:
    async with httpx.AsyncClient(base_url=live_stack.base_url, timeout=30) as c:
        res = await c.post(_PATH, json={"display_name": "No Auth Co"})
    assert res.status_code in (401, 403), res.text


@pytest.mark.integration
async def test_provision_first_workspace_does_not_duplicate(client, seeded_workspace) -> None:
    # The seeded service-token user already owns a workspace, so the self-serve
    # endpoint must hand it back rather than mint a second one.
    res = await client.post(_PATH, json={"display_name": "Should Not Duplicate"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["already_existed"] is True
    assert body["workspace_id"]


@pytest.mark.integration
async def test_provision_rejects_bad_isolation_tier(client, seeded_workspace) -> None:
    res = await client.post(
        _PATH, json={"display_name": "Tiers Co", "isolation_tier": "platinum"}
    )
    assert res.status_code == 400, res.text
