"""Integration coverage for CPA-console client onboarding (POST /api/me/clients).

The seeded service-token user is ``owner`` of the integration workspace, which
is exactly Morgan's shape (owner of his own practice, no ``cpa`` seat anywhere).
On a live stack this proves, through the real product surface:

  * an owner may add a client, and is seated as the new workspace's OWNER;
  * the persona router now reads them as a practitioner ("cpa") because they
    onboarded a client, and the client list flags the workspace as theirs;
  * in the client workspace the practitioner clears the floors that used to
    refuse a ``cpa`` seat: invitations (incl. a manual-link invite = owner
    floor) and the SimpleFIN bank connect (admin floor — a bogus token is now
    rejected by validation, not by the role gate).

The provisioned workspace is archived at the end so repeated runs do not pile
up active workspaces on the stack.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

_PATH = "/api/me/clients"


@pytest.mark.integration
async def test_add_client_requires_auth(live_stack) -> None:
    async with httpx.AsyncClient(base_url=live_stack.base_url, timeout=30) as c:
        res = await c.post(_PATH, json={"business_name": "No Auth Client"})
    assert res.status_code in (401, 403), res.text


@pytest.mark.integration
async def test_add_client_seats_practitioner_as_owner(client, seeded_workspace, db) -> None:
    name = f"Integration Client {uuid.uuid4().hex[:8]}"
    res = await client.post(_PATH, json={"business_name": name})
    assert res.status_code == 201, res.text
    body = res.json()
    wid = body["workspace_id"]
    assert body["your_role"] == "owner"
    assert body["owner_invitation"] is None
    assert body["slug"].endswith(wid[:8])

    try:
        # Persona: onboarding a client makes a practitioner, even with owner seats only.
        persona = await client.get("/api/me/persona")
        assert persona.status_code == 200, persona.text
        p = persona.json()
        assert p["persona"] == "cpa", p
        assert p["clients_onboarded"] >= 1, p
        assert p["can_add_clients"] is True, p

        # The client list carries the new workspace with the owner seat + provenance flag.
        lst = await client.get(_PATH)
        assert lst.status_code == 200, lst.text
        rows = {w["id"]: w for w in lst.json()["workspaces"]}
        assert wid in rows, rows.keys()
        assert rows[wid]["role"] == "owner"
        assert rows[wid]["onboarded_by_me"] is True
        assert lst.json()["persona"] == "cpa"

        # Owner floor in the client workspace: a manual-link invitation (owners only).
        inv = await client.post(
            f"/api/workspaces/{wid}/invitations",
            json={
                "email": "bookkeeper@example.invalid",
                "role": "bookkeeper",
                "delivery_mode": "manual_link",
                "ttl_days": 1,
            },
        )
        assert inv.status_code == 201, inv.text
        inv_id = inv.json().get("id")
        assert inv_id
        cancel = await client.post(f"/api/workspaces/{wid}/invitations/{inv_id}/cancel")
        assert cancel.status_code in (200, 204), cancel.text

        # Admin floor in the client workspace: the bank-connect gate no longer
        # answers 403. A token that is valid base64 but no claim URL is refused
        # by validation (400) before anything is stored or any network call.
        claim = await client.post(
            f"/api/workspaces/{wid}/credentials/simplefin/claim",
            json={"setup_token": "bm90LWEtdG9rZW4=", "label": "probe"},
        )
        assert claim.status_code == 400, claim.text
        assert "claim URL" in claim.json().get("detail", ""), claim.text
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE public.workspace SET status = 'archived' WHERE id = $1",
                uuid.UUID(wid),
            )
