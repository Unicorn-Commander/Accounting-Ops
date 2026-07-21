#!/usr/bin/env python3
"""Seed a default chart of accounts for an entity in a self-host workspace.

This is the provisioning-time COA seed used by the Community Edition self-host
bootstrap. It runs as the ADMIN/owner role (ADMIN_DATABASE_URL, falling back to
DATABASE_URL), so — unlike the ``create_entity_with_coa`` MCP tool — it does NOT
require an authenticated caller. Idempotent on ``(entity_id, account_number)``:
re-running skips accounts that already exist.

Usage:
    python -m scripts.seed_default_coa <workspace_uuid> <entity_slug> \\
        [--template c_corp] [--industry tech_saas]

``--template`` defaults to the entity's own ``kind``. Valid template kinds:
    c_corp s_corp llc partnership sole_proprietorship personal non_profit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents._shared.coa_templates import get_accounts  # noqa: E402
from app.config import get_admin_database_url  # noqa: E402
from app.journal.accounts import canonical_form, normal_balance_for  # noqa: E402
from app.workspace_session import workspace_session  # noqa: E402

# Template alias -> canonical template kind (mirrors create_entity_with_coa).
_TEMPLATE_ALIAS = {
    "sole_prop": "sole_proprietorship",
    "sole_proprietorship": "sole_proprietorship",
    "personal_1040": "personal",
    "personal": "personal",
    "nonprofit": "non_profit",
    "non_profit": "non_profit",
}


async def seed_default_coa(
    workspace_id: uuid.UUID,
    entity_slug: str,
    *,
    template: str | None = None,
    industry: str | None = None,
) -> tuple[int, int]:
    """Insert a template COA for the entity. Returns (inserted, skipped)."""
    engine = create_async_engine(get_admin_database_url(), pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    inserted = skipped = 0
    try:
        async with workspace_session(sm, workspace_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id::text AS id, kind::text AS kind "
                        "FROM entity WHERE slug = :slug"
                    ),
                    {"slug": entity_slug},
                )
            ).mappings().first()
            if not row:
                raise SystemExit(
                    f"No entity with slug {entity_slug!r} in workspace {workspace_id}"
                )
            entity_id = row["id"]
            template_kind = _TEMPLATE_ALIAS.get(
                (template or row["kind"]).strip().lower(), (template or row["kind"])
            )

            for acct in get_accounts(template_kind, industry=industry):
                number = str(acct["number"]).strip()
                name = str(acct["name"]).strip()
                atype = str(acct["type"]).strip().lower()
                nb = acct.get("normal_balance") or normal_balance_for(atype)
                notes_parts = []
                if acct.get("tax_form_line"):
                    notes_parts.append(f"Tax line: {acct['tax_form_line']}")
                if acct.get("notes"):
                    notes_parts.append(acct["notes"])
                notes = " | ".join(notes_parts) or None

                dup = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM chart_of_account "
                            "WHERE entity_id = :eid AND account_number = :num"
                        ),
                        {"eid": entity_id, "num": number},
                    )
                ).first()
                if dup:
                    skipped += 1
                    continue
                await session.execute(
                    text(
                        """
                        INSERT INTO chart_of_account (
                            workspace_id, entity_id, account_number, account_name,
                            canonical_form, account_type, normal_balance,
                            is_system, is_active, notes
                        ) VALUES (
                            :wid, :eid, :num, :name,
                            :canonical, :atype, :nb,
                            true, true, :notes
                        )
                        """
                    ),
                    {
                        "wid": str(workspace_id),
                        "eid": entity_id,
                        "num": number,
                        "name": name,
                        "canonical": canonical_form(number, name),
                        "atype": atype,
                        "nb": nb,
                        "notes": notes,
                    },
                )
                inserted += 1
    finally:
        await engine.dispose()
    return inserted, skipped


async def _main() -> None:
    p = argparse.ArgumentParser(description="Seed a default chart of accounts.")
    p.add_argument("workspace_id", help="Workspace UUID")
    p.add_argument("entity_slug", help="Entity slug (e.g. 'my-company')")
    p.add_argument("--template", help="COA template kind (default: entity kind)")
    p.add_argument("--industry", help="Optional industry sub-template")
    args = p.parse_args()

    inserted, skipped = await seed_default_coa(
        uuid.UUID(args.workspace_id),
        args.entity_slug,
        template=args.template,
        industry=args.industry,
    )
    print(f"chart of accounts seeded: {inserted} inserted, {skipped} already present")


if __name__ == "__main__":
    asyncio.run(_main())
