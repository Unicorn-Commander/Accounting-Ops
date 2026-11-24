"""Invoice pay tokens AS the runtime role (accounting_ops_app) under FORCE RLS.

Covers create (RETURNING through the workspace SELECT policy) and AO-TOK-2:
only a SHA-256 of the token is stored, a link expires, and it is revoked on
void / paid / issue, after which /pay shows no customer data. Needs
ACCTOPS_TEST_DSN (owner) and
ACCTOPS_TEST_APP_DSN (runtime role) on a Postgres with both migration chains at
head and a provisioned workspace with an entity; SKIPS otherwise.
"""
from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import invoices as invoices_api
from app.ar.invoicing import (
    InvoiceError,
    create_invoice_from_draft,
    hash_pay_token,
    issue_invoice_draft,
    record_payment,
    regenerate_pay_link,
    void_invoice,
)
from app.workspace_session import workspace_session
from tests.conftest import StackConfig

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

pytestmark = pytest.mark.integration


def _sqla_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest_asyncio.fixture
async def owner(stack: StackConfig):
    if asyncpg is None:  # pragma: no cover
        pytest.skip("asyncpg not installed")
    try:
        pool = await asyncpg.create_pool(stack.dsn, min_size=1, max_size=2)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable at ACCTOPS_TEST_DSN: {exc!r}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def runtime_sessionmaker(stack: StackConfig):
    if not stack.app_dsn:
        pytest.skip("ACCTOPS_TEST_APP_DSN unset")
    engine = create_async_engine(_sqla_url(stack.app_dsn))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def workspace(owner):
    rows = await owner.fetch(
        """
        SELECT w.id::text AS workspace_id, e.id::text AS entity_id
        FROM public.workspace w
        JOIN LATERAL (SELECT id FROM books.entity e WHERE e.workspace_id = w.id
                      ORDER BY e.created_at LIMIT 1) e ON true
        WHERE w.status = 'active' ORDER BY w.created_at LIMIT 2
        """
    )
    if not rows:
        pytest.skip("no provisioned workspace with an entity")
    return [dict(r) for r in rows]


async def _create(sm, ws: dict, ref: str, **kw) -> dict:
    async with workspace_session(sm, ws["workspace_id"]) as session:
        return await create_invoice_from_draft(
            session,
            workspace_id=ws["workspace_id"],
            entity_id=ws["entity_id"],
            actor_uc_uid="rls-test",
            external_source="rls-test",
            external_ref=ref,
            customer={"name": "Example Customer LLC", "email": "ap@example.com"},
            line_items=[{"description": "Consulting", "quantity": 2, "unit_price": "50.00"}],
            **kw,
        )


async def test_create_stores_only_a_hash_with_expiry(runtime_sessionmaker, owner, workspace) -> None:
    ws = workspace[0]
    view = await _create(runtime_sessionmaker, ws, f"tok-{uuid.uuid4()}")
    token = view["public_token"]
    assert token and len(token) >= 43
    row = await owner.fetchrow(
        "SELECT t.token_hash, t.workspace_id::text AS wid, t.revoked_at, "
        "t.expires_at - now() AS ttl, i.public_token "
        "FROM public.invoice_pay_token t JOIN books.invoice i ON i.id = t.invoice_id "
        "WHERE t.invoice_id = $1::uuid",
        view["id"],
    )
    assert row is not None, "no pay-token row written"
    assert row["wid"] == ws["workspace_id"]
    assert row["token_hash"] == hash_pay_token(token)
    assert row["public_token"] is None, "the raw token must not be stored on the invoice"
    assert row["revoked_at"] is None
    assert 59 <= row["ttl"].days <= 60
    cols = await owner.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'invoice_pay_token'"
    )
    assert "token" not in {c["column_name"] for c in cols}


async def test_runtime_role_cannot_enumerate_pay_tokens(runtime_sessionmaker, workspace) -> None:
    """No workspace and no token-hash GUC: the role sees nothing."""
    await _create(runtime_sessionmaker, workspace[0], f"enum-{uuid.uuid4()}")
    async with runtime_sessionmaker() as session:
        async with session.begin():
            n = (await session.execute(text("SELECT count(*) FROM public.invoice_pay_token"))).scalar_one()
    assert n == 0


async def test_workspace_sees_only_its_own_rows(runtime_sessionmaker, workspace) -> None:
    if len(workspace) < 2:
        pytest.skip("needs two provisioned workspaces")
    a, b = workspace[0], workspace[1]
    await _create(runtime_sessionmaker, a, f"iso-{uuid.uuid4()}")
    await _create(runtime_sessionmaker, b, f"iso-{uuid.uuid4()}")
    for mine, other in ((a, b), (b, a)):
        async with workspace_session(runtime_sessionmaker, mine["workspace_id"]) as session:
            rows = (
                await session.execute(text("SELECT workspace_id::text FROM public.invoice_pay_token"))
            ).scalars().all()
        assert rows and other["workspace_id"] not in rows


async def test_runtime_role_may_only_touch_revoked_at(runtime_sessionmaker, workspace) -> None:
    ws = workspace[0]
    await _create(runtime_sessionmaker, ws, f"grant-{uuid.uuid4()}")
    for stmt in (
        "UPDATE public.invoice_pay_token SET expires_at = now() + interval '9 years'",
        "DELETE FROM public.invoice_pay_token",
    ):
        with pytest.raises(Exception, match="permission denied"):
            async with workspace_session(runtime_sessionmaker, ws["workspace_id"]) as session:
                await session.execute(text(stmt))


async def test_exhausted_attempts_raise(runtime_sessionmaker, workspace, monkeypatch) -> None:
    ws = workspace[0]
    first = await _create(runtime_sessionmaker, ws, f"coll-{uuid.uuid4()}")
    monkeypatch.setattr(
        "app.ar.invoicing.secrets.token_urlsafe", lambda *_a, **_k: first["public_token"]
    )
    with pytest.raises(InvoiceError):
        await _create(runtime_sessionmaker, ws, f"coll-b-{uuid.uuid4()}")


@pytest_asyncio.fixture
async def app_sessions(runtime_sessionmaker, monkeypatch):
    """Point the /pay lookup at the runtime role."""
    monkeypatch.setattr(invoices_api, "SessionLocal", runtime_sessionmaker)
    return runtime_sessionmaker


async def test_public_lookup_live_then_inactive_after_void(app_sessions, owner, workspace) -> None:
    ws = workspace[0]
    view = await _create(app_sessions, ws, f"void-{uuid.uuid4()}")
    token = view["public_token"]
    live = await invoices_api._invoice_for_token(token)
    assert isinstance(live, dict) and live["id"] == view["id"]
    async with workspace_session(app_sessions, ws["workspace_id"]) as session:
        await void_invoice(
            session, workspace_id=ws["workspace_id"], invoice_id=view["id"],
            actor_uc_uid="rls-test", reason="test",
        )
    assert await invoices_api._invoice_for_token(token) is None
    revoked = await owner.fetchval(
        "SELECT revoked_at FROM public.invoice_pay_token WHERE invoice_id = $1::uuid", view["id"]
    )
    assert revoked is not None


async def test_full_payment_revokes_the_link(app_sessions, workspace) -> None:
    ws = workspace[0]
    view = await _create(app_sessions, ws, f"paid-{uuid.uuid4()}")
    async with workspace_session(app_sessions, ws["workspace_id"]) as session:
        paid = await record_payment(
            session, workspace_id=ws["workspace_id"], invoice_id=view["id"],
            actor_uc_uid="rls-test", amount=view["total"],
        )
    assert paid["status"] == "paid"
    assert await invoices_api._invoice_for_token(view["public_token"]) is None


async def test_public_lookup_inactive_when_expired(app_sessions, owner, workspace) -> None:
    ws = workspace[0]
    view = await _create(app_sessions, ws, f"exp-{uuid.uuid4()}")
    await owner.execute(
        "UPDATE public.invoice_pay_token SET expires_at = now() - interval '1 minute' "
        "WHERE invoice_id = $1::uuid",
        view["id"],
    )
    assert await invoices_api._invoice_for_token(view["public_token"]) is None


async def test_issue_rotates_the_draft_link(app_sessions, workspace) -> None:
    ws = workspace[0]
    draft = await _create(app_sessions, ws, f"draft-{uuid.uuid4()}", post_journal=False)
    async with workspace_session(app_sessions, ws["workspace_id"]) as session:
        issued = await issue_invoice_draft(
            session, workspace_id=ws["workspace_id"], invoice_id=draft["id"],
            actor_uc_uid="rls-test",
        )
    assert issued["public_token"] and issued["public_token"] != draft["public_token"]
    assert await invoices_api._invoice_for_token(draft["public_token"]) is None
    assert isinstance(await invoices_api._invoice_for_token(issued["public_token"]), dict)


async def test_unknown_token_is_not_found(app_sessions) -> None:
    assert await invoices_api._invoice_for_token("x" * 43) is None


async def _public_get(path: str) -> tuple[int, bytes]:
    app = FastAPI()
    app.include_router(invoices_api.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://pay.example.com") as client:
        resp = await client.get(path)
    return resp.status_code, resp.content


async def test_expired_revoked_paid_answer_exactly_like_unknown(app_sessions, owner, workspace) -> None:
    """AO110-R2-P2-3: same status and body for unknown, expired, revoked, paid."""
    ws = workspace[0]
    expired = await _create(app_sessions, ws, f"same-exp-{uuid.uuid4()}")
    await owner.execute(
        "UPDATE public.invoice_pay_token SET expires_at = now() - interval '1 minute' "
        "WHERE invoice_id = $1::uuid",
        expired["id"],
    )
    revoked = await _create(app_sessions, ws, f"same-rev-{uuid.uuid4()}")
    async with workspace_session(app_sessions, ws["workspace_id"]) as session:
        await void_invoice(
            session, workspace_id=ws["workspace_id"], invoice_id=revoked["id"],
            actor_uc_uid="rls-test", reason="test",
        )
    paid = await _create(app_sessions, ws, f"same-paid-{uuid.uuid4()}")
    async with workspace_session(app_sessions, ws["workspace_id"]) as session:
        await record_payment(
            session, workspace_id=ws["workspace_id"], invoice_id=paid["id"],
            actor_uc_uid="rls-test", amount=paid["total"],
        )
    live = await _create(app_sessions, ws, f"same-live-{uuid.uuid4()}")
    status, body = await _public_get(f"/pay/{live['public_token']}")
    assert status == 200 and b"Example Customer" in body

    tokens = {
        "unknown": "u" * 43,
        "expired": expired["public_token"],
        "revoked": revoked["public_token"],
        "paid": paid["public_token"],
    }
    for suffix in ("", "/pdf"):
        answers = {k: await _public_get(f"/pay/{t}{suffix}") for k, t in tokens.items()}
        assert answers["unknown"][0] == 404
        for kind, answer in answers.items():
            assert answer == answers["unknown"], f"{kind}{suffix} differs from unknown"
            assert b"Example Customer" not in answer[1] and b"example.com" not in answer[1]


async def test_reads_hide_the_link_and_regenerate_rotates_it(app_sessions, workspace) -> None:
    """AO110-R2-P2-2: a read never shows public_url; the explicit regenerate
    action revokes the old link and returns a new one exactly once."""
    from app.ar.invoicing import get_invoice

    ws = workspace[0]
    view = await _create(app_sessions, ws, f"regen-{uuid.uuid4()}")
    old = view["public_token"]
    async with workspace_session(app_sessions, ws["workspace_id"]) as session:
        read = await get_invoice(session, workspace_id=ws["workspace_id"], invoice_id=view["id"])
    assert "public_token" not in read
    assert "public_url" not in invoices_api._attach_pdf_url(read, uuid.UUID(ws["workspace_id"]))

    async with workspace_session(app_sessions, ws["workspace_id"]) as session:
        fresh = await regenerate_pay_link(
            session, workspace_id=ws["workspace_id"], invoice_id=view["id"]
        )
    new = fresh["public_token"]
    assert new and new != old
    shaped = invoices_api._attach_pdf_url(fresh, uuid.UUID(ws["workspace_id"]))
    assert shaped["public_url"].endswith(f"/pay/{new}")
    assert await invoices_api._invoice_for_token(old) is None
    assert isinstance(await invoices_api._invoice_for_token(new), dict)

    async with workspace_session(app_sessions, ws["workspace_id"]) as session:
        await record_payment(
            session, workspace_id=ws["workspace_id"], invoice_id=view["id"],
            actor_uc_uid="rls-test", amount=view["total"],
        )
    with pytest.raises(InvoiceError, match="paid"):
        async with workspace_session(app_sessions, ws["workspace_id"]) as session:
            await regenerate_pay_link(
                session, workspace_id=ws["workspace_id"], invoice_id=view["id"]
            )


def _as_role(ws: dict, role: str):
    from app.tenancy import WorkspaceContext

    return WorkspaceContext(
        workspace_id=uuid.UUID(ws["workspace_id"]),
        workspace_slug="regen-test",
        role=role,
        user_uc_uid=f"regen-{role}",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )


async def _regenerate_as(ws: dict, invoice_id: str, role: str) -> httpx.Response:
    from app.tenancy import resolve_workspace

    app = FastAPI()
    app.include_router(invoices_api.router)
    app.dependency_overrides[resolve_workspace] = lambda: _as_role(ws, role)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ao.example.com") as client:
        return await client.post(
            f"/api/workspaces/{ws['workspace_id']}/invoices/{invoice_id}/pay-link"
        )


async def test_regenerate_route_is_owner_admin_only(app_sessions, owner, workspace) -> None:
    """AO110-R3-P2-A: accountant and viewer get 403 and the link is untouched;
    owner gets a new link once and the old token stops working."""
    ws = workspace[0]
    view = await _create(app_sessions, ws, f"regen-route-{uuid.uuid4()}")
    old = view["public_token"]
    for role in ("viewer", "accountant"):
        resp = await _regenerate_as(ws, view["id"], role)
        assert resp.status_code == 403, (role, resp.status_code, resp.text)
        assert isinstance(await invoices_api._invoice_for_token(old), dict), role

    resp = await _regenerate_as(ws, view["id"], "owner")
    assert resp.status_code == 200, resp.text
    url = resp.json()["public_url"]
    new = url.rsplit("/pay/", 1)[1]
    assert new and new != old
    assert await invoices_api._invoice_for_token(old) is None
    assert isinstance(await invoices_api._invoice_for_token(new), dict)
    status, _ = await _public_get(f"/pay/{old}")
    assert status == 404

    # AO110-R3-P3-C: one audit row per successful regenerate, none for a 403.
    rows = await owner.fetch(
        "SELECT workspace_id::text AS wid, request_payload::text AS req, "
        "response_payload::text AS resp, outcome FROM books.decision_audit "
        "WHERE tool_name = 'regenerate_pay_link' "
        "AND request_payload->>'invoice_id' = $1",
        view["id"],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["wid"] == ws["workspace_id"] and row["outcome"] == "regenerated"
    assert "regen-owner" in row["req"]
    for token in (old, new):
        assert token not in row["req"] and token not in row["resp"]


async def test_link_sent_before_0021_keeps_working_until_its_expiry(app_sessions, owner, workspace) -> None:
    """AO110-R2-P2-2: 0021 hashed legacy rows in place with the SQL below and
    set expires_at = created_at + 60 days, so a raw token a customer already
    holds still resolves, and stops at the cut-off."""
    ws = workspace[0]
    view = await _create(app_sessions, ws, f"legacy-{uuid.uuid4()}")
    raw = "legacy-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    migrated = await owner.fetchval(
        "SELECT encode(sha256(convert_to($1::text, 'UTF8')), 'hex')", raw
    )
    assert migrated == hash_pay_token(raw)
    await owner.execute(
        "INSERT INTO public.invoice_pay_token (token_hash, workspace_id, invoice_id, created_at, expires_at) "
        "VALUES ($1, $2::uuid, $3::uuid, now() - interval '59 days', now() + interval '1 day')",
        migrated, ws["workspace_id"], view["id"],
    )
    assert isinstance(await invoices_api._invoice_for_token(raw), dict)
    await owner.execute(
        "UPDATE public.invoice_pay_token SET expires_at = created_at + interval '58 days' "
        "WHERE token_hash = $1",
        migrated,
    )
    assert await invoices_api._invoice_for_token(raw) is None
