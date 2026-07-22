"""Sentinel autonomy + per-workspace config helpers.

Autonomy levels (ordered from least to most powerful):

  ``off``                Sentinel disabled for this workspace.
  ``watch_only``         Runs checks; writes only ``sentinel_finding`` rows.
                         No memos, no flags, no emails, no scheduled actions.
  ``draft_and_flag``     RECOMMENDED DEFAULT. Can draft memos, flag items for
                         CPA review, send invitations + informational emails,
                         schedule reminders, create open-items entries, and
                         run specialist agents on demand. Cannot auto-decide
                         approvals, run connector syncs, or set up entities.
  ``fully_autonomous``   Adds: bulk-decide low-risk approvals, initiate
                         connector syncs, set up new entities. Use only for
                         workspaces that have explicitly opted-in to full
                         automation.

Action types each level may take are enumerated in ``actions.ALLOWED_BY_LEVEL``.
"""
from __future__ import annotations

import datetime as dt
import enum
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("acctops.sentinel.config")


class AutonomyLevel(str, enum.Enum):
    OFF = "off"
    WATCH_ONLY = "watch_only"
    DRAFT_AND_FLAG = "draft_and_flag"
    FULLY_AUTONOMOUS = "fully_autonomous"


_RANK = {
    AutonomyLevel.OFF: 0,
    AutonomyLevel.WATCH_ONLY: 1,
    AutonomyLevel.DRAFT_AND_FLAG: 2,
    AutonomyLevel.FULLY_AUTONOMOUS: 3,
}


def at_least(level: AutonomyLevel, minimum: AutonomyLevel) -> bool:
    """True if ``level`` has at least ``minimum`` capability."""
    return _RANK[level] >= _RANK[minimum]


# The full vocabulary of check_types the Sentinel can run. The
# ``enabled_checks`` JSONB column on ``sentinel_config`` filters this list.
ALL_CHECK_TYPES: tuple[str, ...] = (
    # Daily
    "filing_deadlines",
    "approval_aging",
    "recon_drift",
    "uncategorized_txns",
    "pending_invitations",
    # Weekly
    "regulatory_updates",
    "tax_credit_scan",
    "sec_174_compliance",
    "sec_195_election",
    "officer_comp",
    "nol_carryforward",
    "qsbs_holding",
    "subscription_monitor",
    # Monthly
    "cash_runway",
    "yoy_diff",
    "connector_health",
    "coa_coverage",
    "corporate_formalities",
)


CHECK_CADENCE: dict[str, str] = {
    "filing_deadlines":      "daily",
    "approval_aging":        "daily",
    "recon_drift":           "daily",
    "uncategorized_txns":    "daily",
    "pending_invitations":   "daily",
    "regulatory_updates":    "weekly",
    "tax_credit_scan":       "weekly",
    "sec_174_compliance":    "weekly",
    "sec_195_election":      "weekly",
    "officer_comp":          "weekly",
    "nol_carryforward":      "weekly",
    "qsbs_holding":          "weekly",
    "subscription_monitor":  "weekly",
    "cash_runway":           "monthly",
    "yoy_diff":              "monthly",
    "connector_health":      "monthly",
    "coa_coverage":          "monthly",
    "corporate_formalities": "monthly",
}


@dataclass(frozen=True)
class SentinelConfig:
    workspace_id: UUID
    autonomy_level: AutonomyLevel
    check_interval_minutes: int
    max_actions_per_day: int
    enabled_checks: tuple[str, ...]  # the resolved list (after expanding "all")
    notify_owners: bool
    notify_accountants: bool
    # When this workspace's last Sentinel cycle ran. ``None`` == never, which
    # the workspace-level cadence gate treats as DUE (first-cycle rule). Read
    # by agents/sentinel/cadence.is_workspace_due so ``check_interval_minutes``
    # is actually ENFORCED rather than merely displayed in the settings UI.
    last_run_at: dt.datetime | None = None

    def is_enabled(self, check_type: str) -> bool:
        return check_type in self.enabled_checks


def _expand_enabled(enabled: Any) -> tuple[str, ...]:
    """Normalize ``enabled_checks`` JSONB to a tuple of check_type slugs.

    ``["all"]`` (or empty / None) -> the full vocabulary. Otherwise return
    the intersection of the configured list with the vocabulary (drops typos).
    """
    if not enabled:
        return ALL_CHECK_TYPES
    if not isinstance(enabled, list):
        return ALL_CHECK_TYPES
    if any(str(x).strip().lower() == "all" for x in enabled):
        return ALL_CHECK_TYPES
    requested = {str(x).strip().lower() for x in enabled}
    return tuple(c for c in ALL_CHECK_TYPES if c in requested)


async def get_or_create_config(
    session: AsyncSession, workspace_id: UUID
) -> SentinelConfig:
    """Fetch the sentinel_config row for the workspace; insert defaults if absent.

    Runs inside the caller's existing transaction. Workspace search_path + RLS
    GUC must already be set (use ``workspace_session(...)``).
    """
    row = (
        await session.execute(
            text(
                """
                SELECT autonomy_level, check_interval_minutes,
                       max_actions_per_day, enabled_checks,
                       notify_owners, notify_accountants, last_run_at
                FROM sentinel_config
                WHERE workspace_id = :wid
                """
            ),
            {"wid": str(workspace_id)},
        )
    ).mappings().first()

    if row is None:
        await session.execute(
            text(
                """
                INSERT INTO sentinel_config (
                    workspace_id, autonomy_level, check_interval_minutes,
                    max_actions_per_day, enabled_checks,
                    notify_owners, notify_accountants
                ) VALUES (
                    :wid, 'draft_and_flag', 360, 50,
                    '["all"]'::jsonb, true, true
                )
                ON CONFLICT (workspace_id) DO NOTHING
                """
            ),
            {"wid": str(workspace_id)},
        )
        return SentinelConfig(
            workspace_id=workspace_id,
            autonomy_level=AutonomyLevel.DRAFT_AND_FLAG,
            check_interval_minutes=360,
            max_actions_per_day=50,
            enabled_checks=ALL_CHECK_TYPES,
            notify_owners=True,
            notify_accountants=True,
            # Brand-new config row: never run. The workspace gate treats this
            # as due, so a freshly provisioned workspace runs its first cycle
            # immediately instead of waiting out check_interval_minutes.
            last_run_at=None,
        )

    raw_level = (row["autonomy_level"] or "draft_and_flag").strip().lower()
    try:
        level = AutonomyLevel(raw_level)
    except ValueError:
        _log.warning(
            "Unknown autonomy_level %r in workspace %s; falling back to draft_and_flag",
            raw_level, workspace_id,
        )
        level = AutonomyLevel.DRAFT_AND_FLAG

    return SentinelConfig(
        workspace_id=workspace_id,
        autonomy_level=level,
        check_interval_minutes=int(row["check_interval_minutes"] or 360),
        max_actions_per_day=int(row["max_actions_per_day"] or 50),
        enabled_checks=_expand_enabled(row["enabled_checks"]),
        notify_owners=bool(row["notify_owners"]),
        notify_accountants=bool(row["notify_accountants"]),
        last_run_at=row["last_run_at"],
    )


async def update_last_run(session: AsyncSession, workspace_id: UUID) -> None:
    """Stamp ``last_run_at = now()`` for the workspace's config row."""
    await session.execute(
        text(
            """
            UPDATE sentinel_config
            SET last_run_at = now(), updated_at = now()
            WHERE workspace_id = :wid
            """
        ),
        {"wid": str(workspace_id)},
    )


# ---------------------------------------------------------------------------
# Per-check cadence state (tenant table sentinel_check_state, migration 0027)
#
# These two helpers are the durable half of cadence enforcement. Without a
# persisted last-run-per-(workspace, check), every restart of the sentinel
# container would re-fire every monthly check — silently reintroducing the
# "monthly check runs 4x a day" defect this fixes.
# ---------------------------------------------------------------------------


async def load_check_state(
    session: AsyncSession, workspace_id: UUID
) -> dict[str, dt.datetime | None]:
    """Map ``check_type -> last_run_at`` for one workspace.

    A check_type ABSENT from the returned dict has never run, and the cadence
    gate treats that as due. Degrades to ``{}`` (== run everything) if the
    table is missing, so a deploy that lands the code before migration 0027 is
    a no-op rather than a crash — the pre-fix behaviour, not an outage.
    """
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT check_type, last_run_at
                    FROM sentinel_check_state
                    WHERE workspace_id = :wid
                    """
                ),
                {"wid": str(workspace_id)},
            )
        ).mappings().all()
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "sentinel_check_state unreadable for %s (%s); running all enabled "
            "checks this cycle (pre-0027 fallback)",
            workspace_id, e,
        )
        return {}
    return {r["check_type"]: r["last_run_at"] for r in rows}


async def record_check_run(
    session: AsyncSession,
    workspace_id: UUID,
    check_type: str,
    *,
    status: str = "completed",
    cycle_id: UUID | None = None,
    findings: int = 0,
) -> None:
    """Upsert the per-check cadence stamp after a run.

    Called for BOTH success and failure: a crashing check still counts as
    "ran", so a persistently broken check stays on its cadence instead of
    retrying at full loop frequency. ``consecutive_failures`` carries the
    "this check is broken" signal instead.
    """
    ok = status == "completed"
    await session.execute(
        text(
            """
            INSERT INTO sentinel_check_state (
                workspace_id, check_type, last_run_at, last_status,
                last_cycle_id, last_findings_count, consecutive_failures,
                updated_at
            ) VALUES (
                :wid, :ct, now(), :st, :cyc, :findings, :init_fail, now()
            )
            ON CONFLICT (workspace_id, check_type) DO UPDATE SET
                last_run_at = now(),
                last_status = EXCLUDED.last_status,
                last_cycle_id = EXCLUDED.last_cycle_id,
                last_findings_count = EXCLUDED.last_findings_count,
                consecutive_failures = CASE
                    WHEN EXCLUDED.last_status = 'completed' THEN 0
                    ELSE sentinel_check_state.consecutive_failures + 1
                END,
                updated_at = now()
            """
        ),
        {
            "wid": str(workspace_id),
            "ct": check_type,
            "st": "completed" if ok else "failed",
            "cyc": str(cycle_id) if cycle_id else None,
            "findings": int(findings or 0),
            "init_fail": 0 if ok else 1,
        },
    )
