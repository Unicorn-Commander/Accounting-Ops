"""Execute deferred ledger purges whose retention window has elapsed.

An erasure request (POST .../data-rights/erasure) with a non-zero retention
window records a receipt whose ``ledger_purged`` step is ``deferred`` with a
``ledger_purge_due_at`` timestamp — and, until this runner existed, NOTHING
ever executed those purges (docs/DATA-RIGHTS.md known-gap #1). This script is
the sweeper that makes the retention promise real.

Run modes (mirrors scripts/drain_email_outbox.py)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* ``python scripts/purge_due_erasures.py`` — single pass, then exit. Exit code
  2 if the pass refused to run (kill switch / no admin URL), 0 otherwise.
* ``python scripts/purge_due_erasures.py --loop`` — long-running loop with a
  sleep between passes (``--interval-s`` / ``ERASURE_PURGE_INTERVAL_S``,
  default 3600s). Process entry point for the ``erasure-purger`` compose
  service (opt-in profile).

Unlike the sync drainers, this script is async and imports ``app.*`` — it
reuses the exact erasure/receipt code the API uses, so runner and request
paths can never disagree about what a receipt means.

The decision — when does a purge run?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
All three, from the durable receipt only (never operator-invented state):

  1. The receipt's ``ledger_purged`` step is still ``deferred``. ``done``
     means already executed (idempotent re-runs skip it), ``failed`` means an
     operator must look — a failed purge is NEVER auto-retried into a loop —
     and anything else is malformed and refused.
  2. ``ledger_purge_due_at`` parses and has passed (<= now, UTC).
  3. Both gates below are open.

Before purging, a previously-``unavailable`` access revocation (erasure
requested while ADMIN_DATABASE_URL was unset) is completed, so a workspace is
never left live with its ledger destroyed.

Gates (both checked every pass, refusal logged loudly)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* Kill switch: ``AO_ERASURE_PURGE_ENABLED`` must be explicitly truthy
  ("1"/"true"/"yes"/"on"). DEFAULT OFF — the runner refuses to purge anything
  until a human turns it on. Destroying customer ledgers is not a behavior a
  deployment should inherit by accident.
* ``ADMIN_DATABASE_URL`` must be configured (checked via
  ``erasure.admin_connection_available``): the runtime role cannot DELETE
  from the append-only tables, so without it a purge is structurally
  impossible and the runner refuses instead of half-succeeding.

Idempotency + crash safety
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Executing flips the receipt's ``ledger_purged`` step to the purge outcome and
stamps ``ledger_purge_executed_at``, so the next pass refuses it (condition 1).
If the process dies between the purge and the receipt write, the re-run purges
again — deleting zero rows — and then records the receipt: convergent, never
double-destructive. Concurrent instances are likewise harmless for the same
reason, though one instance is the intended deployment.

Every action is logged: pass summaries, each candidate's decision + reason,
each executed purge with row count, and each receipt update.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.data_rights import erasure as erasure_mod
from app.data_rights import retention as retention_mod
from app.db import SessionLocal

_log = logging.getLogger("acctops.erasure-purger")

ENV_ENABLED = "AO_ERASURE_PURGE_ENABLED"
ENV_INTERVAL = "ERASURE_PURGE_INTERVAL_S"
DEFAULT_INTERVAL_S = 3600

LEDGER_STEP = "ledger_purged"
ACCESS_STEP = "access_revoked"

_TRUTHY = {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Pure decision logic — unit-testable without a database.
# --------------------------------------------------------------------------- #
def purge_enabled(env: Mapping[str, str] | None = None) -> bool:
    """The kill switch. True ONLY for an explicit truthy AO_ERASURE_PURGE_ENABLED.

    Missing, empty, or anything unrecognised means OFF — destructive sweeps
    must be opted into, never inherited by default.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    return str(source.get(ENV_ENABLED, "")).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class PurgeDecision:
    """Why a workspace's deferred purge does or does not run this pass."""

    should_purge: bool
    reason: str
    attention: bool = False  # True => an operator should look (logged WARNING)
    due_at: dt.datetime | None = None


def _last_step(receipt: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """The most recent step with this name (receipts append; last wins)."""
    steps = receipt.get("steps")
    if not isinstance(steps, list):
        return None
    for step in reversed(steps):
        if isinstance(step, dict) and step.get("name") == name:
            return step
    return None


def _parse_due_at(value: Any) -> dt.datetime | None:
    """Parse ledger_purge_due_at. None for missing/unparseable; naive => UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def evaluate_receipt(receipt: Mapping[str, Any] | None, now: dt.datetime) -> PurgeDecision:
    """Decide whether a receipt's deferred ledger purge should execute at ``now``.

    Refuses everything that is not exactly: a well-formed receipt whose
    ``ledger_purged`` step is still ``deferred`` AND whose due-at has passed.
    A ``failed`` step is flagged for operator attention and never auto-retried.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    if not isinstance(receipt, Mapping) or not receipt:
        return PurgeDecision(False, "no erasure receipt for this workspace")
    ledger = _last_step(receipt, LEDGER_STEP)
    if ledger is None:
        return PurgeDecision(
            False, f"receipt has no {LEDGER_STEP} step (malformed)", attention=True
        )
    status = ledger.get("status")
    if status != erasure_mod.STATUS_DEFERRED:
        if status == erasure_mod.STATUS_FAILED:
            return PurgeDecision(
                False,
                f"{LEDGER_STEP} is '{status}' — a failed purge is never "
                "auto-retried; needs an operator",
                attention=True,
            )
        return PurgeDecision(
            False,
            f"{LEDGER_STEP} is '{status}', not "
            f"'{erasure_mod.STATUS_DEFERRED}' — nothing to execute",
        )
    due_at = _parse_due_at(receipt.get("ledger_purge_due_at"))
    if due_at is None:
        return PurgeDecision(
            False,
            "deferred but ledger_purge_due_at is missing or unparseable — "
            "needs an operator",
            attention=True,
        )
    if due_at > now:
        return PurgeDecision(
            False, f"not due until {due_at.isoformat()}", due_at=due_at
        )
    return PurgeDecision(True, f"due since {due_at.isoformat()}", due_at=due_at)


def recompute_overall_status(steps: list[dict[str, Any]]) -> str:
    """Mirror of ErasureReceipt.overall_status for stored (dict) steps."""
    statuses = {s.get("status") for s in steps if isinstance(s, dict)}
    if erasure_mod.STATUS_FAILED in statuses:
        return "partial_failed"
    if erasure_mod.STATUS_UNAVAILABLE in statuses:
        return "partial_unavailable"
    if erasure_mod.STATUS_DEFERRED in statuses:
        return "deferred"
    return "completed"


def apply_step_results(
    receipt: Mapping[str, Any],
    results: list[dict[str, Any]],
    *,
    executed_at: str,
) -> dict[str, Any]:
    """Merge executed step results into a COPY of the receipt, by step name.

    Replaces an existing step of the same name (so ``deferred`` becomes the
    purge outcome), appends any new one, recomputes the stored
    ``overall_status`` snapshot, and stamps ``ledger_purge_executed_at``.
    Pure: the input receipt is never mutated.
    """
    updated = dict(receipt)
    steps = [dict(s) for s in updated.get("steps", []) if isinstance(s, dict)]
    by_name = {s.get("name"): s for s in steps}
    for result in results:
        existing = by_name.get(result.get("name"))
        if existing is None:
            copied = dict(result)
            steps.append(copied)
            by_name[copied.get("name")] = copied
        else:
            existing.clear()
            existing.update(result)
    updated["steps"] = steps
    updated["overall_status"] = recompute_overall_status(steps)
    updated["ledger_purge_executed_at"] = executed_at
    return updated


# --------------------------------------------------------------------------- #
# Database plumbing.
# --------------------------------------------------------------------------- #
async def _candidate_workspace_ids() -> list[UUID]:
    """Workspaces whose erasure receipt carries a scheduled purge date.

    Pre-filter ONLY — the authoritative decision is ``evaluate_receipt`` on
    the freshly-read receipt. Deliberately no ``::timestamptz`` cast here: one
    malformed stored value would crash the whole query and starve every other
    workspace. Dueness is decided in Python instead.

    ``public.workspace`` is not under tenant RLS, and deliberately no
    ``status='active'`` filter: erasure suspends the workspace, which is
    exactly why the sentinel loop can never host this sweep.
    """
    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT id
                          FROM public.workspace
                         WHERE (setup_progress
                                #>> '{data_rights,erasure,ledger_purge_due_at}')
                               IS NOT NULL
                         ORDER BY id
                        """
                    )
                )
            ).scalars().all()
    out: list[UUID] = []
    for row in rows:
        try:
            out.append(row if isinstance(row, UUID) else UUID(str(row)))
        except ValueError:
            _log.warning("skipping non-UUID workspace id %r", row)
    return out


async def _write_receipt(workspace_id: UUID, receipt: dict[str, Any]) -> None:
    """Persist the updated receipt (same jsonb_set merge the API path uses)."""
    async with SessionLocal() as session:
        async with session.begin():
            doc = await retention_mod.read_data_rights_doc(session, workspace_id)
            doc.setdefault("version", retention_mod.DATA_RIGHTS_SCHEMA_VERSION)
            doc[erasure_mod.ERASURE_STATE_KEY] = receipt
            await retention_mod._write_data_rights_doc(session, workspace_id, doc)


async def run_once(*, now: dt.datetime | None = None) -> dict[str, Any]:
    """One sweep. Returns a summary; ``refused`` set when a gate was closed."""
    summary: dict[str, Any] = {"considered": 0, "purged": 0, "skipped": 0, "failed": 0}

    if not purge_enabled():
        _log.error(
            "%s is not 'true' — refusing to run; NO deferred purges will be "
            "executed. This is the deliberate default: the runner is double "
            "opt-in (compose profile AND env flag). See docs/DATA-RIGHTS.md.",
            ENV_ENABLED,
        )
        summary["refused"] = "kill_switch"
        return summary
    if not erasure_mod.admin_connection_available():
        _log.error(
            "ADMIN_DATABASE_URL is not configured — refusing to run. The "
            "runtime role cannot DELETE from the append-only tables, so the "
            "purge is structurally impossible; nothing was considered."
        )
        summary["refused"] = "no_admin_url"
        return summary

    now = now or dt.datetime.now(dt.timezone.utc)
    candidates = await _candidate_workspace_ids()
    if not candidates:
        _log.info("no workspaces carry a scheduled ledger purge; nothing to do")
        return summary
    _log.info(
        "%d workspace(s) carry a scheduled ledger purge; evaluating", len(candidates)
    )

    for wid in candidates:
        summary["considered"] += 1
        try:
            async with SessionLocal() as session:
                async with session.begin():
                    receipt = await erasure_mod.read_erasure_record(session, wid)
            decision = evaluate_receipt(receipt, now)
            if not decision.should_purge:
                summary["skipped"] += 1
                _log.log(
                    logging.WARNING if decision.attention else logging.INFO,
                    "workspace %s: skipped — %s",
                    wid,
                    decision.reason,
                )
                continue

            assert receipt is not None  # evaluate_receipt refused None above
            results: list[dict[str, Any]] = []
            access = _last_step(receipt, ACCESS_STEP)
            if (access or {}).get("status") != erasure_mod.STATUS_DONE:
                _log.info(
                    "workspace %s: completing access revocation before the purge",
                    wid,
                )
                results.extend(
                    s.to_dict() for s in await erasure_mod.revoke_access(wid)
                )

            _log.info("workspace %s: executing ledger purge (%s)", wid, decision.reason)
            purge_step = await erasure_mod.purge_workspace_ledger(wid)
            results.append(purge_step.to_dict())

            updated = apply_step_results(
                receipt,
                results,
                executed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            )
            await _write_receipt(wid, updated)

            if purge_step.status == erasure_mod.STATUS_DONE:
                summary["purged"] += 1
                _log.info(
                    "workspace %s: ledger purge DONE (%s row(s)); receipt updated "
                    "(overall_status=%s)",
                    wid,
                    purge_step.count,
                    updated["overall_status"],
                )
            else:
                summary["failed"] += 1
                _log.error(
                    "workspace %s: ledger purge reported '%s' — %s Receipt "
                    "updated; it will NOT be retried automatically (operator case).",
                    wid,
                    purge_step.status,
                    purge_step.detail,
                )
        except Exception:  # noqa: BLE001 — one bad workspace must not starve the rest
            summary["failed"] += 1
            _log.exception(
                "workspace %s: purge pass crashed; receipt untouched, so it "
                "stays deferred and is retried next pass",
                wid,
            )
    return summary


# --------------------------------------------------------------------------- #
# Entry points — mirrors the mail-drainer loop shape.
# --------------------------------------------------------------------------- #
def _run_loop(interval_s: int) -> None:
    _log.info(
        "erasure purger loop started interval_s=%s enabled=%s admin_url_configured=%s",
        interval_s,
        purge_enabled(),
        erasure_mod.admin_connection_available(),
    )
    while True:
        try:
            summary = asyncio.run(run_once())
            if summary.get("considered") or summary.get("refused"):
                _log.info("pass complete %s", summary)
        except KeyboardInterrupt:
            _log.info("erasure purger interrupted; exiting cleanly")
            return
        except Exception:  # noqa: BLE001 — never die silently
            _log.exception("purge pass crashed; will retry next interval")
        time.sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute deferred data-rights ledger purges that are due."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run forever; sweep every --interval-s seconds.",
    )
    parser.add_argument(
        "--interval-s",
        type=int,
        default=int(os.getenv(ENV_INTERVAL, str(DEFAULT_INTERVAL_S))),
        help=f"Loop sleep interval (only with --loop). Default {DEFAULT_INTERVAL_S}s.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable DEBUG logging."
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.loop:
        _run_loop(args.interval_s)
        return 0

    summary = asyncio.run(run_once())
    _log.info("single-pass complete %s", summary)
    # A refused single pass exits non-zero so ad-hoc/cron callers cannot
    # mistake "did nothing because gated" for "swept and found nothing".
    return 2 if summary.get("refused") else 0


if __name__ == "__main__":
    sys.exit(main())
