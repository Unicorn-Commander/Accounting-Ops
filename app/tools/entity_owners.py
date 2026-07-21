"""MCP tools: entity ownership (shareholders / partners) — the Schedule K-1 input.

``books.entity_owner`` (tenant migration 0029) is what the 1120-S / 1065
derivation reads to issue Schedule K-1s: with zero rows the cpa-export sidecar
REFUSES to allocate (``schedule_k1.issued=false`` with a reason — a refusal,
never an estimate). Until now the table had readers but no writer anywhere;
these tools are the missing write/read surface (co-equal with the HTTP pair in
``app/api/tools.py``):

  * ``upsert_entity_owner`` — create or correct one time-sliced ownership row.
    Ownership is CORRECTABLE reference data (unlike the append-only journal),
    so updates are allowed; every write appends a ``decision_audit`` row.
  * ``list_entity_owners`` — read the rows back, oldest slice first.

Semantics (mirrors migration 0029's docstring — read it before "fixing" this):
  * S corporation: ``shares`` is the input. §1377(a)(1) allocates per share,
    per day, so the share COUNT during each effective period is what matters —
    a percentage cannot express a mid-year transfer.
  * Partnership: ``profit_pct`` / ``loss_pct`` / ``capital_pct`` (0..100),
    which routinely differ from one another (§704(a)).
  * ``allocation_basis`` is the safety interlock: NULL means "not recorded"
    and the derivation treats it as a refusal to allocate — never as pro rata.
  * ``tax_id_last4`` holds the LAST 4 digits only; the full SSN/EIN belongs in
    the encrypted-document path, never in this table.

There is deliberately NO delete tool in this pass: closing an ownership slice
is ``effective_to``; hard-deleting K-1 history deserves its own review.

Role floors: upsert=admin (decides who legally receives K-1 income),
list=viewer.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.models.ledger import AllocationBasis, EntityOwner, OwnerType
from app.tools._audit import write_decision_audit
from app.workspace_session import workspace_session

# Allowed vocabularies come from the model enums (single source of truth with
# the CHECK constraints in migration 0029 — the DB is the backstop, these give
# the caller a clean error instead of an IntegrityError).
_OWNER_TYPES = {t.value for t in OwnerType}
_ALLOCATION_BASES = {b.value for b in AllocationBasis}

_MAX_OWNER_NAME = 300  # mirrors entity_owner.owner_name VARCHAR(300)


def _date(value: str | None, field: str) -> dt.date | None:
    """Parse an ISO date string; '' / None -> None."""
    if value in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}") from exc


def _decimal(value: str | None, field: str) -> Decimal | None:
    """Parse a decimal STRING (house pattern — floats lose precision in JSON)."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a decimal string, got {value!r}") from exc


def _pct(value: str | None, field: str) -> Decimal | None:
    """Percentage 0..100 — mirrors ck_entity_owner_pct_range."""
    parsed = _decimal(value, field)
    if parsed is not None and not (Decimal(0) <= parsed <= Decimal(100)):
        raise ValueError(f"{field} must be between 0 and 100, got {value!r}")
    return parsed


def _serialize(row: EntityOwner) -> dict[str, Any]:
    """One ownership row -> the wire shape (Decimals as strings, dates ISO)."""
    return {
        "owner_id": str(row.id),
        "owner_name": row.owner_name,
        "owner_type": row.owner_type,
        "tax_id_last4": row.tax_id_last4,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
        "shares": str(row.shares) if row.shares is not None else None,
        "profit_pct": str(row.profit_pct) if row.profit_pct is not None else None,
        "loss_pct": str(row.loss_pct) if row.loss_pct is not None else None,
        "capital_pct": str(row.capital_pct) if row.capital_pct is not None else None,
        "is_general_partner": row.is_general_partner,
        "is_officer": row.is_officer,
        "allocation_basis": row.allocation_basis,
        "notes": row.notes,
    }


async def upsert_entity_owner(
    workspace_id: UUID,
    entity_id: UUID,
    owner_name: str,
    effective_from: str,
    reasoning: str,
    owner_id: UUID | None = None,
    owner_type: str = "individual",
    tax_id_last4: str | None = None,
    effective_to: str | None = None,
    shares: str | None = None,
    profit_pct: str | None = None,
    loss_pct: str | None = None,
    capital_pct: str | None = None,
    is_general_partner: bool | None = None,
    is_officer: bool | None = None,
    allocation_basis: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create (or update, when ``owner_id`` is given) one time-sliced ownership row.

    This is the Schedule K-1 input: without ``entity_owner`` rows the 1120-S /
    1065 derivation refuses to issue K-1s. ``shares`` drives the S-corp
    per-share-per-day allocation (§1377(a)(1)); the ``*_pct`` fields drive the
    partnership allocation. ``allocation_basis`` is an attestation about what
    the governing agreement says — leaving it NULL is a refusal to allocate,
    never pro-rata.

    Args:
        workspace_id: workspace to operate on.
        entity_id: entity within the workspace (entity-scoped).
        owner_name: legal name exactly as it must appear on the Schedule K-1.
        effective_from: first day this record is in effect (YYYY-MM-DD).
        reasoning: why this write is being made — goes into ``decision_audit``.
        owner_id: existing row to correct; omit to create a new row.
        owner_type: individual | entity | trust | estate.
        tax_id_last4: LAST 4 digits of the owner's SSN/EIN — never the full TIN.
        effective_to: last day in effect (YYYY-MM-DD); omit while still an owner.
        shares: S-corp share count for this period, as a decimal string.
        profit_pct: partnership profit percentage 0..100, as a decimal string.
        loss_pct: partnership loss percentage 0..100, as a decimal string.
        capital_pct: partnership capital percentage 0..100, as a decimal string.
        is_general_partner: TRUE general / FALSE limited (1065 K-1 item G).
        is_officer: shareholder is also an officer (S-corp reasonable comp).
        allocation_basis: per_share_per_day | partnership_agreement_pro_rata |
            partnership_agreement_special. Omit = not recorded (refusal).
        notes: free-form context for the reviewer.

    Returns:
        {"owner_id": str, "created": bool, "owner": {...serialized row...}}

    Role floor: admin (drives who legally receives K-1 income).
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="admin")

    # Validate BEFORE opening a session — same rules the 0029 CHECK constraints
    # enforce, surfaced as clean ValueErrors instead of IntegrityErrors.
    name = (owner_name or "").strip()
    if not name:
        raise ValueError("owner_name is required")
    if len(name) > _MAX_OWNER_NAME:
        raise ValueError(f"owner_name must be at most {_MAX_OWNER_NAME} characters")
    if not (reasoning or "").strip():
        raise ValueError("reasoning is required — this write is audited")
    if owner_type not in _OWNER_TYPES:
        raise ValueError(f"owner_type must be one of {sorted(_OWNER_TYPES)}, got {owner_type!r}")
    if allocation_basis is not None and allocation_basis not in _ALLOCATION_BASES:
        raise ValueError(
            f"allocation_basis must be omitted or one of {sorted(_ALLOCATION_BASES)}, "
            f"got {allocation_basis!r}"
        )
    if tax_id_last4 is not None and not (len(tax_id_last4) == 4 and tax_id_last4.isdigit()):
        raise ValueError("tax_id_last4 must be exactly 4 digits (never the full SSN/EIN)")
    eff_from = _date(effective_from, "effective_from")
    if eff_from is None:
        raise ValueError("effective_from is required (YYYY-MM-DD)")
    eff_to = _date(effective_to, "effective_to")
    if eff_to is not None and eff_to < eff_from:
        raise ValueError("effective_to must be on or after effective_from")
    shares_dec = _decimal(shares, "shares")
    if shares_dec is not None and shares_dec < 0:
        raise ValueError(f"shares must be >= 0, got {shares!r}")
    profit_dec = _pct(profit_pct, "profit_pct")
    loss_dec = _pct(loss_pct, "loss_pct")
    capital_dec = _pct(capital_pct, "capital_pct")

    async with workspace_session(SessionLocal, workspace_id) as session:
        row: EntityOwner | None = None
        if owner_id is not None:
            # RLS pins the workspace; the entity check below closes the
            # cross-entity-within-workspace hole. Belt and braces on both.
            row = await session.get(EntityOwner, owner_id)
            if row is None or row.entity_id != entity_id or row.workspace_id != workspace_id:
                raise ValueError("owner_id not found for this entity")
        created = row is None
        if row is None:
            row = EntityOwner(
                id=uuid4(),
                workspace_id=workspace_id,
                entity_id=entity_id,
                owner_name=name,
                effective_from=eff_from,
            )
            session.add(row)
        row.owner_name = name
        row.owner_type = owner_type
        row.tax_id_last4 = tax_id_last4
        row.effective_from = eff_from
        row.effective_to = eff_to
        row.shares = shares_dec
        row.profit_pct = profit_dec
        row.loss_pct = loss_dec
        row.capital_pct = capital_dec
        row.is_general_partner = is_general_partner
        row.is_officer = is_officer
        row.allocation_basis = allocation_basis
        row.notes = notes
        row.updated_at = dt.datetime.now(dt.timezone.utc)
        await session.flush()  # 0029's CHECK constraints are the backstop

        await write_decision_audit(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            ctx=ctx,
            tool_name="upsert_entity_owner",
            request_payload={
                "owner_id": str(owner_id) if owner_id else None,
                "owner_name": name,
                "owner_type": owner_type,
                "tax_id_last4": tax_id_last4,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "shares": shares,
                "profit_pct": profit_pct,
                "loss_pct": loss_pct,
                "capital_pct": capital_pct,
                "is_general_partner": is_general_partner,
                "is_officer": is_officer,
                "allocation_basis": allocation_basis,
            },
            response_payload={"owner_id": str(row.id), "created": created},
            reasoning=reasoning,
            outcome="applied",
        )

        return {"owner_id": str(row.id), "created": created, "owner": _serialize(row)}


async def list_entity_owners(
    workspace_id: UUID,
    entity_id: UUID,
) -> list[dict[str, Any]]:
    """All ownership rows (shareholders / partners) for an entity.

    Time-sliced rows come back oldest slice first — the same records the
    1120-S / 1065 Schedule K-1 derivation reads. An empty list means the
    derivation will refuse to issue K-1s for this entity.

    Args:
        workspace_id: workspace to operate on.
        entity_id: entity within the workspace (entity-scoped).

    Returns:
        List of serialized ownership rows (Decimals as strings, dates ISO).

    Role floor: viewer (read-only).
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            (
                await session.execute(
                    select(EntityOwner)
                    .where(EntityOwner.entity_id == entity_id)
                    .order_by(EntityOwner.effective_from, EntityOwner.owner_name)
                )
            )
            .scalars()
            .all()
        )
        return [_serialize(r) for r in rows]
