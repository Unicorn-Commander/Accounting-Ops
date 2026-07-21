"""Data-rights integration tests against the LIVE stack.

Skips (never errors) when the stack is not reachable — same contract as the rest
of the integration tier.

What is proven here that the smoke tier cannot prove:

  1. Export is genuinely tenant-scoped **by the database**, not just by our
     query text. Workspace B's rows must not appear in workspace A's archive
     even though both live in the same physical `books` tables under RLS.
  2. Erasure is owner-gated and refuses without an exact confirmation, against
     the real auth stack rather than an overridden dependency.
  3. **Erasing one workspace cannot break another workspace's hash chain.**

On (3) — the load-bearing claim
--------------------------------
It is proven two ways, one always-on and one opt-in:

  * ALWAYS: a structural proof that the chains are entity-partitioned. Every
    non-genesis `prev_hash` in the database resolves to a `this_hash` belonging
    to the SAME entity. Since every entity belongs to exactly one workspace, no
    chain can span a workspace boundary — so removing one workspace's rows
    cannot orphan another's. This is non-destructive and runs every time.

  * OPT-IN: an actually-destructive end-to-end erasure, gated behind
    ACCTOPS_TEST_ALLOW_DESTRUCTIVE=1 plus a disposable workspace id. It is NOT
    enabled by default, because the shared CI workspace is a fixture other tests
    depend on and a test that silently destroyed it would be worse than no test.
"""
from __future__ import annotations

import io
import json
import os
import uuid
import zipfile

import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.conftest import _set_membership

pytestmark = pytest.mark.integration

BASE = f"/api/workspaces/{TEST_WORKSPACE_ID}/data-rights"


# --------------------------------------------------------------------------- #
# Role gating — against the real auth stack
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "bookkeeper", "accountant", "admin"])
async def test_export_and_erasure_refuse_non_owner_roles(
    seeded_workspace: StackConfig, db, client: httpx.AsyncClient, role
):
    prev_role, prev_scope = await _set_membership(db, seeded_workspace.uc_uid, role, None)
    try:
        r = await client.post(f"{BASE}/exports")
        assert r.status_code == 403, f"{role} could start an export: {r.text}"

        r = await client.post(
            f"{BASE}/erasure",
            json={"confirm": "whatever", "acknowledge_irreversible": True},
        )
        assert r.status_code == 403, f"{role} could request erasure: {r.text}"
    finally:
        await _set_membership(db, seeded_workspace.uc_uid, prev_role, prev_scope)


@pytest.mark.asyncio
async def test_erasure_refuses_without_confirmation(
    seeded_workspace: StackConfig, client: httpx.AsyncClient
):
    """Owner role, but no/!wrong confirmation => nothing happens."""
    for body in (
        {"confirm": "definitely-not-the-slug", "acknowledge_irreversible": True},
        {"confirm": "", "acknowledge_irreversible": True},
    ):
        r = await client.post(f"{BASE}/erasure", json=body)
        assert r.status_code == 400, r.text
        assert "Nothing was deleted" in r.json()["detail"]

    # Correct slug but no acknowledgement is still refused.
    ws = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}")
    if ws.status_code == 200:
        slug = ws.json().get("slug")
        if slug:
            r = await client.post(
                f"{BASE}/erasure",
                json={"confirm": slug, "acknowledge_irreversible": False},
            )
            assert r.status_code == 400, r.text
            assert "acknowledge_irreversible" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_export_produces_an_archive_scoped_to_this_workspace_only(
    seeded_workspace: StackConfig, db, client: httpx.AsyncClient
):
    """End-to-end: request -> poll -> download -> inspect.

    Asserts the archive contains this workspace's data AND that no identifier
    belonging to workspace B appears anywhere in it.
    """
    r = await client.post(f"{BASE}/exports")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    # BackgroundTasks runs after the response is sent; poll for the transition.
    payload = None
    for _ in range(60):
        r = await client.get(f"{BASE}/exports/{job_id}")
        assert r.status_code == 200, r.text
        payload = r.json()
        if payload["status"] in ("completed", "failed"):
            break
        await _sleep(0.5)
    assert payload is not None
    assert payload["status"] == "completed", f"export did not complete: {payload}"

    summary = payload["summary"]
    assert summary["total_rows"] > 0, "export claims zero rows for a seeded workspace"
    assert "journal_entry" in summary["tables"]

    r = await client.get(f"{BASE}/exports/{job_id}/download")
    if r.status_code == 503:
        pytest.skip("object storage not configured on this stack")
    assert r.status_code == 200, r.text

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["workspace_id"] == str(TEST_WORKSPACE_ID)

        # Manifest must not overstate: every claimed count matches the file.
        for table, claimed in manifest["contents"]["tables"].items():
            actual = len(json.loads(zf.read(f"json/{table}.json")))
            assert actual == claimed, f"{table}: claimed {claimed}, archive has {actual}"

        # Cross-tenant: workspace B's id must appear nowhere in the archive.
        blob = b"".join(zf.read(n) for n in names)
        assert str(TEST_WORKSPACE_B_ID).encode() not in blob, (
            "workspace B's id leaked into workspace A's export"
        )

        # Every exported row that carries a workspace_id carries OURS.
        for table in manifest["contents"]["tables"]:
            for row in json.loads(zf.read(f"json/{table}.json")):
                if "workspace_id" in row and row["workspace_id"]:
                    assert row["workspace_id"] == str(TEST_WORKSPACE_ID), (
                        f"{table} row belongs to another workspace: {row['workspace_id']}"
                    )


@pytest.mark.asyncio
async def test_export_download_404s_for_an_unknown_job(
    seeded_workspace: StackConfig, client: httpx.AsyncClient
):
    r = await client.get(f"{BASE}/exports/{uuid.uuid4()}")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_retention_round_trips_and_survives_a_setup_progress_patch(
    seeded_workspace: StackConfig, client: httpx.AsyncClient
):
    """Pins the storage decision documented in app/data_rights/retention.py.

    Retention lives as a sibling key inside public.workspace.setup_progress.
    That is only safe because the Setup Assistant's PATCH merges rather than
    replaces. If that ever changes, this test fails — which is the whole point
    of writing it.
    """
    r = await client.put(f"{BASE}/retention", json={"ledger_retention_days": 400})
    assert r.status_code == 200, r.text
    assert r.json()["ledger_retention_days"] == 400
    assert r.json()["is_default"] is False

    # Now poke the column's actual owner.
    r = await client.patch(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/setup-progress",
        json={"steps": {"entity": {"status": "complete"}}},
    )
    if r.status_code not in (200, 403):
        pytest.fail(f"unexpected setup-progress response: {r.status_code} {r.text}")

    r = await client.get(f"{BASE}/retention")
    assert r.status_code == 200, r.text
    assert r.json()["ledger_retention_days"] == 400, (
        "the Setup Assistant PATCH clobbered the retention sibling key — "
        "see the storage-decision note in app/data_rights/retention.py"
    )

    r = await client.put(f"{BASE}/retention", json={"ledger_retention_days": 4000})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# The chain-independence proof (non-destructive, always runs)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hash_chains_are_entity_partitioned_so_erasure_cannot_cross_workspaces(db):
    """Structural proof that workspace-level erasure is safe for other tenants.

    For every journal entry with a non-genesis prev_hash, the predecessor hash
    must belong to an entry for the SAME entity. Since an entity belongs to
    exactly one workspace, no chain link crosses a workspace boundary — so
    deleting all of workspace X's rows can never orphan a link in workspace Y.

    This is the property app/data_rights/erasure.py relies on. If a future
    change made the chain global or cross-entity, this fails and the erasure
    design must be revisited before shipping.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            orphans = await conn.fetch(
                """
                SELECT je.id::text AS id,
                       je.entity_id::text AS entity_id,
                       je.workspace_id::text AS workspace_id
                  FROM journal_entry je
                 WHERE je.prev_hash IS NOT NULL
                   AND je.prev_hash <> decode(repeat('00', 32), 'hex')
                   AND NOT EXISTS (
                         SELECT 1 FROM journal_entry p
                          WHERE p.this_hash = je.prev_hash
                            AND p.entity_id = je.entity_id
                   )
                 LIMIT 20
                """
            )
    assert orphans == [], (
        "found journal entries whose predecessor hash is not an entry of the same "
        f"entity — chains are NOT entity-partitioned: {[dict(o) for o in orphans]}"
    )


@pytest.mark.asyncio
async def test_every_entity_belongs_to_exactly_one_workspace(db):
    """The second half of the argument above."""
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            bad = await conn.fetch(
                """
                SELECT je.entity_id::text AS entity_id,
                       COUNT(DISTINCT je.workspace_id) AS n
                  FROM journal_entry je
                 GROUP BY je.entity_id
                HAVING COUNT(DISTINCT je.workspace_id) > 1
                """
            )
    assert bad == [], f"entities spanning multiple workspaces: {[dict(b) for b in bad]}"


# --------------------------------------------------------------------------- #
# Destructive end-to-end erasure — OPT-IN ONLY
# --------------------------------------------------------------------------- #
def _disposable_workspace_id() -> uuid.UUID | None:
    if os.getenv("ACCTOPS_TEST_ALLOW_DESTRUCTIVE") != "1":
        return None
    raw = os.getenv("ACCTOPS_TEST_DISPOSABLE_WORKSPACE_ID")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


@pytest.mark.asyncio
async def test_purging_a_disposable_workspace_leaves_other_chains_verifiable(
    seeded_workspace: StackConfig, db, client: httpx.AsyncClient
):
    """Actually erase a throwaway workspace, then re-verify the CI workspace.

    Deliberately opt-in. Enable with:
        ACCTOPS_TEST_ALLOW_DESTRUCTIVE=1
        ACCTOPS_TEST_DISPOSABLE_WORKSPACE_ID=<a workspace you can lose>
    """
    disposable = _disposable_workspace_id()
    if disposable is None:
        pytest.skip(
            "destructive erasure test not enabled "
            "(set ACCTOPS_TEST_ALLOW_DESTRUCTIVE=1 and "
            "ACCTOPS_TEST_DISPOSABLE_WORKSPACE_ID)"
        )
    if disposable in (TEST_WORKSPACE_ID, TEST_WORKSPACE_B_ID):
        pytest.fail("refusing to erase a shared fixture workspace")

    # Baseline: the CI workspace's chain verifies before we touch anything.
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
        params={"entity_id": str(TEST_ENTITY_ID)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True, f"chain already broken before the test: {r.json()}"

    from app.data_rights.erasure import purge_workspace_ledger

    step = await purge_workspace_ledger(disposable)
    assert step.status in ("done", "unavailable"), step.detail
    if step.status == "unavailable":
        pytest.skip(f"admin connection not configured: {step.detail}")

    # The load-bearing assertion: the OTHER workspace is untouched and still verifies.
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
        params={"entity_id": str(TEST_ENTITY_ID)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True, (
        f"erasing workspace {disposable} broke workspace {TEST_WORKSPACE_ID}'s "
        f"hash chain: {r.json()}"
    )
    assert r.json()["tampered_count"] == 0

    # And the disposable workspace really is empty.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM journal_entry WHERE workspace_id = $1",
                str(disposable),
            )
    assert remaining == 0, f"{remaining} journal rows survived the purge"


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
