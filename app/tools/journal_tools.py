"""MCP tools for the journal subsystem (entries, posting log, period close).

These tools mirror the routes in ``app/api/journal.py``:

Reads (viewer floor):
  - list_journal_entries — search + filter posted/reversed entries
  - get_journal_entry — single entry with full audit detail
  - verify_journal_chain — re-hash the per-entity SHA-256 chain
  - list_posting_log — recent posting attempts (incl. blocked/refused)
  - list_closed_periods — locked accounting periods for an entity

Writes:
  - post_journal_entry (accountant) — atomic ledger post via the journal
    primitives. Idempotent on (entity, source, source_external_id).
  - close_period (admin) — lock an accounting period for an entity.

The HTTP layer already centralises the apply logic; we delegate where helpful
to keep one implementation. Append-only and the hash chain invariants live in
the journal module and are inherited.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.journal import (
    PostingOutcome,
    PostingRefusal,
    post_journal_entry as _journal_post,
)
from app.journal.hash_chain import chain_summary, verify_chain
from app.journal.posting import CATEGORY_TO_ACCOUNT
from app.mcp_auth import authorize_workspace
from app.tools._audit import project_decision_audit, write_decision_audit
from app.workspace_session import workspace_session


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_journal_entries(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    category: str | None = None,
    status_filter: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Search posted journal entries with filters.

    Mirrors GET /api/workspaces/{wid}/journal-entries.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Optional — restrict to one entity.
        start_date / end_date: ISO YYYY-MM-DD strings (effective_date range).
        category: Exact category match (e.g. "SaaS · AI").
        status_filter: 'posted' | 'reversed'.
        search: Case-insensitive substring on (payee, description, memo,
            debit_account, credit_account).
        limit: Page size (max 500). Default 100.
        offset: Page offset.

    Returns:
        {
          "entries": [ {<journal-row projection>}, ... ],
          "total": int,
          "categories": [...]   # distinct categories visible to the caller
        }

    Role floor: viewer.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    scope_restriction = assert_entity_in_scope(ctx, entity_id)

    def _parse_iso_date(value: str, field: str) -> dt.date:
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"{field} must be an ISO date (YYYY-MM-DD): {value!r}"
            ) from exc

    where = ["1=1"]
    params: dict[str, Any] = {
        "limit": min(max(int(limit), 1), 500),
        "offset": max(int(offset), 0),
    }
    if entity_id:
        where.append("je.entity_id = :eid")
        params["eid"] = str(entity_id)
    if scope_restriction is not None:
        where.append("je.entity_id = ANY(:entity_scope)")
        params["entity_scope"] = [str(e) for e in scope_restriction]
    if start_date:
        where.append("je.effective_date >= :start_date")
        params["start_date"] = _parse_iso_date(start_date, "start_date")
    if end_date:
        where.append("je.effective_date <= :end_date")
        params["end_date"] = _parse_iso_date(end_date, "end_date")
    if category:
        where.append("je.category = :category")
        params["category"] = category
    if status_filter:
        where.append("je.status = :status_filter")
        params["status_filter"] = status_filter
    if search:
        where.append(
            "(lower(coalesce(je.payee,'')) LIKE :s OR "
            "lower(coalesce(je.description,'')) LIKE :s OR "
            "lower(coalesce(je.memo,'')) LIKE :s OR "
            "lower(je.debit_account) LIKE :s OR "
            "lower(je.credit_account) LIKE :s)"
        )
        params["s"] = f"%{search.lower()}%"
    where_sql = " AND ".join(where)

    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT je.id::text, je.entity_id::text,
                           e.display_name AS entity_display_name,
                           e.slug AS entity_slug,
                           je.posted_at, je.effective_date,
                           je.amount, je.currency_code,
                           je.debit_account, je.credit_account,
                           je.category, je.materiality_tier,
                           je.source, je.source_external_id,
                           je.description, je.payee, je.memo,
                           je.status, je.identity,
                           je.approving_human_uc_uid,
                           encode(je.prev_hash, 'hex') AS prev_hash_hex,
                           encode(je.this_hash, 'hex') AS this_hash_hex
                    FROM journal_entry je
                    LEFT JOIN entity e ON e.id = je.entity_id
                    WHERE {where_sql}
                    ORDER BY je.effective_date DESC, je.posted_at DESC, je.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()
        total = (
            await session.execute(
                text(f"SELECT count(*) FROM journal_entry je WHERE {where_sql}"),
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).scalar_one()

        cat_where = "category IS NOT NULL"
        cat_params: dict[str, Any] = {}
        if scope_restriction is not None:
            cat_where += " AND entity_id = ANY(:entity_scope)"
            cat_params["entity_scope"] = [str(e) for e in scope_restriction]
        categories = (
            await session.execute(
                text(
                    f"SELECT DISTINCT category FROM journal_entry "
                    f"WHERE {cat_where} ORDER BY category"
                ),
                cat_params,
            )
        ).scalars().all()

    def _b(v: Any) -> Any:
        if v is None or isinstance(v, (int, float, bool, str, dict, list)):
            return v
        return str(v)

    return {
        "entries": [{k: _b(v) for k, v in dict(r).items()} for r in rows],
        "total": int(total),
        "categories": list(categories),
    }


async def get_journal_entry(
    workspace_id: UUID,
    entry_id: UUID,
) -> dict[str, Any]:
    """Fetch a single journal entry with the full audit payload.

    Returns ledger + provenance fields (reasoning_trace, model_version,
    reverses_entry_id, hash-chain pointers, etc.). Workspace + entity scope
    enforced: an out-of-scope entry is reported as not found.

    Args:
        workspace_id: Workspace UUID.
        entry_id: Journal entry UUID.

    Returns the full entry row (or raises LookupError if not visible).

    Role floor: viewer.
    """
    ctx = await authorize_workspace(workspace_id, None, min_role="viewer")
    scope_restriction = assert_entity_in_scope(ctx, None)
    where = "je.id = :id"
    params: dict[str, Any] = {"id": str(entry_id)}
    if scope_restriction is not None:
        where += " AND je.entity_id = ANY(:entity_scope)"
        params["entity_scope"] = [str(e) for e in scope_restriction]

    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text(
                    f"""
                    SELECT je.id::text, je.entity_id::text,
                           e.display_name AS entity_display_name,
                           e.slug AS entity_slug,
                           je.posted_at, je.effective_date,
                           je.amount, je.currency_code,
                           je.debit_account, je.credit_account,
                           je.category, je.materiality_tier,
                           je.source, je.source_external_id,
                           je.description, je.payee, je.memo,
                           je.status, je.identity,
                           je.approving_human_uc_uid,
                           je.reverses_entry_id::text AS reverses_entry_id,
                           je.reasoning_trace, je.model_version,
                           encode(je.prev_hash, 'hex') AS prev_hash_hex,
                           encode(je.this_hash, 'hex') AS this_hash_hex
                    FROM journal_entry je
                    LEFT JOIN entity e ON e.id = je.entity_id
                    WHERE {where}
                    """
                ),
                params,
            )
        ).mappings().first()
        if not row:
            raise LookupError(f"Journal entry {entry_id} not found")

    def _b(v: Any) -> Any:
        if v is None or isinstance(v, (int, float, bool, str, dict, list)):
            return v
        return str(v)

    return {k: _b(v) for k, v in dict(row).items()}


async def verify_journal_chain(
    workspace_id: UUID,
    entity_id: UUID,
) -> dict[str, Any]:
    """Re-hash an entity's append-only journal and report tampering.

    Walks every posted entry for the entity, recomputes the per-entry SHA-256
    hash, and compares to the stored prev_hash/this_hash. Tampered entries are
    listed; an empty list = chain is intact.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.

    Returns the chain_summary dict: {"entity_id", "total_entries",
    "tampered_count", "tampered": [...], "is_intact": bool}.

    Role floor: viewer.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    async with workspace_session(SessionLocal, workspace_id) as session:
        tampered = await verify_chain(session, str(entity_id))
    return {"entity_id": str(entity_id), **chain_summary(tampered)}


async def list_posting_log(
    workspace_id: UUID,
    outcome: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List recent journal-posting attempts (the post-attempt audit feed).

    Mirrors GET /api/workspaces/{wid}/posting-log. Every call to
    ``post_journal_entry`` lands one row here regardless of outcome — posted,
    queued, blocked, duplicate. Useful for an agent diagnosing why an attempted
    post didn't land.

    Args:
        workspace_id: Workspace UUID.
        outcome: Optional outcome filter (e.g. 'posted', 'queued_for_confirm',
            'blocked_period_closed', 'duplicate').
        limit: Max rows (max 500). Default 100.

    Returns:
        {"entries": [...]}

    Role floor: viewer.
    """
    ctx = await authorize_workspace(workspace_id, None, min_role="viewer")
    scope_restriction = assert_entity_in_scope(ctx, None)

    async with workspace_session(SessionLocal, workspace_id) as session:
        where = ["workspace_id = :wid"]
        params: dict[str, Any] = {
            "wid": str(workspace_id),
            "limit": min(max(int(limit), 1), 500),
        }
        if outcome:
            where.append("outcome = :o")
            params["o"] = outcome
        if scope_restriction is not None:
            where.append("entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id::text, entity_id::text, source, source_external_id,
                           outcome, journal_entry_id::text, reason, actor_uc_uid,
                           actor_agent_id, amount_cents, category, created_at
                    FROM posting_log
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()

    def _b(v: Any) -> Any:
        if v is None or isinstance(v, (int, float, bool, str, dict, list)):
            return v
        return str(v)

    return {"entries": [{k: _b(v) for k, v in dict(r).items()} for r in rows]}


async def list_closed_periods(
    workspace_id: UUID,
    entity_id: UUID,
) -> dict[str, Any]:
    """List closed accounting periods for one entity.

    Mirrors GET /api/workspaces/{wid}/entities/{eid}/closed-periods. Useful for
    an agent before attempting a post to a period that may be locked.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.

    Returns:
        {"entity_id": "<uuid>", "closed_periods": [
            {"period_end": "YYYY-MM-DD", "closed_at": "...",
             "closed_by_uc_uid": "...", "reason": "..."}, ...]}

    Role floor: viewer.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT period_end::text, closed_at, closed_by_uc_uid, reason
                    FROM closed_period
                    WHERE entity_id = :eid
                    ORDER BY period_end DESC
                    """
                ),
                {"eid": str(entity_id)},
            )
        ).mappings().all()

    def _b(v: Any) -> Any:
        if v is None or isinstance(v, (int, float, bool, str, dict, list)):
            return v
        return str(v)

    return {
        "entity_id": str(entity_id),
        "closed_periods": [{k: _b(v) for k, v in dict(r).items()} for r in rows],
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def _verified_confirmation(
    session,
    *,
    entity_id: UUID,
    source_external_id: str,
    amount_cents: int,
) -> str | None:
    """Resolve a claimed human confirmation to the human who actually decided.

    A tool argument can be set by the model, so ``user_confirmed=True`` on the
    MCP surface is a CLAIM, not an attestation. The only server-verifiable
    evidence of a human decision is an APPROVED approval_queue row for this
    exact transaction (entity + source_external_id + amount). Returns the
    deciding human's uc_uid, or None when no such row exists.

    Amount is matched as its canonical string to avoid cast errors on
    malformed legacy payloads (a non-matching or missing amount simply fails
    the match — fail closed).
    """
    row = (
        await session.execute(
            text(
                """
                SELECT decided_by_uc_uid
                FROM approval_queue
                WHERE entity_id = :eid
                  AND subject_type = 'bank_transaction_import'
                  AND status = 'approved'
                  AND payload->>'source_external_id' = :ext
                  AND payload->>'amount_cents' = :amt
                  AND decided_by_uc_uid IS NOT NULL
                ORDER BY decided_at DESC
                LIMIT 1
                """
            ),
            {
                "eid": str(entity_id),
                "ext": source_external_id,
                "amt": str(int(amount_cents)),
            },
        )
    ).first()
    return row.decided_by_uc_uid if row else None


# Account types whose normal balance implies a direction for the category side.
# Anything not listed (asset, liability, equity, ...) is legitimately signed
# either way — an owner contribution and a distribution both hit equity — so we
# never call those a mismatch.
_OUTFLOW_ACCOUNT_TYPES = frozenset({"expense", "other_expense", "cogs", "tax"})
_INFLOW_ACCOUNT_TYPES = frozenset({"revenue", "other_income"})


async def _derived_sign_matched(
    session,
    *,
    entity_id: UUID,
    category: str,
    amount_cents: int,
) -> bool:
    """Decide server-side whether the amount's sign fits the category.

    ``sign_matched`` feeds the tier-L auto-post trust check, and the agent is
    the wrong authority on it — the server knows the category's account type.
    Resolve the category to its COA account and compare the normal balance to
    the sign: an expense must be an outflow, revenue an inflow.

    Returns True when the direction fits OR when the account type is
    genuinely ambiguous / the account cannot be resolved (an unresolvable
    account blocks the post later on its own; a missing lookup must not
    manufacture a spurious mismatch). Callers AND this with the agent's claim,
    so a caller can only ever make the gate stricter.
    """
    from app.journal.posting import TEMPLATE_CATEGORY_ACCOUNTS, _resolve_category_account

    if any(category in mapping for mapping in TEMPLATE_CATEGORY_ACCOUNTS.values()):
        canonical = await _resolve_category_account(
            session, entity_id=str(entity_id), category=category,
        )
        if canonical is None:
            return True  # unresolved category is refused by the post itself
        predicate = "canonical_form = :target"
        target = canonical
    else:
        target = CATEGORY_TO_ACCOUNT.get(category)
        if not target:
            return True
        predicate = "account_number = :target"
    row = (
        await session.execute(
            text(
                "SELECT account_type FROM chart_of_account "
                "WHERE entity_id = :eid AND " + predicate + " AND is_active = true LIMIT 1"
            ),
            {"eid": str(entity_id), "target": target},
        )
    ).first()
    if row is None or not row.account_type:
        return True
    account_type = str(row.account_type).strip().lower()
    if account_type in _OUTFLOW_ACCOUNT_TYPES:
        return amount_cents < 0
    if account_type in _INFLOW_ACCOUNT_TYPES:
        return amount_cents > 0
    return True


# Structural sanity bound on a single posted amount ($1B). This is a guard
# against a malformed or hallucinated magnitude, NOT a business policy — the
# materiality gate is what decides whether a plausible amount needs a human.
MAX_ABS_AMOUNT_CENTS = 100_000_000_000


def _validate_post_arguments(
    *,
    source: str,
    source_external_id: str,
    description: str,
    category: str,
    category_status: str,
    amount_cents: int,
    payee: str | None,
    model_version: str,
) -> None:
    """Enforce on the MCP surface what ``PostRequest`` enforces over HTTP.

    The HTTP route validates through a pydantic model; the MCP tool took bare
    type hints, so an agent could send an unbounded string or an absurd
    magnitude that the HTTP caller could not. Same limits, same failure.

    ``payee`` and ``model_version`` are bounded here even though the HTTP model
    also omits them: their columns are VARCHAR(256)/VARCHAR(128), so an
    overlong value is a DB DataError surfacing as a 500 rather than a usable
    validation message.
    """
    if category_status not in ("auto", "suggest", "unknown"):
        raise ValueError("category_status must be 'auto', 'suggest', or 'unknown'")

    for name, value, limit in (
        ("source", source, 64),
        ("source_external_id", source_external_id, 256),
        ("description", description, 512),
        ("category", category, 64),
    ):
        if not value:
            raise ValueError(f"{name} is required and must be non-empty")
        if len(value) > limit:
            raise ValueError(
                f"{name} exceeds max length {limit} (got {len(value)} chars)"
            )

    for name, value, limit in (
        ("payee", payee, 256),
        ("model_version", model_version, 128),
    ):
        if value is not None and len(value) > limit:
            raise ValueError(
                f"{name} exceeds max length {limit} (got {len(value)} chars)"
            )

    if amount_cents == 0:
        raise ValueError("amount_cents must be non-zero")
    if abs(amount_cents) > MAX_ABS_AMOUNT_CENTS:
        raise ValueError(
            f"amount_cents magnitude {amount_cents} exceeds the sanity bound "
            f"{MAX_ABS_AMOUNT_CENTS} (${MAX_ABS_AMOUNT_CENTS / 100:,.0f}). "
            "Re-check the units — this field is CENTS, not dollars."
        )


async def post_journal_entry(
    workspace_id: UUID,
    entity_id: UUID,
    source: str,
    source_external_id: str,
    effective_date: str,
    amount_cents: int,
    description: str,
    category: str,
    category_status: str = "auto",
    payee: str | None = None,
    memo: str | None = None,
    sign_matched: bool = True,
    external_account_id: str | None = None,
    receipt_link_id: str | None = None,
    user_confirmed: bool = False,
    reasoning_trace: str = "",
    model_version: str = "",
) -> dict[str, Any]:
    """Atomically post a transaction to the journal (full materiality gate).

    Mirrors POST /api/workspaces/{wid}/journal-entries. The post is idempotent
    on (entity_id, source, source_external_id). A material (M/H) post queues
    for human attestation instead of auto-applying; ``user_confirmed`` is
    honored only after server-side verification against the approval queue
    (see the arg docs below).

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.
        source: Source system label (e.g. 'bofa-statement', 'ebay', 'manual').
        source_external_id: Source's unique id for the transaction.
        effective_date: ISO date string YYYY-MM-DD.
        amount_cents: Signed integer cents (positive = inflow, negative = outflow).
        description: 1-512 char description.
        category: Category key (must be in CATEGORY_TO_ACCOUNT for posting).
        category_status: 'auto' | 'suggest' | 'unknown'. Default 'auto'.
        payee: Optional payee name.
        memo: Optional memo.
        sign_matched: True if the amount sign matches the category's expected
            direction (debit/credit). Default True.
        external_account_id: Optional external account id (e.g. SimpleFIN
            account id) for cash-side resolution.
        receipt_link_id: Optional UUID of a recon_link if a receipt is already
            attached.
        user_confirmed: Claim that a human attested this post. NOT trusted as
            given: the server honors it only when an APPROVED approval_queue
            row exists for this exact (entity, source_external_id, amount) —
            otherwise the full materiality gate applies as if False. To get a
            material entry posted, let it queue and have a human decide it
            (decide_approval / the reconciliation queue).
        reasoning_trace: Free-text reasoning from the agent.
        model_version: Identifier for the agent/model issuing the post.

    Returns:
        on success:
          {"ok": True, "outcome": "posted",
           "journal_entry_id": "<uuid>",
           "materiality_tier": "L"|"M"|"H",
           "reason": "..."}
        on refusal:
          {"ok": False, "outcome": "queued_for_confirm" | "blocked_*"
                          | "duplicate" | ...,
           "reason": "...",
           "existing_entry_id": "<uuid>" | null}

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")

    _validate_post_arguments(
        source=source,
        source_external_id=source_external_id,
        description=description,
        category=category,
        category_status=category_status,
        amount_cents=int(amount_cents),
        payee=payee,
        model_version=model_version,
    )

    try:
        eff_date = dt.date.fromisoformat(effective_date)
    except ValueError as exc:
        raise ValueError(
            f"effective_date must be YYYY-MM-DD; got {effective_date!r}"
        ) from exc

    receipt_uuid = UUID(receipt_link_id) if receipt_link_id else None

    async with workspace_session(SessionLocal, workspace_id) as session:
        # Entity-mapping check (same as HTTP)
        entity_mapped = False
        if external_account_id:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT 1 FROM account_entity_mapping
                        WHERE entity_id = :eid
                          AND source = :src
                          AND external_account_id = :ext
                        LIMIT 1
                        """
                    ),
                    {
                        "eid": str(entity_id),
                        "src": source,
                        "ext": external_account_id,
                    },
                )
            ).first()
            entity_mapped = row is not None

        # user_confirmed is agent-supplied on this surface — derive the real
        # attestation from the approval queue instead of trusting the flag.
        approving_human: str | None = None
        if user_confirmed:
            approving_human = await _verified_confirmation(
                session,
                entity_id=entity_id,
                source_external_id=source_external_id,
                amount_cents=int(amount_cents),
            )

        # Likewise sign_matched: the server owns the category's expected
        # direction. AND with the caller's claim so a caller can only tighten.
        effective_sign_matched = bool(sign_matched) and await _derived_sign_matched(
            session,
            entity_id=entity_id,
            category=category,
            amount_cents=int(amount_cents),
        )

        result = await _journal_post(
            session,
            workspace_id=str(workspace_id),
            entity_id=str(entity_id),
            source=source,
            source_external_id=source_external_id,
            effective_date=eff_date,
            amount_cents=int(amount_cents),
            description=description,
            payee=payee,
            memo=memo,
            category=category,
            category_status=category_status,
            entity_mapped=entity_mapped,
            sign_matched=effective_sign_matched,
            external_account_id=external_account_id,
            has_receipt_link=receipt_uuid is not None,
            receipt_link_id=receipt_uuid,
            posted_by_uc_uid=ctx.user_uc_uid,
            posted_by_agent_id=ctx.agent_id,
            user_confirmed=approving_human is not None,
            approving_human_uc_uid=approving_human,
            reasoning_trace=reasoning_trace,
            model_version=model_version,
        )

        posted = isinstance(result, PostingOutcome)
        entry_id = str(result.journal_entry_id) if posted else None
        audit_outcome = "posted" if posted else result.outcome

        # The append-only agent-decision trail. Posting is the highest-stakes
        # agent write, so it belongs here alongside categorize/link_receipt —
        # and REFUSALS are recorded too: a blocked post is exactly the signal
        # an operator reviewing agent behavior needs to see. Same transaction
        # as the post, so the entry and its provenance commit together.
        audit_id = await write_decision_audit(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            ctx=ctx,
            tool_name="post_journal_entry",
            request_payload={
                "source": source,
                "source_external_id": source_external_id,
                "effective_date": effective_date,
                "amount_cents": int(amount_cents),
                "description": description,
                "payee": payee,
                "category": category,
                "category_status": category_status,
                "external_account_id": external_account_id,
                "sign_matched_claimed": bool(sign_matched),
                "sign_matched_effective": effective_sign_matched,
                "user_confirmed_claimed": bool(user_confirmed),
                "confirmation_verified": approving_human is not None,
            },
            response_payload={
                "outcome": audit_outcome,
                "journal_entry_id": entry_id,
                "materiality_tier": result.materiality.tier if posted else None,
                "reason": result.materiality.reason if posted else result.reason,
                "approving_human_uc_uid": approving_human,
            },
            reasoning=(reasoning_trace or "")[:1000],
            outcome=audit_outcome,
        )

    # Post-commit, best-effort graph mirror (never affects the request).
    project_decision_audit(
        workspace_id=workspace_id,
        entity_id=entity_id,
        audit_id=audit_id,
        ctx=ctx,
        tool_name="post_journal_entry",
        outcome=audit_outcome,
        reasoning=(reasoning_trace or "")[:1000],
        subject_id=entry_id,
    )

    if isinstance(result, PostingOutcome):
        return {
            "ok": True,
            "outcome": "posted",
            "journal_entry_id": str(result.journal_entry_id),
            "materiality_tier": result.materiality.tier,
            "reason": result.materiality.reason,
        }
    assert isinstance(result, PostingRefusal)
    reason = result.reason
    if user_confirmed and approving_human is None:
        reason += (
            " (user_confirmed was supplied but is not trusted from tool"
            " arguments: confirmation is derived server-side from an approved"
            " approval-queue record, and none matched this transaction. Let"
            " the entry queue and have a human decide it via decide_approval"
            " or the reconciliation queue.)"
        )
    return {
        "ok": False,
        "outcome": result.outcome,
        "reason": reason,
        "existing_entry_id": (
            str(result.existing_entry_id) if result.existing_entry_id else None
        ),
    }


async def close_period(
    workspace_id: UUID,
    entity_id: UUID,
    period_end: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Close (lock) an accounting period for an entity.

    Mirrors POST /api/workspaces/{wid}/entities/{eid}/close-period. After this
    succeeds, any post attempt with effective_date <= period_end for the same
    entity is refused with outcome='blocked_period_closed'.

    There is intentionally NO reopen-period MCP tool. Reopening is an ops
    action and stays HTTP / DB-direct with human attestation.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.
        period_end: ISO date string YYYY-MM-DD — the latest effective_date
            covered by the close.
        reason: Optional human-readable reason recorded on the row.

    Returns:
        {"ok": True, "entity_id": "...", "entity_display_name": "...",
         "closed_period": {...}, "entries_in_period": int, "note": "..."}

    Role floor: admin.

    Raises:
        ValueError on a duplicate close for (entity, period_end).
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="admin")

    try:
        period_end_date = dt.date.fromisoformat(period_end)
    except ValueError as exc:
        raise ValueError(
            f"period_end must be YYYY-MM-DD; got {period_end!r}"
        ) from exc

    async with workspace_session(SessionLocal, workspace_id) as session:
        entity_row = (
            await session.execute(
                text("SELECT id::text, display_name FROM entity WHERE id = :eid"),
                {"eid": str(entity_id)},
            )
        ).mappings().first()
        if entity_row is None:
            raise LookupError(f"Entity {entity_id} not found in this workspace")

        existing = (
            await session.execute(
                text(
                    """
                    SELECT period_end, closed_at, closed_by_uc_uid
                    FROM closed_period
                    WHERE entity_id = :eid AND period_end = :pe
                    """
                ),
                {"eid": str(entity_id), "pe": period_end_date},
            )
        ).mappings().first()
        if existing is not None:
            raise ValueError(
                f"Period ending {period_end_date} is already closed for entity "
                f"{entity_id}"
            )

        await session.execute(
            text(
                """
                INSERT INTO closed_period (
                    entity_id, period_end, closed_by_uc_uid, reason, workspace_id
                ) VALUES (:eid, :pe, :uid, :reason, :wid)
                """
            ),
            {
                "eid": str(entity_id),
                "pe": period_end_date,
                "uid": ctx.user_uc_uid,
                "reason": reason,
                "wid": str(workspace_id),
            },
        )

        entries_count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM journal_entry
                    WHERE entity_id = :eid
                      AND effective_date <= :pe
                      AND status = 'posted'
                    """
                ),
                {"eid": str(entity_id), "pe": period_end_date},
            )
        ).scalar_one()

        closed_row = (
            await session.execute(
                text(
                    """
                    SELECT entity_id::text, period_end::text, closed_at,
                           closed_by_uc_uid, reason
                    FROM closed_period
                    WHERE entity_id = :eid AND period_end = :pe
                    """
                ),
                {"eid": str(entity_id), "pe": period_end_date},
            )
        ).mappings().first()

    def _b(v: Any) -> Any:
        if v is None or isinstance(v, (int, float, bool, str, dict, list)):
            return v
        return str(v)

    return {
        "ok": True,
        "entity_id": str(entity_id),
        "entity_display_name": entity_row["display_name"],
        "closed_period": {k: _b(v) for k, v in dict(closed_row).items()},
        "entries_in_period": int(entries_count),
        "note": (
            f"Period ending {period_end_date} is now locked. Any post attempt "
            f"with effective_date <= {period_end_date} for entity {entity_id} "
            "will be refused with outcome='blocked_period_closed'."
        ),
    }
