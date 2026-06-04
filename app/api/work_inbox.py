"""Cross-client CPA "work inbox" (v1) — one merged list of actionable items.

THE GAP THIS CLOSES
-------------------
``GET /api/me/clients`` + ``/clients/triage`` show cross-client *counts*, but
every actual item a CPA must act on lives in a *per-workspace* queue. To work
the items the CPA switches workspaces ~20 times. This endpoint iterates every
workspace the caller can act in and returns the OPEN items themselves — one
merged, stalest-first list — so the CPA can triage across all clients in a
single view, then deep-link into the specific workspace+page to act.

ENDPOINT
--------
  GET /api/me/work-inbox
      ?max_total=300&max_per_workspace=100

For each workspace the caller belongs to with a role that can ACT on
reviews/approvals (rank >= ``accountant`` — i.e. accountant / cpa / admin /
owner / platform_admin; this is the floor the per-workspace approvals list +
tax-review resolve require), opens that workspace's ``workspace_session`` and
pulls:

  1. ``approval_queue`` WHERE status='pending' — LEFT JOIN
     ``approval_recommendation`` ON approval_id for the AI's
     recommended_decision / recommended_category / confidence.
  2. ``tax_review_queue`` OPEN items (status IN ('pending','reviewed') — the two
     non-terminal states the resolve endpoint accepts; 'resolved'/'rejected' are
     terminal and excluded).

RLS / isolation: every per-workspace read happens INSIDE that workspace's
``workspace_session`` (search_path + app.current_workspace pinned), exactly like
``/clients/triage``. Entity-scope (BOLA): a member scoped to specific entities
on a workspace only sees that workspace's items for their entities. A single
broken workspace degrades to zero items for that workspace — it never 500s the
inbox. All SQL is parameterized.

This endpoint is strictly READ-ONLY: it opens no write transaction, touches no
ledger / append-only table, and writes no audit row.

The merge / sort (stalest first) / cap (``truncated`` + ``dropped``) / totals
math is the PURE function ``app.metrics.work_inbox.build_work_inbox`` so it is
unit-tested without a stack.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.db import SessionLocal
from app.metrics.work_inbox import (
    DEFAULT_MAX_PER_WORKSPACE,
    DEFAULT_MAX_TOTAL,
    ITEM_TYPE_APPROVAL,
    ITEM_TYPE_TAX_REVIEW,
    build_work_inbox,
)
from app.tenancy import role_at_least
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.work_inbox")

router = APIRouter(tags=["clients"])

# The role floor a member needs in a workspace for its items to appear in the
# work inbox. ``accountant`` (rank 3) == ``cpa`` rank and is the floor the
# per-workspace approvals list + tax-review resolve endpoints require to ACT.
# Members below this (viewer / client_user / bookkeeper) cannot decide
# approvals, so their workspaces are not actionable for them here.
_ACTOR_ROLE_FLOOR = "accountant"

# Tax-review statuses that are still OPEN (not terminal). Mirrors the resolve
# endpoint, which accepts a transition only from 'pending' or 'reviewed'.
_OPEN_TAX_REVIEW_STATUSES = ("pending", "reviewed")

# Per-workspace SELECT cap (defense in depth: even before the pure-function cap,
# we never pull an unbounded result set out of one workspace). Generous headroom
# above the default per-workspace response cap so the response cap, not this, is
# what the UI sees as "truncated".
_SQL_FETCH_LIMIT = 500


async def _user_id_for(uc_uid: str) -> UUID | None:
    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text("SELECT id FROM public.user_account WHERE uc_uid = :uc"),
                    {"uc": uc_uid},
                )
            ).first()
            return row.id if row else None


def _age_hours(created_at: dt.datetime | None, now: dt.datetime) -> float:
    """Whole-ish hours between created_at and now (>= 0). None -> 0.0."""
    if created_at is None:
        return 0.0
    # created_at is tz-aware (TIMESTAMPTZ). Guard against a naive value just in
    # case a driver hands one back.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    delta = now - created_at
    return max(delta.total_seconds() / 3600.0, 0.0)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _approval_title(subject_type: str, payload: dict[str, Any] | None) -> str:
    """A short human summary for an approval row from subject_type + payload."""
    payload = payload or {}
    if subject_type == "bank_transaction_import":
        payee = payload.get("payee") or payload.get("description")
        cat = payload.get("category")
        if payee and cat:
            return f"Bank import: {payee} → {cat}"
        if payee:
            return f"Bank import: {payee}"
        return "Bank transaction import"
    if subject_type == "journal_recategorization":
        frm = payload.get("from_category")
        to = payload.get("new_category")
        if frm and to:
            return f"Recategorize: {frm} → {to}"
        return "Journal recategorization"
    if subject_type == "compliance_rule":
        return "Proposed compliance rule"
    # Fallback: humanize the subject_type.
    return subject_type.replace("_", " ").capitalize() if subject_type else "Approval"


# tax_review_queue.confidence ('low'|'medium'|'high') maps to a review-urgency
# "severity": a LOW-confidence specialist position is the one most needing a
# human's eyes, so confidence is inverted. flagged_position outranks the others
# at equal confidence (a flagged risk beats a routine memo).
_CONFIDENCE_TO_SEVERITY = {"low": "high", "medium": "medium", "high": "low"}


def _tax_review_severity(item_type: str, confidence: str | None) -> str:
    base = _CONFIDENCE_TO_SEVERITY.get((confidence or "").lower(), "medium")
    if item_type == "flagged_position" and base == "low":
        # A flagged risk is never "low" attention even at high confidence.
        return "medium"
    return base


def _tax_review_title(
    item_type: str, form_reference: str | None, content: dict[str, Any] | None
) -> str:
    """A short human summary for a tax-review row from item_type + content."""
    content = content or {}

    def _clip(s: Any, n: int = 140) -> str:
        s = str(s).strip()
        return s if len(s) <= n else s[: n - 1].rstrip() + "…"

    if item_type == "flagged_position":
        issue = content.get("issue")
        if issue:
            return _clip(f"Flagged: {issue}")
        return f"Flagged position ({form_reference})" if form_reference else "Flagged position"
    if item_type == "form_line_proposal":
        line = content.get("line")
        label = content.get("label")
        if line and label:
            return _clip(f"Line {line}: {label}")
        if form_reference:
            return f"Form line proposal ({form_reference})"
        return "Form line proposal"
    if item_type in ("memo", "question"):
        txt = content.get("text")
        if txt:
            prefix = "Memo" if item_type == "memo" else "Question"
            return _clip(f"{prefix}: {txt}")
        return item_type.capitalize()
    # Unknown item_type — humanize it.
    return item_type.replace("_", " ").capitalize() if item_type else "Tax review item"


async def _approvals_for_workspace(
    ws,
    *,
    workspace_id: str,
    workspace_name: str,
    scope: list[str] | None,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    """OPEN approval_queue items for one workspace, with the AI recommendation.

    Runs INSIDE the caller-provided ``workspace_session`` (``ws``). Entity-scope
    is applied as a row predicate when ``scope`` is not None (BOLA). LEFT JOINs
    approval_recommendation (1:1 via the unique approval_id) for the AI's
    recommended_decision / recommended_category / confidence.
    """
    params: dict[str, Any] = {"limit": _SQL_FETCH_LIMIT}
    where = ["aq.status = 'pending'"]
    if scope is not None:
        where.append("aq.entity_id = ANY(:scope)")
        params["scope"] = scope
    where_sql = " AND ".join(where)

    rows = (
        await ws.execute(
            text(
                f"""
                SELECT aq.id::text                AS id,
                       aq.entity_id::text         AS entity_id,
                       aq.subject_type            AS subject_type,
                       aq.amount                  AS amount,
                       aq.currency_code           AS currency_code,
                       aq.payload                 AS payload,
                       aq.created_at              AS created_at,
                       ar.recommended_decision    AS recommended_decision,
                       ar.recommended_category    AS recommended_category,
                       ar.confidence              AS confidence
                FROM approval_queue aq
                LEFT JOIN approval_recommendation ar
                       ON ar.approval_id = aq.id
                WHERE {where_sql}
                ORDER BY aq.created_at ASC, aq.id ASC
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()

    out: list[dict[str, Any]] = []
    for r in rows:
        subject_type = r["subject_type"]
        amount = r["amount"]
        out.append(
            {
                "id": r["id"],
                "workspace_id": workspace_id,
                "workspace_display_name": workspace_name,
                "entity_id": r["entity_id"],
                "item_type": ITEM_TYPE_APPROVAL,
                "subject_type": subject_type,
                "title": _approval_title(subject_type, r["payload"]),
                "amount": str(amount) if amount is not None else None,
                "currency_code": r["currency_code"],
                "recommended_decision": r["recommended_decision"],
                "recommended_category": r["recommended_category"],
                "confidence": r["confidence"],
                "severity": None,
                "created_at": _iso(r["created_at"]),
                "age_hours": _age_hours(r["created_at"], now),
                "deep_link": _deep_link(
                    "approval", workspace_id, r["entity_id"], r["id"]
                ),
            }
        )
    return out


async def _tax_reviews_for_workspace(
    ws,
    *,
    workspace_id: str,
    workspace_name: str,
    scope: list[str] | None,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    """OPEN tax_review_queue items for one workspace.

    Runs INSIDE the caller-provided ``workspace_session``. title + severity are
    DERIVED (the table has no such columns) from item_type / content / confidence.
    """
    params: dict[str, Any] = {
        "statuses": list(_OPEN_TAX_REVIEW_STATUSES),
        "limit": _SQL_FETCH_LIMIT,
    }
    where = ["trq.status = ANY(:statuses)"]
    if scope is not None:
        where.append("trq.entity_id = ANY(:scope)")
        params["scope"] = scope
    where_sql = " AND ".join(where)

    rows = (
        await ws.execute(
            text(
                f"""
                SELECT trq.id::text         AS id,
                       trq.entity_id::text  AS entity_id,
                       trq.item_type        AS item_type,
                       trq.form_reference   AS form_reference,
                       trq.content          AS content,
                       trq.confidence       AS confidence,
                       trq.status           AS status,
                       trq.created_at       AS created_at
                FROM tax_review_queue trq
                WHERE {where_sql}
                ORDER BY trq.created_at ASC, trq.id ASC
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()

    out: list[dict[str, Any]] = []
    for r in rows:
        item_type = r["item_type"]
        confidence = r["confidence"]
        out.append(
            {
                "id": r["id"],
                "workspace_id": workspace_id,
                "workspace_display_name": workspace_name,
                "entity_id": r["entity_id"],
                "item_type": ITEM_TYPE_TAX_REVIEW,
                # subject_type carries the tax-review discriminator (parallel to
                # an approval's subject_type): form_line_proposal / flagged_position
                # / memo / question.
                "subject_type": item_type,
                "title": _tax_review_title(item_type, r["form_reference"], r["content"]),
                "amount": None,
                "currency_code": None,
                "recommended_decision": None,
                "recommended_category": None,
                "confidence": confidence,
                "severity": _tax_review_severity(item_type, confidence),
                "created_at": _iso(r["created_at"]),
                "age_hours": _age_hours(r["created_at"], now),
                "deep_link": _deep_link(
                    "tax_review", workspace_id, r["entity_id"], r["id"]
                ),
            }
        )
    return out


def _deep_link(
    item_type: str,
    workspace_id: str,
    entity_id: str | None,
    item_id: str | None = None,
) -> str:
    """Build a workspace-scoped deep link to the page that works this item.

    Mirrors ``app/api/clients.py:_deep_link_for_tool`` in spirit: the UI
    cookie-swaps to ``workspace_id`` then router.push(this path). Approvals land
    on ``/approvals``; tax-review items on ``/taxes``. The workspace id is
    carried as a query param so the web layer can switch workspaces first; the
    item id (``focus``) lets the target page scroll to / highlight the exact row.
    """
    page = "/approvals" if item_type == "approval" else "/taxes"
    base = f"{page}?workspace={workspace_id}"
    if entity_id:
        base += f"&entity={entity_id}"
    if item_id:
        base += f"&focus={item_id}"
    return base


@router.get("/api/me/work-inbox")
async def work_inbox(
    max_total: int = Query(default=DEFAULT_MAX_TOTAL, ge=1, le=1000),
    max_per_workspace: int = Query(default=DEFAULT_MAX_PER_WORKSPACE, ge=1, le=500),
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """One merged, stalest-first list of OPEN actionable items across clients.

    See the module docstring for the full contract. Returns
    ``{items, totals, truncated, dropped}`` where ``items`` is sorted by
    ``age_hours`` DESC (stalest first) and capped (``truncated``/``dropped``
    make any drop visible). Best-effort per workspace: a broken workspace
    contributes nothing and is logged, never raised.
    """
    user_id = await _user_id_for(auth.uc_uid)
    if user_id is None:
        return {
            "items": [],
            "totals": {
                "total": 0,
                "by_item_type": {ITEM_TYPE_APPROVAL: 0, ITEM_TYPE_TAX_REVIEW: 0},
                "by_workspace": [],
            },
            "truncated": False,
            "dropped": 0,
        }

    # Memberships: id, display_name, role, entity_scope. Same gate every other
    # surface uses — the caller only ever sees workspaces they have a row in.
    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                            w.id::text     AS id,
                            w.display_name AS display_name,
                            m.role::text   AS role,
                            m.entity_scope AS entity_scope
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :user_id
                          AND w.status = 'active'
                        ORDER BY w.display_name
                        """
                    ),
                    {"user_id": user_id},
                )
            ).mappings().all()

    now = dt.datetime.now(dt.timezone.utc)
    collected: list[dict[str, Any]] = []

    for row in rows:
        # Only workspaces where the caller can ACT on reviews/approvals.
        if not role_at_least(row["role"], _ACTOR_ROLE_FLOOR):
            continue

        workspace_id = row["id"]
        workspace_name = row["display_name"]
        # Entity-scope (BOLA): None == all entities; a list == restricted to
        # those entities. Normalize to a list[str] of UUIDs for the SQL ANY().
        raw_scope = row["entity_scope"]
        scope = (
            [str(e) for e in raw_scope]
            if raw_scope is not None and len(raw_scope) > 0
            else None
        )

        try:
            async with workspace_session(SessionLocal, workspace_id) as ws:
                collected.extend(
                    await _approvals_for_workspace(
                        ws,
                        workspace_id=workspace_id,
                        workspace_name=workspace_name,
                        scope=scope,
                        now=now,
                    )
                )
                collected.extend(
                    await _tax_reviews_for_workspace(
                        ws,
                        workspace_id=workspace_id,
                        workspace_name=workspace_name,
                        scope=scope,
                        now=now,
                    )
                )
        except Exception as exc:  # noqa: BLE001 — one bad workspace must not 500
            _log.info(
                "[work-inbox] workspace %s item pull failed: %s", workspace_id, exc
            )
            continue

    return build_work_inbox(
        collected,
        max_total=max_total,
        max_per_workspace=max_per_workspace,
    )


__all__ = ("router",)
