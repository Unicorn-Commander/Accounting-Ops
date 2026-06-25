"""Grant a workspace COMPED (free) inference access — for friends / dogfooders.

Two comp modes, plus an org-bridge backfill. All are operator (admin) actions
run from a box that can reach the DB + the UC gateway; nothing here is exposed to
end users (the self-service equivalent is the BYOK settings page —
``web/app/settings/inference`` → ``app/api/inference_settings.py``).

  byok    Store a workspace's OWN provider key (Anthropic / OpenRouter). The
          inference resolver gives BYOK the highest precedence, so the workspace
          runs entirely on the customer's key — **$0 to us** and not metered.
          This is the preferred comp: full access, their key, no cost to UC.

  credit  Mint a BUDGET-CAPPED UC-gateway virtual key and store it as the
          workspace's platform key. Usage meters to the UC org via Lago and the
          gateway hard-stops once ``--budget`` (upstream USD) is spent — a comped
          credit grant with a built-in loss-stop. The fallback for a friend who
          won't manage their own key.

  org     Backfill ``public.workspace.uc_org_id`` only (the UC org a workspace
          meters under). Also accepted as ``--uc-org-id`` on the other modes.

Storage contracts (kept in lockstep with the resolver — do NOT change without
changing the named modules):
  - BYOK  → connector ``byo_llm_key``, label = provider, value = JSON
            ``{"key","provider","base_url"}``  (app/security/workspace_llm_keys.get_byok)
  - credit→ connector ``litellm_virtual_key``, label ``platform``, value = the
            ``sk-…`` key  (app/security/workspace_llm_keys.get_or_mint_virtual_key)

Security: a key is NEVER printed in full or logged — only its last 4 chars.

Examples:
    # Preferred: Shafen brings his own Anthropic key (free to us)
    python -m scripts.grant_comped_access byok \\
        --workspace 1ab938df-43f2-567a-8566-800f8a14a466 \\
        --provider anthropic --key sk-ant-... --uc-org-id org_<uuid>

    # Fallback: comp him a $25 credit grant on UC's gateway
    python -m scripts.grant_comped_access credit \\
        --workspace 1ab938df-43f2-567a-8566-800f8a14a466 \\
        --budget 25 --uc-org-id org_<uuid>

    # Just bridge a workspace to its UC org
    python -m scripts.grant_comped_access org \\
        --workspace <uuid> --uc-org-id org_<uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_admin_database_url  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.security.credentials import store_credential  # noqa: E402

# The resolver's exact connector constants. Imported (not re-literalled) so this
# script can never drift from what app/security/workspace_llm_keys.py reads.
from app.security.workspace_llm_keys import (  # noqa: E402
    CONNECTOR_BYOK,
    CONNECTOR_VIRTUAL,
)

# Label for the platform virtual-key credential. Must stay in lockstep with
# workspace_llm_keys._VIRTUAL_LABEL (kept as a local literal rather than
# importing a private name).
_VIRTUAL_LABEL = "platform"

ALLOWED_PROVIDERS = ("anthropic", "openrouter")


def _last4(secret: str) -> str:
    s = secret or ""
    return s[-4:] if len(s) >= 4 else s


@lru_cache
def _admin_engine() -> AsyncEngine:
    # public.workspace writes are an owner/admin operation (mirrors
    # scripts/provision_workspace.py), so bind to the table-owning role.
    return create_async_engine(get_admin_database_url(), pool_pre_ping=True)


async def _workspace_exists(workspace_id: uuid.UUID) -> bool:
    async with _admin_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT 1 FROM public.workspace WHERE id = :wid"),
                {"wid": str(workspace_id)},
            )
        ).first()
    return row is not None


async def _current_uc_org_id(workspace_id: uuid.UUID) -> str | None:
    async with _admin_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT uc_org_id FROM public.workspace WHERE id = :wid"),
                {"wid": str(workspace_id)},
            )
        ).first()
    return str(row[0]) if row and row[0] else None


async def backfill_uc_org_id(workspace_id: uuid.UUID, uc_org_id: str) -> None:
    """Set public.workspace.uc_org_id — the org a workspace meters under."""
    async with _admin_engine().begin() as conn:
        result = await conn.execute(
            text("UPDATE public.workspace SET uc_org_id = :org WHERE id = :wid"),
            {"org": uc_org_id, "wid": str(workspace_id)},
        )
        if result.rowcount == 0:
            raise SystemExit(f"ERROR: no workspace row for {workspace_id}")
    print(f"  uc_org_id  : {uc_org_id}  (workspace.uc_org_id backfilled)")


async def store_byok(
    workspace_id: uuid.UUID, provider: str, api_key: str, base_url: str | None
) -> None:
    """Store a workspace's own provider key in the resolver's BYOK shape."""
    value = json.dumps({"key": api_key, "provider": provider, "base_url": base_url})
    async with SessionLocal() as session:
        await store_credential(
            session, workspace_id, CONNECTOR_BYOK, provider, value, created_by_uc_uid=None
        )
        await session.commit()
    print(
        f"  BYOK stored: provider={provider} key=…{_last4(api_key)}"
        + (f" base_url={base_url}" if base_url else "")
    )
    print("  cost model : $0 to us — runs on the customer's own key (not metered).")


async def grant_credit(
    workspace_id: uuid.UUID,
    budget: float,
    org_id: str | None,
    budget_duration: str | None = None,
) -> None:
    """Mint a budget-capped gateway virtual key + store it as the platform key.

    ``budget_duration`` (e.g. "30d") makes the budget RESET each period — a
    recurring monthly comp; omit it for a one-time cumulative cap. Imports the
    gateway admin client lazily so the byok/org modes never require a configured
    LLM_GATEWAY_ADMIN_KEY.
    """
    from app.security.litellm_admin import (
        DEFAULT_ALLOWED_MODELS,
        LiteLLMAdminError,
        mint_virtual_key,
    )

    customer_id = org_id or await _current_uc_org_id(workspace_id)
    if not customer_id:
        raise SystemExit(
            "ERROR: credit grants meter to a UC org. Pass --org-id or first set "
            "the bridge with --uc-org-id (so usage doesn't orphan in Lago)."
        )

    try:
        key = await mint_virtual_key(
            customer_id=customer_id,
            max_budget=budget,
            budget_duration=budget_duration,
            models=DEFAULT_ALLOWED_MODELS,
            metadata={
                "workspace_id": str(workspace_id),
                "org_id": customer_id,
                "source": "accounting-ops",
                "comp": "true",
            },
        )
    except LiteLLMAdminError as e:
        # Status/message only — mint_virtual_key never puts key material in the error.
        raise SystemExit(f"ERROR: gateway mint failed: {e}")

    async with SessionLocal() as session:
        await store_credential(
            session,
            workspace_id,
            CONNECTOR_VIRTUAL,
            _VIRTUAL_LABEL,
            key,
            created_by_uc_uid=None,
        )
        await session.commit()
    period = f"/{budget_duration}" if budget_duration else " (one-time)"
    print(
        f"  credit key : minted …{_last4(key)} budget=${budget:g}{period} "
        f"customer={customer_id}"
    )
    print(
        "  cost model : metered to the UC org via Lago; gateway hard-stops at the "
        "budget (can't overspend)."
    )


async def _run(args: argparse.Namespace) -> None:
    workspace_id = uuid.UUID(args.workspace)

    if not await _workspace_exists(workspace_id):
        raise SystemExit(
            f"ERROR: workspace {workspace_id} not found. Provision it first "
            "(python -m scripts.provision_workspace ...)."
        )

    print(f"comping workspace {workspace_id} (mode={args.mode})")
    if args.dry_run:
        print("  DRY RUN — no writes, no minting.")

    # --uc-org-id (or the `org` mode's required value) backfills the bridge first
    # so a same-invocation credit grant resolves the customer.
    uc_org_id = getattr(args, "uc_org_id", None)
    if args.mode == "org":
        uc_org_id = args.uc_org_id
    if uc_org_id:
        if args.dry_run:
            print(f"  would set workspace.uc_org_id = {uc_org_id}")
        else:
            await backfill_uc_org_id(workspace_id, uc_org_id)

    if args.mode == "byok":
        provider = args.provider.strip().lower()
        if provider not in ALLOWED_PROVIDERS:
            raise SystemExit(
                f"ERROR: unsupported provider '{args.provider}'. "
                f"Choose one of: {', '.join(ALLOWED_PROVIDERS)}."
            )
        api_key = args.key.strip()
        if len(api_key) < 8:
            raise SystemExit("ERROR: --key looks too short to be a real key.")
        base_url = (args.base_url or "").strip() or None
        if args.dry_run:
            print(
                f"  would store BYOK: provider={provider} key=…{_last4(api_key)}"
                + (f" base_url={base_url}" if base_url else "")
            )
        else:
            await store_byok(workspace_id, provider, api_key, base_url)

    elif args.mode == "credit":
        if args.budget <= 0:
            raise SystemExit("ERROR: --budget must be a positive USD amount.")
        period = f"/{args.budget_duration}" if args.budget_duration else " (one-time)"
        if args.dry_run:
            cust = args.org_id or await _current_uc_org_id(workspace_id) or "(unresolved)"
            print(
                f"  would mint a ${args.budget:g}{period} budget-capped key for "
                f"customer {cust} and store it as the platform key."
            )
        else:
            await grant_credit(
                workspace_id, args.budget, args.org_id, args.budget_duration
            )

    print("done.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grant a workspace comped (free) inference access."
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_byok = sub.add_parser("byok", help="Store the workspace's own provider key ($0 to us).")
    p_byok.add_argument("--workspace", required=True, help="Workspace UUID")
    p_byok.add_argument("--provider", required=True, choices=ALLOWED_PROVIDERS)
    p_byok.add_argument("--key", required=True, help="The provider API key (never logged)")
    p_byok.add_argument("--base-url", help="Optional custom endpoint for the key")
    p_byok.add_argument("--uc-org-id", help="Also backfill workspace.uc_org_id")
    p_byok.add_argument("--dry-run", action="store_true")

    p_credit = sub.add_parser(
        "credit", help="Mint a budget-capped comped gateway key (metered, hard-stop)."
    )
    p_credit.add_argument("--workspace", required=True, help="Workspace UUID")
    p_credit.add_argument(
        "--budget", required=True, type=float, help="Comped credit ceiling in upstream USD"
    )
    p_credit.add_argument(
        "--budget-duration",
        help="Reset the budget each period (e.g. '30d' for a monthly $/mo comp). "
        "Omit for a one-time cumulative cap.",
    )
    p_credit.add_argument(
        "--org-id", help="UC org / Lago customer id (default: workspace.uc_org_id)"
    )
    p_credit.add_argument("--uc-org-id", help="Also backfill workspace.uc_org_id first")
    p_credit.add_argument("--dry-run", action="store_true")

    p_org = sub.add_parser("org", help="Backfill workspace.uc_org_id only.")
    p_org.add_argument("--workspace", required=True, help="Workspace UUID")
    p_org.add_argument("--uc-org-id", required=True, help="UC org id (org_<uuid>)")
    p_org.add_argument("--dry-run", action="store_true")

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
