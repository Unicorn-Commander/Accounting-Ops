"""``/.well-known/mcps.json`` — Brigade MCP federation discovery manifest.

Brigade (https://brigade.unicorncommander.ai/mcp) aggregates tools from many UC
apps under a single namespaced MCP endpoint. To register Accounting-Ops with
Brigade, this manifest at the well-known path describes:

  * Where our MCP server lives (``mcp_endpoint``)
  * What namespace our tools live under in Brigade (``tool_namespace``)
  * How to authenticate — both our native PAT and the Brigade-signed JWT
  * The full tool catalogue with per-tool scopes / tier tags / latency hints

The tool list is generated PROGRAMMATICALLY from the FastMCP server's live
registrations. Add a tool in ``mcp_server.py`` and it appears here on the next
request — no manual sync. Per-tool metadata (min_role / read_only / latency)
comes from the small registry in this module; adding a new tool requires one
line in ``_TOOL_METADATA``.

NO authentication is required to fetch the manifest — it's a discovery
document. It contains zero customer data: only tool shapes, role floors, and
issuer URLs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

from app.config import get_settings

_log = logging.getLogger("acctops.discovery")

router = APIRouter(tags=["discovery"])


# ---------------------------------------------------------------------------
# Per-tool metadata registry
# ---------------------------------------------------------------------------
# The MCP tool surface itself declares min_role via the authorize_workspace
# call inside each tool body — there's no machine-readable "min_role" property
# on the FastMCP registration. This registry mirrors what each tool requires
# so we can stamp it into the manifest. Keep in sync with the tools.
#
# Fields per tool:
#   min_role     — viewer | accountant | admin (matches authorize_workspace)
#   read_only    — true for pure-read tools (no DB writes, no side effects)
#   latency_ms   — observed/expected p50 latency. Used by Brigade for routing.
#
# Tools missing from this registry get conservative defaults (admin / write /
# 800ms) and a warning log line, so a new MCP tool can't silently leak as
# "free / read_only" to the manifest.

_DEFAULT_META = {
    "min_role": "admin",
    "read_only": False,
    "latency_ms": 800,
}

_TOOL_METADATA: dict[str, dict[str, Any]] = {
    # ----- Discovery / workspace enumeration (viewer floor, read-only) ------
    "list_my_workspaces":            {"min_role": "viewer",     "read_only": True,  "latency_ms": 200},
    "list_workspace_entities":       {"min_role": "viewer",     "read_only": True,  "latency_ms": 200},

    # ----- Approval queue ---------------------------------------------------
    # analyze_pending_approvals writes recommendations rows so it is NOT read-only.
    "analyze_pending_approvals":     {"min_role": "accountant", "read_only": False, "latency_ms": 1200},
    "list_pending_approvals":        {"min_role": "viewer",     "read_only": True,  "latency_ms": 300},
    "list_approval_recommendations": {"min_role": "viewer",     "read_only": True,  "latency_ms": 300},
    "decide_approval":               {"min_role": "admin",      "read_only": False, "latency_ms": 800},
    "bulk_decide_approvals":         {"min_role": "admin",      "read_only": False, "latency_ms": 2000},

    # ----- Entity-scoped reads ---------------------------------------------
    "list_transactions":             {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},
    "get_transaction":               {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},
    "list_recent_transactions":      {"min_role": "viewer",     "read_only": True,  "latency_ms": 300},
    "get_cash_position":             {"min_role": "viewer",     "read_only": True,  "latency_ms": 350},
    "query_specialty_rules":         {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},

    # ----- Entity-scoped writes --------------------------------------------
    "categorize_transaction":        {"min_role": "accountant", "read_only": False, "latency_ms": 800},
    "link_receipt_to_transaction":   {"min_role": "accountant", "read_only": False, "latency_ms": 600},
    # add_specialty_rule writes a specialty_rule row → admin floor.
    "add_specialty_rule":            {"min_role": "admin",      "read_only": False, "latency_ms": 800},

    # ----- Reports (entity or consolidated) --------------------------------
    "get_p_and_l":                   {"min_role": "viewer",     "read_only": True,  "latency_ms": 400},
    "get_runway":                    {"min_role": "viewer",     "read_only": True,  "latency_ms": 400},
    # Consolidated cross-entity views require unrestricted entity_scope ⇒ admin.
    "get_consolidated_p_and_l":      {"min_role": "admin",      "read_only": True,  "latency_ms": 600},
    "get_balance_sheet":             {"min_role": "viewer",     "read_only": True,  "latency_ms": 400},
    "get_cash_flow":                 {"min_role": "viewer",     "read_only": True,  "latency_ms": 400},
    "get_trial_balance":             {"min_role": "viewer",     "read_only": True,  "latency_ms": 400},
    "get_1099_nec_report":           {"min_role": "viewer",     "read_only": True,  "latency_ms": 500},

    # ----- Chart of accounts (entity-scoped) -------------------------------
    "list_chart_of_accounts":        {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},
    "create_coa_account":            {"min_role": "accountant", "read_only": False, "latency_ms": 600},
    "update_coa_account":            {"min_role": "accountant", "read_only": False, "latency_ms": 600},

    # ----- Setup Assistant — provisioning ----------------------------------
    "create_entity_with_coa":        {"min_role": "accountant", "read_only": False, "latency_ms": 1500},
    # create_workspace runs alembic upgrade + DDL — owner+ enforced inline.
    "create_workspace":              {"min_role": "owner",      "read_only": False, "latency_ms": 5000},
    "suggest_connectors":            {"min_role": "viewer",     "read_only": True,  "latency_ms": 200},
    "mark_intake_complete":          {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},

    # ----- Journal + period close ------------------------------------------
    "list_journal_entries":          {"min_role": "viewer",     "read_only": True,  "latency_ms": 350},
    "get_journal_entry":             {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},
    "verify_journal_chain":          {"min_role": "viewer",     "read_only": True,  "latency_ms": 600},
    "list_posting_log":              {"min_role": "viewer",     "read_only": True,  "latency_ms": 400},
    "post_journal_entry":            {"min_role": "accountant", "read_only": False, "latency_ms": 800},
    "list_closed_periods":           {"min_role": "viewer",     "read_only": True,  "latency_ms": 200},
    "close_period":                  {"min_role": "admin",      "read_only": False, "latency_ms": 800},

    # ----- Connectors / mappings / overrides / credentials -----------------
    "list_credentials":              {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},
    "list_account_mappings":         {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},
    "upsert_account_mappings":       {"min_role": "accountant", "read_only": False, "latency_ms": 600},
    "list_category_overrides":       {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},
    "create_category_override":      {"min_role": "accountant", "read_only": False, "latency_ms": 600},

    # ----- Tax Knowledge RAG (global, no workspace scoping) ----------------
    # Read-only, public-data tools. min_role left at viewer for the manifest.
    "get_current_rule":              {"min_role": "viewer",     "read_only": True,  "latency_ms": 350},
    "list_tax_knowledge_topics":     {"min_role": "viewer",     "read_only": True,  "latency_ms": 300},
    "search_tax_knowledge":          {"min_role": "viewer",     "read_only": True,  "latency_ms": 500},
    "get_tax_knowledge_doc":         {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},

    # ----- 1120 Preparer + tax review queue --------------------------------
    "get_tax_review_queue":          {"min_role": "viewer",     "read_only": True,  "latency_ms": 350},
    "resolve_tax_review_item":       {"min_role": "accountant", "read_only": False, "latency_ms": 600},
    "get_form_1120_data":            {"min_role": "viewer",     "read_only": True,  "latency_ms": 800},
    "get_period_journal":            {"min_role": "viewer",     "read_only": True,  "latency_ms": 400},
    "get_1099_payees":               {"min_role": "viewer",     "read_only": True,  "latency_ms": 400},
    # draft_memo and flag_for_cpa_review write tax_review_queue / memo rows.
    "draft_memo":                    {"min_role": "accountant", "read_only": False, "latency_ms": 800},
    "flag_for_cpa_review":           {"min_role": "accountant", "read_only": False, "latency_ms": 600},

    # ----- CPA Narrative + tax status dashboard ----------------------------
    "get_tax_narrative":             {"min_role": "viewer",     "read_only": True,  "latency_ms": 350},
    "trigger_tax_narrative":         {"min_role": "accountant", "read_only": False, "latency_ms": 2000},
    "get_tax_status":                {"min_role": "viewer",     "read_only": True,  "latency_ms": 500},

    # ----- Specialist run observability ------------------------------------
    "list_specialist_runs":          {"min_role": "viewer",     "read_only": True,  "latency_ms": 300},
    "get_specialist_run":            {"min_role": "viewer",     "read_only": True,  "latency_ms": 250},
    "start_specialist_run":          {"min_role": "accountant", "read_only": False, "latency_ms": 800},
    "finish_specialist_run":         {"min_role": "accountant", "read_only": False, "latency_ms": 600},

    # ----- propose_payment (Phase-1 stub; admin floor for safety) ----------
    # The MCP server wraps propose_payment so its registered name is
    # propose_payment_tool. We track BOTH so any rename is forgiving.
    "propose_payment":               {"min_role": "admin",      "read_only": False, "latency_ms": 800},
    "propose_payment_tool":          {"min_role": "admin",      "read_only": False, "latency_ms": 800},
}


# min_role -> required scope mapping. Mirrored across the manifest and the
# runtime — these names are what Brigade carries in the JWT's ``scope`` claim
# when it federates a UC user into the call.
#
# Multi-persona expansion (2026-05-30) added several roles between viewer and
# owner; see app/tenancy.py ROLE_ORDER. For Brigade-facing scope mapping we
# collapse them to the same three buckets (read / write / admin) since Brigade
# only knows about those three scope strings today.
_SCOPE_FOR_ROLE = {
    "viewer":         ["read:accounting"],
    "client_user":    ["read:accounting"],
    "bookkeeper":     ["write:accounting"],
    "accountant":     ["write:accounting"],
    "cpa":            ["write:accounting"],
    "admin":          ["admin:accounting"],
    "owner":          ["admin:accounting"],
    "client_owner":   ["admin:accounting"],
    "platform_admin": ["admin:accounting"],
}


# tier mapping — used by Brigade to gate by customer tier.
def _tier_for(min_role: str, read_only: bool) -> str:
    """Tier mapping policy:

      free       — read tools with viewer floor (the basic look-around surface).
      pro        — write tools with accountant floor (the working surface).
      enterprise — admin-floor tools (cross-entity, rule changes, etc.).
    """
    if min_role == "admin" or min_role == "owner":
        return "enterprise"
    if not read_only:
        return "pro"
    return "free"


# ---------------------------------------------------------------------------
# FastMCP server access — single shared instance for the lifetime of the process.
# Building the server is cheap but not free; cache the manifest payload too.
# ---------------------------------------------------------------------------


_manifest_cache: dict[str, Any] | None = None
_manifest_lock = asyncio.Lock()


async def _build_manifest() -> dict[str, Any]:
    """Construct the full manifest from the live MCP server registrations.

    Imported lazily to avoid pulling MCP server build-time work into module
    import (keeps test imports fast and matches main.py's lazy build).
    """
    from mcp_server import build_server

    settings = get_settings()
    server = build_server()
    tools = await server.list_tools()

    tools_out: list[dict[str, Any]] = []
    for tool in tools:
        meta = _TOOL_METADATA.get(tool.name)
        if meta is None:
            _log.warning(
                "discovery manifest: no metadata registered for tool %r — "
                "defaulting to %r. Add a row to _TOOL_METADATA.",
                tool.name,
                _DEFAULT_META,
            )
            meta = _DEFAULT_META
        min_role = str(meta.get("min_role", _DEFAULT_META["min_role"]))
        read_only = bool(meta.get("read_only", _DEFAULT_META["read_only"]))
        latency = int(meta.get("latency_ms", _DEFAULT_META["latency_ms"]))
        tools_out.append({
            "name": tool.name,
            "description": (tool.description or "").strip(),
            "input_schema": tool.inputSchema or {"type": "object", "properties": {}},
            "scopes_required": list(_SCOPE_FOR_ROLE.get(min_role, ["admin:accounting"])),
            "tier_required": _tier_for(min_role, read_only),
            "read_only": read_only,
            "expected_latency_ms": latency,
            "min_role": min_role,
        })

    # Stable order so the manifest is diff-friendly across deploys.
    tools_out.sort(key=lambda t: t["name"])

    base = settings.public_base_url.rstrip("/")
    return {
        "$schema": "https://brigade.unicorncommander.ai/.well-known/mcps-schema-v1.json",
        "name": "Accounting-Ops",
        "version": "0.1.0",
        "mcp_endpoint": f"{base}/mcp",
        "tool_namespace": "account",
        "auth": {
            "type": "pat",
            "prefix": "aops_pat_",
            "mint_url": f"{base}/settings/pats",
            "federated_auth": {
                "type": "brigade_jwt",
                "trusted_issuer": "https://brigade.unicorncommander.ai",
                "jwks_url": "https://brigade.unicorncommander.ai/.well-known/jwks.json",
                "expected_audience": "accounting-ops",
                "signing_algorithm": "ed25519",
                # Brigade may also sign RS256 — we accept either. Listed
                # explicitly so Brigade can pick whichever is convenient.
                "accepted_algorithms": ["EdDSA", "RS256"],
            },
        },
        "tools": tools_out,
        "resources": [],
        "prompts": [],
        "rate_limits": {
            "per_user_per_minute": 60,
            "per_user_per_hour": 1000,
        },
        "contact": "support@magicunicorn.dev",
    }


async def _cached_manifest() -> dict[str, Any]:
    """Return the manifest, building once per process.

    The manifest is fully derived from compile-time data (tool registrations +
    static metadata), so a single build per process is correct. If Brigade
    needs runtime updates, this can become a TTL cache later.
    """
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache
    async with _manifest_lock:
        if _manifest_cache is None:
            _manifest_cache = await _build_manifest()
    return _manifest_cache


def _reset_cache_for_tests() -> None:
    """Test helper: discard the cached manifest so the next request rebuilds."""
    global _manifest_cache
    _manifest_cache = None


@router.get("/.well-known/mcps.json", include_in_schema=False)
async def well_known_mcps() -> dict[str, Any]:
    """Serve the Brigade MCP federation discovery manifest.

    NO auth required — discovery is public. The manifest contains zero customer
    data: it's tool shapes + issuer URLs + scope names.
    """
    return await _cached_manifest()


__all__ = ("router",)
