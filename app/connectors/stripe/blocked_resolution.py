"""Manual "resolved elsewhere" unblock for a BLOCKED Stripe sync item.

Kit's rule (KIT-41-39): a blocked Stripe item leaves the head of the sync
chain in exactly two ways.

  1. Fix the config and let the worker retry it (scripts/stripe_sync.py).
  2. A human OWNER/ADMIN says it was resolved elsewhere. That claim needs
     either a manual journal entry id that exists in THIS workspace's books,
     or an explicit "not books-relevant" reason.

Path 2 lives here. It never posts to the books, never touches materiality and
never runs on its own. In ONE transaction it:

  * appends a ``decision_audit`` row (who, when, why, journal entry or reason);
  * marks the "(blocked)" approval row decided (status ``rejected``: the held
    posting is refused, nothing was posted) and stores the resolution on it;
  * clears ``blocked_item`` and that id's failure count in
    ``connector_state.state``, the same merge ``_record_sync_outcome`` does with
    ``clear_blocked_item=True``, and parks the continuation cursor on the
    resolved item so the next pass continues after it;
  * remembers the id under ``resolved_blocked_items`` so a later re-read of the
    24h window overlap skips it instead of posting it a second time.

Concurrency: ONE lock serializes every writer of the Stripe
``connector_state`` row: ``SELECT ... FOR UPDATE`` on that row (see
``lock_stripe_state``). The sync worker takes it at the start of its mapping
transaction and re-reads the resolved ids under it; the worker's outcome
write takes it and merges only its own keys; resolve and reopen take it and
decide only from state read after it. The side writers (payout reconciliation
in reconcile.py, the webhook delivery log in app/api/stripe_oauth.py) take it
too and merge only their own keys through ``merge_side_state_keys``, which
refuses every block-protocol key. tests/unit/test_stripe_state_writers.py
fails when a new connector_state writer appears outside this set. All of them write with a jsonb merge
(MERGE_STATE_SQL), never a whole-state replacement, and none of the worker's
writes can drop ``resolved_blocked_items`` or put a resolved id back as the
blocked item.

Every resolve, reopen and worker block transition bumps
``connector_state.state.block_revision`` under that lock. A sync pass carries
the revision it loaded, and an outcome write from an older revision may not
touch the block, cursor, window, failure counts or last_sync_at. That is what
stops a stale pass from erasing a reopen of the SAME id it had loaded.

The approval-posting path (app/api/approvals.py decide_approval, Stripe rows)
takes the same state lock BEFORE it locks the approval row. Resolve does the
same. One lock order everywhere: Stripe state row, then approval row.

A resolution can be undone only by ``reopen_resolved_item``: an owner/admin
human, a required reason, an audit row. It re-arms the item as the blocked
item so the worker maps it normally, and marks every existing rejection of
that item ``superseded_by_reopen`` so the worker's rejected-approval hold no
longer applies to it. Nothing expires on its own.

The approval row, the audit row and connector_state are all workspace tables,
so the customer export (app/data_rights/export.py, dynamic table discovery)
carries the resolution without any change there.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tenancy import WorkspaceContext
from app.tools._audit import write_decision_audit

RESOLVED_STATE_KEY = "resolved_blocked_items"
AUDIT_TOOL_NAME = "stripe_blocked_item_resolve"
AUDIT_OUTCOME = "resolved_elsewhere"
REOPEN_AUDIT_TOOL_NAME = "stripe_blocked_item_reopen"
REOPEN_OUTCOME = "resolution_reopened"
REOPENED_STATE_KEY = "reopened_blocked_items"
# Monotonic counter bumped under the shared lock by every resolve, every reopen
# and every worker block transition (a new blocked item, or the block cleared).
# A sync pass carries the value it loaded; an outcome write from an older
# revision is stale and may not touch the chain position (see
# scripts/stripe_sync.py _record_sync_outcome). Id equality alone cannot tell
# "the same block the pass loaded" from "that id resolved and reopened since".
BLOCK_REVISION_KEY = "block_revision"
# Set on a rejected approval row by an audited reopen. The worker's
# rejected-approval checks (scripts/stripe_sync.py _stripe_item_was_rejected)
# skip superseded rows; an ordinary rejection never carries this key.
SUPERSEDED_BY_REOPEN_KEY = "superseded_by_reopen"
# Durable per-item record of a human REJECTION of an ordinary Stripe review
# row: {bt_id: {approval_id, rejected_by_uc_uid, rejected_at, ...}}. Written
# under the shared lock by decide_approval (record_stripe_decision) and by the
# worker when it honours a rejected row it finds. The worker checks it on every
# pass before the mapper, so an overlap re-read never posts a rejected item or
# raises a new review row for it. Never dropped by a merge; an entry leaves
# only when a human APPROVES a new approval row for that bt_id.
# It does not touch a persisted block: a blocked retry keeps its own hold.
REJECTED_STATE_KEY = "rejected_stripe_items"
MIN_REASON_CHARS = 10
MAX_REASON_CHARS = 2000
# How long a human call waits for a running sync pass to release the lock.
LOCK_TIMEOUT_MS = 5000


class BlockedResolutionError(Exception):
    """A refusal with the HTTP status the API layer should return."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def lock_stripe_state_row(
    session: AsyncSession,
    entity_id: UUID,
    instance: str = "default",
    *,
    lock_timeout_ms: int | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Take THE Stripe sync lock on one exact row; return (row id, fresh state).

    Every writer of ``connector_state.state`` for the Stripe connector takes
    this row lock first: the sync worker's mapping transaction, the worker's
    outcome write, the payout reconciliation, the webhook delivery log, the
    manual resolve and the manual reopen. Whatever a caller decides from the
    returned state stays true until its transaction ends. Returns None when
    the row does not exist yet (nothing can be blocked or resolved then).
    tests/test_stripe_state_writers.py fails if a new writer skips it.

    ``lock_timeout_ms`` bounds the wait (a request path must not hang behind a
    long sync pass); the timeout then raises from the driver as usual.
    """
    if lock_timeout_ms is not None:
        await session.execute(
            text(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}ms'")
        )
    row = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, state FROM connector_state
                WHERE entity_id = :eid AND connector_name = 'stripe_api'
                  AND connector_instance_name = :instance
                FOR UPDATE
                """
            ),
            {"eid": str(entity_id), "instance": instance},
        )
    ).first()
    if lock_timeout_ms is not None:
        await session.execute(text("SET LOCAL lock_timeout = 0"))
    if row is None:
        return None
    return str(row.id), _state_dict(getattr(row, "state", None))


async def lock_stripe_state(
    session: AsyncSession, entity_id: UUID, instance: str = "default"
) -> dict[str, Any] | None:
    """lock_stripe_state_row, returning only the fresh state."""
    locked = await lock_stripe_state_row(session, entity_id, instance)
    return None if locked is None else locked[1]


# Keys the block protocol owns. Only the worker's outcome write
# (scripts/stripe_sync.py _record_sync_outcome, revision-checked), resolve
# and reopen may write them. Side writers (reconciliation, webhook log) merge
# their own keys through merge_side_state_keys, which refuses these.
BLOCK_PROTOCOL_KEYS = frozenset(
    {
        "blocked_item",
        "awaiting_review_item",
        "_stripe_continuation_cursor",
        "_stripe_failure_counts",
        "window_gte",
        "window_lt",
        "last_outcome",
        RESOLVED_STATE_KEY,
        REOPENED_STATE_KEY,
        REJECTED_STATE_KEY,
        BLOCK_REVISION_KEY,
    }
)


async def merge_side_state_keys(
    session: AsyncSession, row_id: str, patch: dict[str, Any]
) -> None:
    """jsonb-merge ``patch`` into a row the caller locked with lock_stripe_state_row.

    Writes ONLY the keys in ``patch``; every other key (block, resolutions,
    revision, cursor) is whatever the row holds at write time.
    """
    forbidden = set(patch) & BLOCK_PROTOCOL_KEYS
    if forbidden:
        raise ValueError(f"side writers may not write {sorted(forbidden)}")
    await session.execute(text(MERGE_STATE_SQL), merge_params(row_id, patch, set()))


def resolved_ids(state: dict[str, Any] | None) -> frozenset[str]:
    """Stripe ids under ``resolved_blocked_items`` in a state dict."""
    resolved = (state or {}).get(RESOLVED_STATE_KEY)
    if isinstance(resolved, str):
        try:
            resolved = json.loads(resolved)
        except ValueError:
            resolved = None
    if not isinstance(resolved, dict):
        return frozenset()
    return frozenset(key for key in resolved if isinstance(key, str) and key)


def rejected_items(state: dict[str, Any] | None) -> dict[str, Any]:
    """The ``rejected_stripe_items`` map of a state dict (a copy)."""
    rejected = (state or {}).get(REJECTED_STATE_KEY)
    if isinstance(rejected, str):
        try:
            rejected = json.loads(rejected)
        except ValueError:
            rejected = None
    if not isinstance(rejected, dict):
        return {}
    return {key: value for key, value in rejected.items() if isinstance(key, str) and key}


def is_blocked_review_row(payload: dict[str, Any] | None) -> bool:
    """True for a "(blocked)" alert row (mapper.enqueue_blocked_balance_transaction)."""
    payload = payload or {}
    return bool(payload.get("blocked")) or payload.get("debit_account") == "(blocked)"


async def record_stripe_decision(
    session: AsyncSession,
    *,
    entity_id: UUID,
    bt_id: str,
    approval_id: str,
    decision: str,
    decided_by: str | None,
    payload: dict[str, Any] | None,
    instance: str = "default",
) -> dict[str, Any] | None:
    """Keep ``rejected_stripe_items`` in step with a Stripe approval decision.

    Called by decide_approval on the same transaction, AFTER it took the
    shared lock (lock_stripe_state_for_approval). A REJECT of an ordinary
    review row adds ``bt_id``; an APPROVE removes it. A "(blocked)" row's
    rejection is not recorded here: a persisted block keeps its own hold in
    the worker, and a reopen may supersede it. Returns the entry written for
    a rejection, else None. Fails closed with 409 when a rejection cannot be
    recorded because the Stripe state row is missing.
    """
    if not bt_id:
        return None
    if decision == "reject" and is_blocked_review_row(payload):
        return None
    locked = await lock_stripe_state_row(session, entity_id, instance)
    if locked is None:
        if decision == "reject":
            raise BlockedResolutionError(
                409,
                "Stripe sync state is missing for this entity; the rejection "
                "cannot be recorded durably",
            )
        return None
    row_id, state = locked
    rejected = rejected_items(state)
    entry: dict[str, Any] | None = None
    if decision == "reject":
        entry = {
            "approval_id": approval_id,
            "rejected_by_uc_uid": decided_by,
            "rejected_at": datetime.now(tz=timezone.utc).isoformat(),
            "source": "approval_decision",
        }
        rejected[bt_id] = entry
    elif decision == "approve" and bt_id in rejected:
        rejected.pop(bt_id)
    else:
        return None
    await session.execute(
        text(MERGE_STATE_SQL),
        merge_params(row_id, {REJECTED_STATE_KEY: rejected}, set()),
    )
    return entry


async def remember_rejection(
    session: AsyncSession,
    locked: "LockedBlockState",
    *,
    bt_id: str,
    approval_id: str | None,
) -> None:
    """Worker-side backfill of ``rejected_stripe_items`` under its held lock.

    For a rejection decided before decide_approval recorded it durably. Reads
    the map again under the same lock so a concurrent merge is never lost.
    """
    if locked.row_id is None or not bt_id:
        return
    state = await lock_stripe_state(session, locked.entity_id, locked.instance)
    rejected = rejected_items(state)
    if bt_id in rejected:
        return
    rejected[bt_id] = {
        "approval_id": approval_id,
        "rejected_by_uc_uid": None,
        "rejected_at": datetime.now(tz=timezone.utc).isoformat(),
        "source": "sync_worker_backfill",
    }
    await session.execute(
        text(MERGE_STATE_SQL),
        merge_params(locked.row_id, {REJECTED_STATE_KEY: rejected}, set()),
    )


async def load_resolved_blocked_ids(
    session: AsyncSession, entity_id: UUID, instance: str = "default"
) -> frozenset[str]:
    """Lock the Stripe state row, then return the ids resolved elsewhere.

    The sync worker calls this at the start of its mapping transaction. The
    lock is held until that transaction commits, so a resolution cannot land
    between this read and a post of the same item.
    """
    return resolved_ids(await lock_stripe_state(session, entity_id, instance))


@dataclass(frozen=True)
class LockedBlockState:
    """What the worker may act on, read under the shared Stripe state lock."""

    exists: bool
    resolved: frozenset[str]
    revision: int
    blocked_id: str | None
    # Items a human rejected as ordinary review rows (REJECTED_STATE_KEY).
    rejected: frozenset[str] = frozenset()
    row_id: str | None = None
    entity_id: UUID | None = None
    instance: str = "default"


async def lock_block_state(
    session: AsyncSession, entity_id: UUID, instance: str = "default"
) -> LockedBlockState:
    """Lock the Stripe state row, then read resolutions, revision and block.

    The sync worker calls this FIRST in its mapping transaction and makes every
    decision about the items it maps (resolved elsewhere, block still the one
    it loaded, rejected by a human) only after it. The lock is held until that
    transaction commits, so a resolve, reopen or rejection either committed
    before this read (and is seen) or waits for the pass to finish.
    """
    locked = await lock_stripe_state_row(session, entity_id, instance)
    if locked is None:
        return LockedBlockState(False, frozenset(), 0, None)
    state = locked[1]
    block = state.get("blocked_item")
    block_id = block.get("id") if isinstance(block, dict) else None
    return LockedBlockState(
        True,
        resolved_ids(state),
        block_revision(state),
        block_id if isinstance(block_id, str) and block_id else None,
        frozenset(rejected_items(state)),
        locked[0],
        entity_id,
        instance,
    )


MERGE_STATE_SQL = """
    UPDATE connector_state
    SET state = (COALESCE(state, '{}'::jsonb) - CAST(:drop_keys AS text[]))
                || CAST(:patch AS jsonb)
    WHERE id = :id
"""


def merge_params(row_id: str, patch: dict[str, Any], drop_keys: set[str]) -> dict[str, Any]:
    """Bind values for MERGE_STATE_SQL. The resolutions key is never dropped."""
    if RESOLVED_STATE_KEY in drop_keys:
        raise ValueError("resolved_blocked_items is never dropped by a merge")
    if REJECTED_STATE_KEY in drop_keys:
        raise ValueError("rejected_stripe_items is never dropped by a merge")
    return {
        "id": row_id,
        "patch": json.dumps(patch),
        "drop_keys": sorted(drop_keys - set(patch)),
    }


def block_revision(state: dict[str, Any] | None) -> int:
    """The state's block revision; 0 for rows written before it existed."""
    value = (state or {}).get(BLOCK_REVISION_KEY)
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0) if value is not None else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _state_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _clean_reason(value: str | None, field: str) -> str | None:
    reason = (value or "").strip() or None
    if reason is not None and not MIN_REASON_CHARS <= len(reason) <= MAX_REASON_CHARS:
        raise BlockedResolutionError(
            422, f"{field} must be {MIN_REASON_CHARS} to {MAX_REASON_CHARS} characters"
        )
    return reason


def _is_lock_timeout(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if code == "55P03" or type(current).__name__ == "LockNotAvailableError":
            return True
        current = getattr(current, "orig", None) or current.__cause__
    return False


async def _lock_workspace_stripe_rows(
    session: AsyncSession, workspace_id: UUID, entity_id: UUID | None
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Take the shared Stripe state lock (see lock_stripe_state) for a human call.

    A sync pass holds the lock for its whole mapping transaction. A human
    call waits at most LOCK_TIMEOUT_MS for it and is then told to retry,
    rather than hanging the request.
    """
    params: dict[str, Any] = {"wid": str(workspace_id)}
    entity_filter = ""
    if entity_id is not None:
        entity_filter = "AND entity_id = :eid"
        params["eid"] = str(entity_id)
    await session.execute(text(f"SET LOCAL lock_timeout = '{int(LOCK_TIMEOUT_MS)}ms'"))
    try:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id::text AS id, entity_id::text AS entity_id, state
                    FROM connector_state
                    WHERE workspace_id = :wid AND connector_name = 'stripe_api'
                      {entity_filter}
                    ORDER BY id
                    FOR UPDATE
                    """  # noqa: S608 - entity_filter is a fixed literal
                ),
                params,
            )
        ).mappings().all()
    except Exception as exc:
        if _is_lock_timeout(exc):
            raise BlockedResolutionError(
                409, "A Stripe sync pass is running for this workspace; retry shortly"
            ) from exc
        raise
    await session.execute(text("SET LOCAL lock_timeout = 0"))
    return [(dict(row), _state_dict(row["state"])) for row in rows]


async def lock_stripe_state_for_approval(
    session: AsyncSession, workspace_id: UUID, entity_id: UUID
) -> frozenset[str]:
    """Take the shared Stripe state lock for a Stripe approval decision.

    app/api/approvals.py decide_approval calls this BEFORE it locks the
    approval row, so approval posting, the sync worker, resolve and reopen all
    take locks in one order: Stripe state row, then approval row. Waits at
    most LOCK_TIMEOUT_MS (then BlockedResolutionError 409). Returns the ids
    resolved elsewhere, read under the lock: approving one of those would
    book it a second time.
    """
    rows = await _lock_workspace_stripe_rows(session, workspace_id, entity_id)
    resolved: set[str] = set()
    for _row, state in rows:
        resolved.update(resolved_ids(state))
    return frozenset(resolved)


async def _stripe_post_exists(session: AsyncSession, entity_id: UUID, bt_id: str) -> bool:
    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM journal_entry
                WHERE entity_id = :eid AND source = 'stripe_api'
                  AND source_external_id = :ext
                LIMIT 1
                """
            ),
            {"eid": str(entity_id), "ext": bt_id},
        )
    ).first()
    return row is not None


def resolution_patch(
    state: dict[str, Any], bt_id: str, resolution: dict[str, Any]
) -> tuple[dict[str, Any], set[str]]:
    """The keys a manual resolution of ``bt_id`` writes, and the keys it drops.

    Mirrors ``_record_sync_outcome(clear_blocked_item=True)``: drop the block
    and that id's failure count. The pinned window and last_sync_at stay, and
    the cursor moves onto the resolved item, which is where the chain stood
    once everything before it had been handled. ``state`` must have been read
    under the shared lock.
    """
    patch: dict[str, Any] = {
        "_stripe_continuation_cursor": bt_id,
        "last_outcome": AUDIT_OUTCOME,
        BLOCK_REVISION_KEY: block_revision(state) + 1,
    }
    drop = {"blocked_item"}
    counts = state.get("_stripe_failure_counts")
    counts = (
        {key: value for key, value in counts.items() if key != bt_id}
        if isinstance(counts, dict)
        else {}
    )
    if counts:
        patch["_stripe_failure_counts"] = counts
    else:
        drop.add("_stripe_failure_counts")
    resolved = state.get(RESOLVED_STATE_KEY)
    resolved = dict(resolved) if isinstance(resolved, dict) else {}
    resolved[bt_id] = resolution
    patch[RESOLVED_STATE_KEY] = resolved
    return patch, drop


def reopen_patch(
    state: dict[str, Any], bt_id: str, blocked_item: dict[str, Any], window: tuple[int, int]
) -> tuple[dict[str, Any], set[str]]:
    """The keys a reopen writes, and the keys it drops.

    The item goes back to the head of the chain as the blocked item, with a
    fresh failure count, inside the window it was blocked in, read again from
    that window's newest end. The worker's normal blocked-item retry then maps
    it through the mapper (normal materiality). Once it advances, the chain
    finishes that window and last_sync_at moves back to its end, so everything
    after it is read again; the mapper's journal idempotency keeps that safe.
    """
    resolved = state.get(RESOLVED_STATE_KEY)
    resolved = {key: value for key, value in (resolved or {}).items() if key != bt_id}
    patch: dict[str, Any] = {
        "blocked_item": blocked_item,
        "window_gte": window[0],
        "window_lt": window[1],
        RESOLVED_STATE_KEY: resolved,
        "last_outcome": REOPEN_OUTCOME,
        BLOCK_REVISION_KEY: block_revision(state) + 1,
    }
    drop = {"_stripe_continuation_cursor", "awaiting_review_item"}
    counts = state.get("_stripe_failure_counts")
    counts = (
        {key: value for key, value in counts.items() if key != bt_id}
        if isinstance(counts, dict)
        else {}
    )
    if counts:
        patch["_stripe_failure_counts"] = counts
    else:
        drop.add("_stripe_failure_counts")
    return patch, drop


async def resolve_blocked_item(
    session: AsyncSession,
    *,
    ctx: WorkspaceContext,
    bt_id: str,
    entity_id: UUID | None,
    journal_entry_id: UUID | None,
    not_books_relevant_reason: str | None,
    scope_check: Callable[[WorkspaceContext, UUID], Any],
) -> dict[str, Any]:
    """Record a manual resolution. Caller owns the transaction and authz floor."""
    reason = (not_books_relevant_reason or "").strip() or None
    if (journal_entry_id is None) == (reason is None):
        raise BlockedResolutionError(
            422, "Provide exactly one of journal_entry_id or not_books_relevant_reason"
        )
    reason = _clean_reason(reason, "not_books_relevant_reason")
    workspace_id = ctx.workspace_id

    # Everything below is decided on state read AFTER taking the shared lock.
    rows = await _lock_workspace_stripe_rows(session, workspace_id, entity_id)
    blocked = [
        (row, state)
        for row, state in rows
        if isinstance(state.get("blocked_item"), dict)
        and isinstance(state["blocked_item"].get("id"), str)
    ]
    matches = [(row, state) for row, state in blocked if state["blocked_item"]["id"] == bt_id]
    if not matches:
        if not blocked:
            raise BlockedResolutionError(409, "No Stripe item is currently blocked")
        raise BlockedResolutionError(409, "bt_id is not the current blocked Stripe item")
    if len(matches) > 1:
        raise BlockedResolutionError(409, "bt_id is blocked on more than one entity; pass entity_id")
    row, state = matches[0]
    row_entity = UUID(row["entity_id"])
    scope_check(ctx, row_entity)
    blocked_item = dict(state["blocked_item"])
    if await _stripe_post_exists(session, row_entity, bt_id):
        # A sync retry posted it before we got the lock. Resolving it now
        # would book it twice; the worker's own outcome write clears the block.
        raise BlockedResolutionError(
            409, "The sync already posted this Stripe item; nothing to resolve"
        )

    journal_ref: dict[str, Any] | None = None
    if journal_entry_id is not None:
        je = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, entity_id::text AS entity_id
                    FROM journal_entry
                    WHERE id = :jid AND workspace_id = :wid
                    """
                ),
                {"jid": str(journal_entry_id), "wid": str(workspace_id)},
            )
        ).mappings().first()
        if je is None:
            raise BlockedResolutionError(
                422, "journal_entry_id does not exist in this workspace's books"
            )
        journal_ref = {"journal_entry_id": je["id"], "journal_entry_entity_id": je["entity_id"]}

    # The item's approval row in ANY state, locked (state row first, then
    # approval rows: the one lock order). A pending "(blocked)" row is
    # decided by this resolution; a row a human already rejected or skipped
    # keeps its status and decision history and gets the resolution attached.
    # Without a row to link, the resolution is refused rather than recorded
    # with no approval.
    approvals = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, status::text AS status,
                       decided_by_uc_uid, decided_at
                FROM approval_queue
                WHERE entity_id = :eid
                  AND subject_type = 'stripe_post_balance_transaction'
                  AND payload ->> 'stripe_balance_txn_id' = :stripe_id
                ORDER BY (status = 'pending') DESC,
                         CASE WHEN status = 'pending' THEN created_at END ASC,
                         decided_at DESC NULLS LAST, created_at DESC
                FOR UPDATE
                """
            ),
            {"eid": str(row_entity), "stripe_id": bt_id},
        )
    ).mappings().all()
    approval = approvals[0] if approvals else None
    if approval is None:
        raise BlockedResolutionError(
            409,
            "No approval row exists for this blocked Stripe item, so the "
            "resolution cannot be linked; let the next sync pass raise it, then retry",
        )
    if approval["status"] not in {"pending", "rejected", "skipped"}:
        raise BlockedResolutionError(
            409,
            f"The approval for this Stripe item is already {approval['status']}; "
            "nothing to resolve",
        )
    approval_id = approval["id"]
    prior_decision: dict[str, Any] | None = None
    if approval["status"] != "pending":
        decided_at = approval.get("decided_at")
        prior_decision = {
            "status": approval["status"],
            "decided_by_uc_uid": approval.get("decided_by_uc_uid"),
            "decided_at": (
                decided_at.isoformat()
                if hasattr(decided_at, "isoformat")
                else decided_at
            ),
        }

    resolved_by = ctx.user_uc_uid
    resolution: dict[str, Any] = {
        "kind": "journal_entry" if journal_ref else "not_books_relevant",
        "resolved_by_uc_uid": resolved_by,
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
        "journal_entry_id": journal_ref["journal_entry_id"] if journal_ref else None,
        "not_books_relevant_reason": reason,
        "blocked_item": blocked_item,
        "approval_id": approval_id,
        "approval_prior_decision": prior_decision,
        # The window the item was blocked in, so a reopen can re-arm it.
        "position": {
            "window_gte": _epoch(state.get("window_gte")),
            "window_lt": _epoch(state.get("window_lt")),
            "continuation_cursor": state.get("_stripe_continuation_cursor"),
        },
    }
    note = (
        f"Resolved elsewhere by {resolved_by}: manual journal entry "
        f"{resolution['journal_entry_id']}"
        if journal_ref
        else f"Resolved elsewhere by {resolved_by}: not books-relevant: {reason}"
    )
    response = {
        "ok": True,
        "bt_id": bt_id,
        "entity_id": str(row_entity),
        "approval_id": approval_id,
        "posted": False,
        "resolution": resolution,
    }
    audit_id = await write_decision_audit(
        session,
        workspace_id=workspace_id,
        entity_id=row_entity,
        ctx=ctx,
        tool_name=AUDIT_TOOL_NAME,
        request_payload={
            "bt_id": bt_id,
            "entity_id": str(entity_id) if entity_id else None,
            "journal_entry_id": str(journal_entry_id) if journal_entry_id else None,
            "not_books_relevant_reason": reason,
        },
        response_payload=response,
        reasoning=note,
        outcome=AUDIT_OUTCOME,
    )
    resolution["audit_id"] = str(audit_id)

    if prior_decision is None:
        linked = await session.execute(
            text(
                """
                UPDATE approval_queue
                SET status = CAST('rejected' AS approval_status),
                    decided_by_uc_uid = :uid,
                    decided_at = now(),
                    reasoning = reasoning || ' | ' || :note,
                    payload = payload || CAST(:payload AS jsonb)
                WHERE id = :id AND status = 'pending'
                RETURNING id::text AS id
                """
            ),
            {
                "id": approval_id,
                "uid": resolved_by,
                "note": note,
                "payload": json.dumps({"blocked_resolution": resolution}),
            },
        )
    else:
        # Keep the human's earlier rejection or skip as it was: status,
        # decided_by and decided_at are untouched; only the note and the
        # resolution are attached.
        linked = await session.execute(
            text(
                """
                UPDATE approval_queue
                SET reasoning = reasoning || ' | ' || :note,
                    payload = payload || CAST(:payload AS jsonb)
                WHERE id = :id AND status = CAST(:status AS approval_status)
                RETURNING id::text AS id
                """
            ),
            {
                "id": approval_id,
                "status": prior_decision["status"],
                "note": note,
                "payload": json.dumps({"blocked_resolution": resolution}),
            },
        )
    if linked.mappings().first() is None:
        # The row is locked above, so this means it changed under us anyway.
        # Refuse: the caller's transaction rolls back the audit row too.
        raise BlockedResolutionError(
            409, "The approval for this Stripe item changed; retry the resolution"
        )

    patch, drop = resolution_patch(state, bt_id, resolution)
    await session.execute(text(MERGE_STATE_SQL), merge_params(row["id"], patch, drop))
    response["audit_id"] = str(audit_id)
    return response


async def reopen_resolved_item(
    session: AsyncSession,
    *,
    ctx: WorkspaceContext,
    bt_id: str,
    entity_id: UUID | None,
    reason: str | None,
    scope_check: Callable[[WorkspaceContext, UUID], Any],
) -> dict[str, Any]:
    """Undo a manual resolution: audited, human, and it posts nothing itself.

    ``bt_id`` leaves ``resolved_blocked_items`` and becomes the blocked item
    again (see reopen_patch), so the next sync pass retries it through the
    mapper with normal materiality. Nothing expires on its own. Caller owns
    the transaction and the authz floor.
    """
    reason = _clean_reason(reason, "reason")
    if reason is None:
        raise BlockedResolutionError(422, "A reason is required to reopen a resolution")
    workspace_id = ctx.workspace_id

    rows = await _lock_workspace_stripe_rows(session, workspace_id, entity_id)
    matches = [(row, state) for row, state in rows if bt_id in resolved_ids(state)]
    if not matches:
        raise BlockedResolutionError(409, "bt_id has no manual resolution to reopen")
    if len(matches) > 1:
        raise BlockedResolutionError(409, "bt_id is resolved on more than one entity; pass entity_id")
    row, state = matches[0]
    row_entity = UUID(row["entity_id"])
    scope_check(ctx, row_entity)
    current_block = state.get("blocked_item")
    if isinstance(current_block, dict) and current_block.get("id"):
        raise BlockedResolutionError(
            409,
            "Another Stripe item is blocked; fix or resolve it before reopening this one",
        )
    resolution = state[RESOLVED_STATE_KEY][bt_id]
    resolution = dict(resolution) if isinstance(resolution, dict) else {}
    position = resolution.get("position") if isinstance(resolution.get("position"), dict) else {}
    window_gte = _epoch(position.get("window_gte"))
    window_lt = _epoch(position.get("window_lt"))
    if window_gte is None or window_lt is None:
        raise BlockedResolutionError(
            409, "This resolution has no recorded sync window, so it cannot be re-armed safely"
        )
    original = resolution.get("blocked_item")
    original = dict(original) if isinstance(original, dict) else {}
    original["id"] = bt_id

    reopened_by = ctx.user_uc_uid
    reopen: dict[str, Any] = {
        "reopened_by_uc_uid": reopened_by,
        "reopened_at": datetime.now(tz=timezone.utc).isoformat(),
        "reason": reason,
        "resolution": resolution,
    }
    manual_je = resolution.get("journal_entry_id")
    note = f"Resolution reopened by {reopened_by}: {reason}"
    response: dict[str, Any] = {
        "ok": True,
        "bt_id": bt_id,
        "entity_id": str(row_entity),
        "posted": False,
        "reopen": reopen,
    }
    if manual_je:
        # Kit decides whether to require a reversal first; until then, say it.
        response["warning"] = (
            f"Manual journal entry {manual_je} cited by the resolution still exists. "
            "If the sync now posts this item, reverse that entry or it is booked twice."
        )
    audit_id = await write_decision_audit(
        session,
        workspace_id=workspace_id,
        entity_id=row_entity,
        ctx=ctx,
        tool_name=REOPEN_AUDIT_TOOL_NAME,
        request_payload={
            "bt_id": bt_id,
            "entity_id": str(entity_id) if entity_id else None,
            "reason": reason,
        },
        response_payload=response,
        reasoning=note,
        outcome=REOPEN_OUTCOME,
    )
    reopen["audit_id"] = str(audit_id)

    approval_id = resolution.get("approval_id")
    if approval_id:
        # The old "(blocked)" row stays decided as history. If the item fails
        # again the worker raises a fresh pending "(blocked)" row as usual.
        await session.execute(
            text(
                """
                UPDATE approval_queue
                SET reasoning = reasoning || ' | ' || :note,
                    payload = payload || CAST(:payload AS jsonb)
                WHERE id = :id AND entity_id = :eid
                """
            ),
            {
                "id": str(approval_id),
                "eid": str(row_entity),
                "note": note,
                "payload": json.dumps({"blocked_reopen": reopen}),
            },
        )
    # A rejected approval for this item holds a blocked retry without running
    # the mapper (scripts/stripe_sync.py). The reopen is the audited decision
    # to run the mapper again, so every rejection of this item that exists now
    # is marked superseded by it. A rejection made AFTER this reopen carries no
    # marker and holds the item again, exactly like any ordinary rejection.
    # Approval rows are locked after the state row: the one lock order.
    superseded = (
        await session.execute(
            text(
                """
                UPDATE approval_queue
                SET payload = payload || CAST(:payload AS jsonb)
                WHERE entity_id = :eid
                  AND subject_type = 'stripe_post_balance_transaction'
                  AND status = 'rejected'
                  AND payload ->> 'stripe_balance_txn_id' = :stripe_id
                  AND payload -> :marker IS NULL
                RETURNING id::text AS id
                """
            ),
            {
                "eid": str(row_entity),
                "stripe_id": bt_id,
                "marker": SUPERSEDED_BY_REOPEN_KEY,
                "payload": json.dumps({
                    SUPERSEDED_BY_REOPEN_KEY: {
                        "audit_id": reopen["audit_id"],
                        "reopened_by_uc_uid": reopened_by,
                        "reopened_at": reopen["reopened_at"],
                    }
                }),
            },
        )
    ).mappings().all()
    reopen["superseded_rejection_ids"] = sorted(row_["id"] for row_ in superseded)
    response["superseded_rejection_ids"] = reopen["superseded_rejection_ids"]

    blocked_item = dict(original)
    blocked_item["reopened"] = {
        "reopened_by_uc_uid": reopened_by,
        "reopened_at": reopen["reopened_at"],
        "audit_id": reopen["audit_id"],
    }
    patch, drop = reopen_patch(state, bt_id, blocked_item, (window_gte, window_lt))
    history = state.get(REOPENED_STATE_KEY)
    history = dict(history) if isinstance(history, dict) else {}
    entries = history.get(bt_id)
    history[bt_id] = [*(entries if isinstance(entries, list) else []), reopen]
    patch[REOPENED_STATE_KEY] = history
    await session.execute(text(MERGE_STATE_SQL), merge_params(row["id"], patch, drop))
    response["audit_id"] = reopen["audit_id"]
    return response
