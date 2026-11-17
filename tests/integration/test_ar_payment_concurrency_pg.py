"""record_payment under real concurrency, against a real PostgreSQL.

Task 320c07af. ``app.ar.invoicing.record_payment`` read the invoice without a
row lock, checked the remaining balance, then posted the payment and rewrote
``balance_due``. Two DIFFERENT payments (distinct external_ref, so the
(invoice_id, external_ref) unique index does not help) could both pass the
balance check and both be accepted, overpaying the invoice.

The interleaving is forced, not left to timing:

  1. Session A records a payment and keeps its transaction OPEN.
  2. Session B starts recording a second payment on the same invoice.
  3. The test polls pg_stat_activity until B's backend is waiting on a lock
     (it is blocked behind A on the invoice row), so B has provably started.
  4. A commits; B is released and finishes.

On main (no lock) B blocks only at its final UPDATE, after it has already
passed the balance check, so both payments land (120.00 recorded on a 100.00
invoice, balance_due -20.00). With the invoice row lock B blocks at its first
read and then sees A's committed balance.

Runs as the least-privilege runtime role through the real
``workspace_session`` (RLS, search_path) on a migrated database with at least
one provisioned workspace (``make integration`` or any PostgreSQL brought to
the same state). Needs ``ACCTOPS_TEST_DSN`` (owner, for discovery + the lock
probe) and ``ACCTOPS_TEST_APP_DSN`` (runtime role). SKIPS when either is
unset or unreachable. No network beyond that database.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any, AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ar.invoicing import (
    InvoiceError,
    create_invoice_from_draft,
    record_payment,
    void_invoice,
)
from app.workspace_session import workspace_session
from tests.conftest import StackConfig

pytestmark = pytest.mark.integration

_SCHEMA = "books"
_APP_NAME = "ao-itest-payment-race"
_ACTOR = "itest-payment-race"
# How long to wait for session B to reach its lock wait before giving up.
_LOCK_WAIT_TIMEOUT_S = 20.0


def _sa_url(dsn: str) -> str:
    scheme, rest = dsn.split("://", 1)
    if "+" in scheme:
        scheme = scheme.split("+", 1)[0]
    return f"{scheme}+asyncpg://{rest}"


@pytest_asyncio.fixture
async def owner(stack: StackConfig) -> AsyncIterator["asyncpg.Pool"]:
    if asyncpg is None:  # pragma: no cover
        pytest.skip("asyncpg not installed")
    if not stack.app_dsn:
        pytest.skip("ACCTOPS_TEST_APP_DSN unset; runtime role not configured")
    try:
        pool = await asyncpg.create_pool(stack.dsn, min_size=1, max_size=2)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable at ACCTOPS_TEST_DSN: {exc!r}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def sessionmaker(stack: StackConfig, owner) -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(
        _sa_url(stack.app_dsn or ""),
        pool_size=4,
        max_overflow=0,
        connect_args={"server_settings": {"application_name": _APP_NAME}},
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def owner_sessionmaker(stack: StackConfig, owner) -> AsyncIterator[async_sessionmaker]:
    """OWNER-role sessions, used only to seed the invoice under test.

    Seeding goes through the real create_invoice_from_draft, but as the owner:
    as the runtime role that function currently fails on
    public.invoice_pay_token (INSERT ... RETURNING needs the SELECT policy,
    which keys on app.pay_token). That is outside this test's subject, which
    is the payment path; the payments below all run as the runtime role.
    """
    engine = create_async_engine(_sa_url(stack.dsn), pool_size=1, max_overflow=0)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def workspace(owner: "asyncpg.Pool") -> dict[str, str]:
    row = await owner.fetchrow(
        f"""
        SELECT w.id::text AS workspace_id, e.id::text AS entity_id
        FROM public.workspace w
        JOIN LATERAL (
            SELECT id FROM {_SCHEMA}.entity e
            WHERE e.workspace_id = w.id ORDER BY e.created_at LIMIT 1
        ) e ON true
        WHERE w.status = 'active'
        ORDER BY w.created_at
        LIMIT 1
        """
    )
    if row is None:
        pytest.skip("need one provisioned workspace with an entity")
    return dict(row)


async def _new_invoice(sm: async_sessionmaker, ws: dict[str, str], total: str) -> str:
    """Create and post a one-line invoice for ``total`` and return its id."""
    async with workspace_session(sm, ws["workspace_id"]) as session:
        view = await create_invoice_from_draft(
            session,
            workspace_id=ws["workspace_id"],
            entity_id=ws["entity_id"],
            actor_uc_uid=_ACTOR,
            external_source="manual",
            external_ref=f"race-{uuid.uuid4()}",
            customer={"name": "Fictional Race Customer", "email": "race@example.com"},
            line_items=[
                {"description": "Fictional services", "quantity": 1, "unit_price": total}
            ],
        )
    return str(view["id"])


async def _pay(
    sm: async_sessionmaker, ws: dict[str, str], invoice_id: str, amount: str, ref: str
) -> dict[str, Any]:
    async with workspace_session(sm, ws["workspace_id"]) as session:
        return await record_payment(
            session,
            workspace_id=ws["workspace_id"],
            invoice_id=invoice_id,
            actor_uc_uid=_ACTOR,
            amount=amount,
            external_ref=ref,
        )


async def _wait_for_lock_waiter(owner: "asyncpg.Pool", task: asyncio.Task) -> None:
    """Block until one of our backends is waiting on a heavyweight lock.

    Fails the test if ``task`` finishes first: that would mean session B ran to
    completion without ever blocking behind session A, so the interleaving this
    file exists to force did not happen.
    """
    deadline = asyncio.get_running_loop().time() + _LOCK_WAIT_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        if task.done():
            raise AssertionError(
                f"session B finished without blocking behind session A: {task.exception()!r}"
                if task.exception() else "session B finished without blocking behind session A"
            )
        waiting = await owner.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = $1 AND wait_event_type = 'Lock'",
            _APP_NAME,
        )
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("session B never reached a lock wait")


async def _invoice_state(owner: "asyncpg.Pool", invoice_id: str) -> dict[str, Any]:
    row = await owner.fetchrow(
        f"""
        SELECT i.total, i.balance_due, i.status,
               (SELECT count(*) FROM {_SCHEMA}.invoice_payment p WHERE p.invoice_id = i.id)
                   AS n_payments,
               (SELECT COALESCE(sum(amount), 0) FROM {_SCHEMA}.invoice_payment p
                 WHERE p.invoice_id = i.id) AS paid
        FROM {_SCHEMA}.invoice i WHERE i.id = $1
        """,
        uuid.UUID(invoice_id),
    )
    return dict(row)


async def _race(
    sm: async_sessionmaker,
    owner: "asyncpg.Pool",
    ws: dict[str, str],
    invoice_id: str,
    *,
    amount_a: str,
    ref_a: str,
    second: Callable[[], Awaitable[dict[str, Any]]],
) -> BaseException | dict[str, Any]:
    """Run payment A (held open) and ``second`` (forced to start while A is open).

    Returns the second operation's result, or the exception it raised.
    """
    async with workspace_session(sm, ws["workspace_id"]) as session_a:
        await record_payment(
            session_a,
            workspace_id=ws["workspace_id"],
            invoice_id=invoice_id,
            actor_uc_uid=_ACTOR,
            amount=amount_a,
            external_ref=ref_a,
        )
        task_b = asyncio.create_task(second())
        await _wait_for_lock_waiter(owner, task_b)
        # Leaving this block commits A and releases B.
    try:
        return await asyncio.wait_for(task_b, timeout=_LOCK_WAIT_TIMEOUT_S)
    except BaseException as exc:  # noqa: BLE001
        return exc


async def test_concurrent_distinct_payments_exactly_one_accepted(
    sessionmaker, owner_sessionmaker, owner, workspace
):
    """100.00 invoice, two distinct 60.00 payments in overlapping transactions.

    Exactly one is accepted; the other gets the existing over-balance error and
    leaves nothing behind. Failed on main: both were accepted (120.00 recorded,
    balance_due -20.00).
    """
    invoice_id = await _new_invoice(owner_sessionmaker, workspace, "100.00")
    tag = uuid.uuid4().hex[:8]
    result_b = await _race(
        sessionmaker, owner, workspace, invoice_id,
        amount_a="60.00", ref_a=f"race-{tag}-a",
        second=lambda: _pay(sessionmaker, workspace, invoice_id, "60.00", f"race-{tag}-b"),
    )
    assert isinstance(result_b, InvoiceError), result_b
    assert "exceeds the remaining balance 40.00" in str(result_b)
    state = await _invoice_state(owner, invoice_id)
    assert state["n_payments"] == 1
    assert Decimal(state["paid"]) == Decimal("60.00")
    assert Decimal(state["balance_due"]) == Decimal("40.00")
    assert state["status"] == "partial"


async def test_concurrent_same_event_duplicate_is_idempotent(
    sessionmaker, owner_sessionmaker, owner, workspace
):
    """The same external_ref delivered twice at once (a Stripe webhook retry).

    The second delivery waits for the first, then finds its payment and returns
    the invoice view: one payment row, no error. On main the second delivery
    raised a unique-violation from invoice_payment instead.
    """
    invoice_id = await _new_invoice(owner_sessionmaker, workspace, "100.00")
    ref = f"evt-{uuid.uuid4().hex[:12]}"
    result_b = await _race(
        sessionmaker, owner, workspace, invoice_id,
        amount_a="40.00", ref_a=ref,
        second=lambda: _pay(sessionmaker, workspace, invoice_id, "40.00", ref),
    )
    assert not isinstance(result_b, BaseException), result_b
    assert Decimal(str(result_b["balance_due"])) == Decimal("60.00")
    state = await _invoice_state(owner, invoice_id)
    assert state["n_payments"] == 1
    assert Decimal(state["balance_due"]) == Decimal("60.00")
    assert state["status"] == "partial"


async def test_void_waits_for_concurrent_payment_and_refuses(
    sessionmaker, owner_sessionmaker, owner, workspace
):
    """void_invoice takes the same lock, so its no-payments guard sees a
    payment that commits while the void is in flight. On main the void read
    paid=0, then set status='void', balance_due=0 over a recorded payment.
    """
    invoice_id = await _new_invoice(owner_sessionmaker, workspace, "100.00")

    async def _void() -> dict[str, Any]:
        async with workspace_session(sessionmaker, workspace["workspace_id"]) as session:
            return await void_invoice(
                session,
                workspace_id=workspace["workspace_id"],
                invoice_id=invoice_id,
                actor_uc_uid=_ACTOR,
                reason="fictional race void",
            )

    result_b = await _race(
        sessionmaker, owner, workspace, invoice_id,
        amount_a="25.00", ref_a=f"pre-void-{uuid.uuid4().hex[:8]}",
        second=_void,
    )
    assert isinstance(result_b, InvoiceError), result_b
    assert "payments recorded" in str(result_b)
    state = await _invoice_state(owner, invoice_id)
    assert state["status"] == "partial"
    assert Decimal(state["balance_due"]) == Decimal("75.00")
    assert state["n_payments"] == 1


async def test_sequential_payments_unchanged(
    sessionmaker, owner_sessionmaker, owner, workspace
):
    """No concurrency: partial, then a payment that settles it, then a refusal."""
    invoice_id = await _new_invoice(owner_sessionmaker, workspace, "100.00")
    tag = uuid.uuid4().hex[:8]
    first = await _pay(sessionmaker, workspace, invoice_id, "30.00", f"seq-{tag}-1")
    assert Decimal(str(first["balance_due"])) == Decimal("70.00")
    assert first["status"] == "partial"
    second = await _pay(sessionmaker, workspace, invoice_id, "70.00", f"seq-{tag}-2")
    assert Decimal(str(second["balance_due"])) == Decimal("0.00")
    assert second["status"] == "paid"
    with pytest.raises(InvoiceError, match="already fully paid"):
        await _pay(sessionmaker, workspace, invoice_id, "1.00", f"seq-{tag}-3")
    state = await _invoice_state(owner, invoice_id)
    assert state["n_payments"] == 2
    assert Decimal(state["paid"]) == Decimal("100.00")
