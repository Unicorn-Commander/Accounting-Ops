"""CFO brief: the plain-language "concierge home" payload.

This endpoint backs the CFO brief card at the TOP of /dashboard. It is a
DETERMINISTIC assembler — NOT a live LLM call. It reads three existing,
already-RBAC-enforced sources and synthesizes one small, ranked, plain-
language payload for a NON-accountant:

  1. open-items aggregator  (app/api/open_items.py :: get_open_items)
       — time-sensitive items, documents the owner must pull, decisions needing
         their OK, submissions awaiting follow-up. This is the "open items for
         the workspace" source of truth.
  2. cash position          (app/api/cash_position.py :: workspace_cash_position)
       — live SimpleFIN assets vs. credit-card balances.
  3. sentinel findings      (sentinel_finding table)
       — the autonomous CPA daemon's open findings (officer comp missing,
         filing deadline approaching, cash runway low, etc.).

We REUSE the open-items and cash-position endpoint functions directly
(calling them in-process with the resolved WorkspaceContext) rather than
duplicating their SQL — they enforce their own role floor + entity scope,
so the brief inherits all of it. Sentinel findings are read with the same
workspace_session + scope pattern as app/api/sentinel.py::list_findings.

Ranking + plain-language phrasing live in pure module-level helpers
(_rank_priorities, _plain_cash_status, _runway_from_pl) so the assembly
logic is unit-testable with no DB.

If any source errors, the brief degrades to whatever it could gather; the
web layer additionally degrades to the plain dashboard if the whole brief
fails. Read floor: viewer. Entity-scope enforced (by the reused endpoints
+ the sentinel query below).
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.cfo_brief")

router = APIRouter(tags=["cfo-brief"])


# ---------------------------------------------------------------------------
# Ranking constants
# ---------------------------------------------------------------------------

# Severity weights for sentinel findings. Higher = more urgent. Used both to
# pick the headline finding(s) and to rank them against open-items priorities.
_SEVERITY_RANK: dict[str, int] = {
    "critical": 100,
    "high": 80,
    "medium": 50,
    "low": 30,
    "info": 10,
}

# Base priority scores for the deterministic ranker. A higher score floats to
# the top of "what needs you now". These are intentionally coarse buckets;
# within a bucket we break ties on recency / severity / due-date.
_SCORE_TIME_SENSITIVE = 90
_SCORE_AWAITING_FOLLOWUP = 85  # the owner must call the taxing authority
_SCORE_DECISION = 70  # needs the owner's OK
_SCORE_DOCUMENT = 55  # the owner must go pull a document
# Sentinel findings are scored from their severity (see _finding_score).

# How many priorities the brief surfaces. The card shows the top 2-3; we
# return a few extra so the UI can decide.
_TOP_N = 5

# Months of runway under which we phrase cash as a warning rather than a
# reassurance.
_RUNWAY_WARN_MONTHS = Decimal("6")


# ---------------------------------------------------------------------------
# Wire model
# ---------------------------------------------------------------------------


class CfoBriefPriority(BaseModel):
    """One ranked 'what needs you now' line, in plain language."""

    id: str
    # Coarse kind, drives the icon + accent on the card.
    kind: str  # "time_sensitive" | "decision" | "document" | "filing" | "finding"
    severity: str  # "critical" | "high" | "medium" | "low" | "info"
    headline: str  # plain-language, non-accountant phrasing
    detail: str | None = None
    # One-tap deep-link target (relative path within the web app).
    action_href: str
    action_label: str
    score: int  # ranking score (debug / stable sort key)


class CfoBriefCash(BaseModel):
    """Plain-words cash status."""

    configured: bool
    headline: str  # e.g. "$182,304 in the bank"
    detail: str | None = None  # e.g. "~7 months runway" or credit-card note
    assets_total: str | None = None
    liabilities_total: str | None = None
    net_total: str | None = None
    months_runway: str | None = None  # null = not burning / unknown
    monthly_burn: str | None = None  # avg monthly net cash burn (>0 = burning); null = unknown
    tone: str  # "credit" | "debit" | "pending" | "ink"


class CfoBriefResponse(BaseModel):
    workspace_id: str
    entity_id: str | None
    as_of: str
    # The headline 2-3 (up to _TOP_N) ranked priorities.
    priorities: list[CfoBriefPriority]
    # Total count of action-required items (mirrors open_items.action_required
    # plus open sentinel findings), so the card can say "and N more".
    action_required_total: int
    cash: CfoBriefCash
    # A single plain-language summary sentence ("3 things need your OK; you
    # have ~7 months of runway"). Deterministically composed.
    summary_line: str
    # Cold-start flag: TRUE when this workspace has no bank connected AND no
    # posted journal entries — a brand-new user who hasn't started yet. The card
    # uses this to show "here's how to begin" guidance instead of the misleading
    # "you're all caught up" (which reads like the product is empty/broken).
    onboarding: bool = False
    # Deep-link to keep talking to the CFO about everything here.
    ask_cfo_href: str
    # Which sources actually contributed (for graceful-degradation debugging
    # + so the UI can note "cash unavailable" without guessing).
    sources_ok: dict[str, bool]


# ---------------------------------------------------------------------------
# Pure helpers — no DB. Unit-tested directly.
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _fmt_usd(value: Any) -> str:
    """$1,234 (whole dollars — the brief is for a non-accountant glance)."""
    d = _to_decimal(value)
    if d is None:
        return "$—"
    sign = "-" if d < 0 else ""
    whole = abs(d).quantize(Decimal("1"))
    return f"{sign}${whole:,}"


def _runway_from_pl(
    cash: Any, trailing_net_income: Any, window_months: int
) -> Decimal | None:
    """Deterministic months-of-runway from net cash + trailing net income.

    Mirrors app/tools/reporting.py::get_runway's model:
        avg_monthly_net_burn = -(trailing_net_income) / window_months
        runway = cash / avg_monthly_net_burn   (only when burning)

    Returns None when not burning (cash-flow positive) or inputs are unusable —
    "runway not bounded by burn", so we don't show a scary number.
    """
    cash_d = _to_decimal(cash)
    ni_d = _to_decimal(trailing_net_income)
    try:
        wm = int(window_months)
    except (TypeError, ValueError):
        return None
    if cash_d is None or ni_d is None or wm <= 0:
        return None
    avg_monthly_net_burn = -(ni_d / Decimal(wm))
    if avg_monthly_net_burn <= 0:
        return None  # not burning
    if cash_d <= 0:
        return Decimal("0")
    return (cash_d / avg_monthly_net_burn).quantize(Decimal("0.1"))


def _plain_cash_status(
    cash: dict[str, Any] | None,
    months_runway: Decimal | None,
    monthly_burn: Decimal | None = None,
) -> CfoBriefCash:
    """Turn the cash-position payload into plain words for a non-accountant.

    Avoids jargon: 'in the bank', 'on the credit cards', 'months of runway'.
    """
    if not cash or not cash.get("configured"):
        return CfoBriefCash(
            configured=False,
            headline="Bank not connected yet",
            detail="Connect a bank to see live cash.",
            tone="ink",
        )

    assets = cash.get("assets_total") or cash.get("total_usd")
    liabilities = cash.get("liabilities_total") or "0"
    net = cash.get("net_total") or cash.get("total_usd")

    headline = f"{_fmt_usd(assets)} in the bank"

    detail_bits: list[str] = []
    liab_d = _to_decimal(liabilities)
    if liab_d is not None and liab_d > 0:
        detail_bits.append(f"{_fmt_usd(liabilities)} owed on credit cards")
    if months_runway is not None:
        # Round to a friendly integer-ish phrase.
        mr = months_runway.quantize(Decimal("0.1"))
        if mr < Decimal("1"):
            detail_bits.append("under a month of runway at the current burn")
        else:
            detail_bits.append(f"~{mr.normalize()} months of runway at the current burn")

    # Tone: green if net positive and runway healthy; amber if low runway;
    # red if net negative.
    net_d = _to_decimal(net)
    tone = "credit"
    if net_d is not None and net_d < 0:
        tone = "debit"
    elif months_runway is not None and months_runway < _RUNWAY_WARN_MONTHS:
        tone = "pending"

    return CfoBriefCash(
        configured=True,
        headline=headline,
        detail="; ".join(detail_bits) if detail_bits else None,
        assets_total=str(assets) if assets is not None else None,
        liabilities_total=str(liabilities),
        net_total=str(net) if net is not None else None,
        months_runway=str(months_runway) if months_runway is not None else None,
        monthly_burn=str(monthly_burn) if monthly_burn is not None else None,
        tone=tone,
    )


def _finding_score(severity: str) -> int:
    """Map a sentinel finding's severity to a ranking score.

    Critical findings outrank everything (officer-comp-missing, a blown
    deadline). High findings sit alongside decisions; lower severities fall
    below documents.
    """
    sev = (severity or "info").strip().lower()
    rank = _SEVERITY_RANK.get(sev, 10)
    # Map severity rank (10..100) onto the same scale as open-item scores so
    # they interleave sensibly: critical->95, high->75, medium->50, low->35.
    return {100: 95, 80: 75, 50: 50, 30: 35, 10: 20}.get(rank, 20)


def _decision_headline(question: str | None) -> str:
    """Strip accountant jargon prefixes from a decision question."""
    q = (question or "Needs your decision").strip()
    # The catalogue questions are already fairly plain; just ensure they read
    # as an ask rather than a label.
    return q


def _truncate(text_in: str | None, n: int = 160) -> str | None:
    if not text_in:
        return None
    t = str(text_in).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _rank_priorities(
    open_items: dict[str, Any] | None,
    findings: list[dict[str, Any]],
) -> list[CfoBriefPriority]:
    """Deterministically rank 'what needs you now' across all sources.

    Inputs are plain dicts (the open-items payload + a list of sentinel
    finding rows). Pure — no DB, no I/O — so it's directly unit-testable.

    Ordering: descending score, then a stable secondary key (kind, headline)
    so the output is deterministic for identical inputs.
    """
    out: list[CfoBriefPriority] = []

    oi = open_items or {}

    # 1. Time-sensitive open items (CRITICAL-tagged queue rows).
    for row in oi.get("time_sensitive", []) or []:
        rid = str(row.get("id") or "")
        summary = _truncate(row.get("summary"))
        out.append(
            CfoBriefPriority(
                id=f"ts:{rid}",
                kind="time_sensitive",
                severity="critical",
                headline=summary or "A time-sensitive item needs your attention",
                detail="This is flagged urgent — handle it soon.",
                action_href="/open-items",
                action_label="See what's urgent",
                score=_SCORE_TIME_SENSITIVE,
            )
        )

    # 2. Submissions awaiting follow-up — the owner must call the taxing authority.
    for row in oi.get("submissions_in_flight", []) or []:
        if (row.get("status") or "") != "awaiting_followup":
            continue
        rid = str(row.get("id") or "")
        title = row.get("submission_title") or "A tax filing"
        juris = row.get("jurisdiction") or ""
        out.append(
            CfoBriefPriority(
                id=f"sub:{rid}",
                kind="filing",
                severity="high",
                headline=f"Follow up on {title}",
                detail=_truncate(
                    f"{juris} hasn't responded — you may need to call them."
                    if juris
                    else "The taxing authority hasn't responded — you may need to call."
                ),
                action_href="/open-items",
                action_label="Open the filing",
                score=_SCORE_AWAITING_FOLLOWUP,
            )
        )

    # 3. Decisions needing the owner's OK (only the still-pending ones).
    for dec in oi.get("decisions", []) or []:
        if (dec.get("status") or "pending") != "pending":
            continue
        key = str(dec.get("key") or "")
        rec = dec.get("recommendation")
        out.append(
            CfoBriefPriority(
                id=f"dec:{key}",
                kind="decision",
                severity="medium",
                headline=_decision_headline(dec.get("question")),
                detail=_truncate(f"Suggested: {rec}" if rec else "Needs your OK."),
                action_href="/open-items#decisions",
                action_label="Give your OK",
                score=_SCORE_DECISION,
            )
        )

    # 4. Documents the owner must physically pull (only the still-pending ones).
    for doc in oi.get("documents", []) or []:
        if (doc.get("status") or "pending") != "pending":
            continue
        key = str(doc.get("key") or "")
        name = doc.get("name") or "A document"
        needed_for = doc.get("needed_for")
        # Time-sensitive docs outrank ordinary ones.
        is_ts = (doc.get("priority") or "") == "time_sensitive"
        out.append(
            CfoBriefPriority(
                id=f"doc:{key}",
                kind="document",
                severity="high" if is_ts else "low",
                headline=f"Find: {name}",
                detail=_truncate(
                    f"Needed for {needed_for}" if needed_for else None
                ),
                action_href="/open-items#documents",
                action_label="See what's needed",
                score=_SCORE_DOCUMENT + (20 if is_ts else 0),
            )
        )

    # 5. Open sentinel findings — the autonomous CPA's proactive flags.
    for f in findings or []:
        fid = str(f.get("id") or "")
        sev = (f.get("severity") or "info").strip().lower()
        out.append(
            CfoBriefPriority(
                id=f"find:{fid}",
                kind="finding",
                severity=sev,
                headline=_truncate(f.get("title")) or "Your AI CFO flagged something",
                detail=_truncate(f.get("summary")),
                action_href="/sentinel",
                action_label="Review the flag",
                score=_finding_score(sev),
            )
        )

    # Stable descending sort: score desc, then kind, then headline.
    out.sort(key=lambda p: (-p.score, p.kind, p.headline))
    # Dedupe identical headlines so one finding surfaced as several queue rows
    # (e.g. the §174 flag appearing 3×) can't flood the top of the brief — and
    # so the summary counts don't double-count. Keeps the highest-scored
    # instance, since the list is already sorted by score descending.
    seen_headlines: set[str] = set()
    deduped: list[CfoBriefPriority] = []
    for p in out:
        key = p.headline.strip().lower()
        if key in seen_headlines:
            continue
        seen_headlines.add(key)
        deduped.append(p)
    return deduped


def _summary_line(
    priorities: list[CfoBriefPriority], cash: CfoBriefCash
) -> str:
    """One plain sentence stitching the headline counts + cash together."""
    parts: list[str] = []

    n_decisions = sum(1 for p in priorities if p.kind == "decision")
    n_urgent = sum(
        1 for p in priorities if p.kind in ("time_sensitive", "filing")
    )
    n_findings_hi = sum(
        1
        for p in priorities
        if p.kind == "finding" and p.severity in ("critical", "high")
    )
    n_docs = sum(1 for p in priorities if p.kind == "document")

    if n_urgent:
        parts.append(
            f"{n_urgent} urgent thing{'s' if n_urgent != 1 else ''} need"
            f"{'' if n_urgent != 1 else 's'} attention"
        )
    if n_decisions:
        parts.append(
            f"{n_decisions} thing{'s' if n_decisions != 1 else ''} "
            f"need{'' if n_decisions != 1 else 's'} your OK"
        )
    if n_findings_hi:
        parts.append(
            f"your AI CFO flagged {n_findings_hi} item"
            f"{'s' if n_findings_hi != 1 else ''}"
        )
    if n_docs and not (n_urgent or n_decisions or n_findings_hi):
        parts.append(
            f"{n_docs} document{'s' if n_docs != 1 else ''} to gather"
        )

    if not parts:
        head = "You're all caught up — nothing needs you right now."
    else:
        # Sentence-case the joined clauses.
        joined = "; ".join(parts)
        head = joined[0].upper() + joined[1:] + "."

    if cash.configured:
        tail = cash.headline
        if cash.months_runway is not None:
            mr = _to_decimal(cash.months_runway)
            if mr is not None:
                mr_q = mr.quantize(Decimal("0.1"))
                tail += (
                    " (under a month of runway)"
                    if mr_q < Decimal("1")
                    else f" (~{mr_q.normalize()} months runway)"
                )
        return f"{head} {tail}."
    return head


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/api/workspaces/{workspace_id}/cfo-brief",
    response_model=CfoBriefResponse,
)
async def get_cfo_brief(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> CfoBriefResponse:
    """Assemble the plain-language CFO brief for the concierge home.

    Deterministic synthesis of open-items + cash-position + sentinel findings
    into a ranked top-N payload. Best-effort: each source is wrapped so a
    single failure degrades the brief rather than 500-ing the home page.
    """
    require_role(workspace, "viewer")
    # Validate entity scope up-front so an out-of-scope concrete entity 403s
    # consistently (the reused endpoints would too, but this keeps the error
    # at the brief boundary).
    assert_entity_in_scope(workspace, entity_id)

    sources_ok: dict[str, bool] = {
        "open_items": False,
        "cash": False,
        "sentinel": False,
    }

    # --- Source 1: open items (reuse the endpoint function in-process) ---
    open_items_payload: dict[str, Any] | None = None
    try:
        from app.api.open_items import get_open_items

        oi_model = await get_open_items(
            workspace_id=workspace_id,
            entity_id=entity_id,
            tax_year=None,
            workspace=workspace,
        )
        open_items_payload = oi_model.model_dump()
        sources_ok["open_items"] = True
    except Exception as exc:  # noqa: BLE001 — best-effort assembly
        _log.warning("cfo-brief: open-items source failed: %s", exc)

    # --- Source 2: cash position (reuse the endpoint function in-process) ---
    cash_payload: dict[str, Any] | None = None
    try:
        from app.api.cash_position import workspace_cash_position

        cash_payload = await workspace_cash_position(
            workspace_id=workspace_id,
            entity_id=str(entity_id) if entity_id else None,
            workspace=workspace,
        )
        sources_ok["cash"] = True
    except Exception as exc:  # noqa: BLE001 — best-effort assembly
        _log.warning("cfo-brief: cash-position source failed: %s", exc)

    # --- Source 3: open sentinel findings (direct read, scope-enforced) ---
    # has_journal_entries defaults True so we NEVER mislabel an established
    # workspace as "onboarding" on an error — onboarding guidance only shows
    # when we positively confirm zero posted entries.
    findings: list[dict[str, Any]] = []
    has_journal_entries = True
    try:
        scope = assert_entity_in_scope(workspace, entity_id)
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            where = ["f.status = 'open'"]
            params: dict[str, Any] = {}
            if entity_id:
                where.append("(f.entity_id = :eid OR f.entity_id IS NULL)")
                params["eid"] = str(entity_id)
            if scope is not None:
                where.append("(f.entity_id = ANY(:scope) OR f.entity_id IS NULL)")
                params["scope"] = [str(e) for e in scope]
            where_sql = " AND ".join(where)
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT f.id::text, f.entity_id::text, f.check_type,
                               f.severity, f.title, f.summary, f.created_at
                        FROM sentinel_finding f
                        WHERE {where_sql}
                        ORDER BY
                          CASE lower(f.severity)
                            WHEN 'critical' THEN 0
                            WHEN 'high'     THEN 1
                            WHEN 'medium'   THEN 2
                            WHEN 'low'      THEN 3
                            ELSE 4
                          END,
                          f.created_at DESC
                        LIMIT 50
                        """
                    ),
                    params,
                )
            ).mappings().all()
            findings = [dict(r) for r in rows]
            # Cheap cold-start signal (same session): any posted journal entry?
            try:
                ex = (
                    await session.execute(
                        text("SELECT EXISTS(SELECT 1 FROM journal_entry)")
                    )
                ).scalar()
                has_journal_entries = bool(ex)
            except Exception:  # noqa: BLE001 — leave default True on any failure
                pass
        sources_ok["sentinel"] = True
    except Exception as exc:  # noqa: BLE001 — table may be absent / RLS gap
        _log.warning("cfo-brief: sentinel source failed: %s", exc)

    # --- Runway: best-effort from open-items entity P&L is not available here,
    # so derive from the journal-backed runway tool when a single entity is in
    # view. For the consolidated/landing case we keep it simple: skip runway
    # (the cash headline still renders). This avoids a second heavy query on
    # the hot path. ---
    months_runway: Decimal | None = None
    monthly_burn: Decimal | None = None
    if entity_id is not None and cash_payload and cash_payload.get("configured"):
        try:
            from app.tools.reporting import get_runway

            rw = await get_runway(workspace_id, entity_id)
            mr = rw.get("months_of_runway")
            if mr is not None:
                months_runway = _to_decimal(mr)
            mb = (rw.get("inputs") or {}).get("avg_monthly_net_burn")
            if mb is not None:
                monthly_burn = _to_decimal(mb)
        except Exception as exc:  # noqa: BLE001 — runway is a nice-to-have
            _log.debug("cfo-brief: runway unavailable: %s", exc)

    # --- Synthesize ---
    cash_status = _plain_cash_status(cash_payload, months_runway, monthly_burn)
    ranked = _rank_priorities(open_items_payload, findings)
    top = ranked[:_TOP_N]

    # action_required_total = open-items' own action_required + open findings.
    oi_action = 0
    if open_items_payload:
        counts = open_items_payload.get("counts") or {}
        oi_action = int(counts.get("action_required") or 0)
    action_required_total = oi_action + len(findings)

    # Cold start: no bank connected AND no posted entries. A brand-new user who
    # hasn't started — guide them instead of saying "you're all caught up".
    onboarding = (not cash_status.configured) and (not has_journal_entries)

    if onboarding:
        summary = (
            "Welcome — let's get your books started. Connect your bank to see "
            "live transactions, or drop in a receipt or statement to begin."
        )
    else:
        summary = _summary_line(ranked, cash_status)

    return CfoBriefResponse(
        workspace_id=str(workspace_id),
        entity_id=str(entity_id) if entity_id else None,
        as_of=dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        priorities=top,
        action_required_total=action_required_total,
        cash=cash_status,
        summary_line=summary,
        onboarding=onboarding,
        ask_cfo_href="/chat?persona=cfo-agent",
        sources_ok=sources_ok,
    )
