"""Sentinel check functions.

Each check is an async coroutine ``run(ctx)`` where ``ctx`` carries the
ActionContext (session + config + check_run_id + check_type + workspace_id)
PLUS the iterable of entities Sentinel should consider.

Checks call into ``agents.sentinel.actions.execute_action(...)`` for every
finding/action they want to take, so the safety gates (autonomy level, daily
rate limit, closed-period guard, kill switch) apply uniformly.

A check function MUST be a no-op for entities outside its applicable domain
(don't, e.g., 1099-NEC check personal entities).

Return shape from a check coroutine::

    {"findings": int, "actions": int, "summary": str}

Used by the run_sentinel.py runner to fill in the sentinel_check_run row.
"""
from __future__ import annotations

from agents.sentinel.checks.approval_aging import run as run_approval_aging
from agents.sentinel.checks.cash_runway import run as run_cash_runway
from agents.sentinel.checks.coa_coverage import run as run_coa_coverage
from agents.sentinel.checks.connector_health import run as run_connector_health
from agents.sentinel.checks.corporate_formalities import run as run_corporate_formalities
from agents.sentinel.checks.filing_deadlines import run as run_filing_deadlines
from agents.sentinel.checks.nol_carryforward import run as run_nol_carryforward
from agents.sentinel.checks.officer_comp import run as run_officer_comp
from agents.sentinel.checks.pending_invitations import run as run_pending_invitations
from agents.sentinel.checks.qsbs_holding import run as run_qsbs_holding
from agents.sentinel.checks.recon_drift import run as run_recon_drift
from agents.sentinel.checks.regulatory_updates import run as run_regulatory_updates
from agents.sentinel.checks.sec_174_compliance import run as run_sec_174_compliance
from agents.sentinel.checks.sec_195_election import run as run_sec_195_election
from agents.sentinel.checks.tax_credit_scan import run as run_tax_credit_scan
from agents.sentinel.checks.uncategorized_txns import run as run_uncategorized_txns
from agents.sentinel.checks.yoy_diff import run as run_yoy_diff


CHECK_REGISTRY = {
    "filing_deadlines":      run_filing_deadlines,
    "approval_aging":        run_approval_aging,
    "recon_drift":           run_recon_drift,
    "uncategorized_txns":    run_uncategorized_txns,
    "pending_invitations":   run_pending_invitations,
    "regulatory_updates":    run_regulatory_updates,
    "tax_credit_scan":       run_tax_credit_scan,
    "sec_174_compliance":    run_sec_174_compliance,
    "sec_195_election":      run_sec_195_election,
    "officer_comp":          run_officer_comp,
    "nol_carryforward":      run_nol_carryforward,
    "qsbs_holding":          run_qsbs_holding,
    "cash_runway":           run_cash_runway,
    "yoy_diff":              run_yoy_diff,
    "connector_health":      run_connector_health,
    "coa_coverage":          run_coa_coverage,
    "corporate_formalities": run_corporate_formalities,
}


__all__ = ["CHECK_REGISTRY"]
