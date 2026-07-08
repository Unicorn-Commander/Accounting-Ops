"""MCP tool: provision a new entity + tailored COA in one shot.

Backs the Setup Assistant chat persona. End-to-end:

  1. Insert a row in ``entity`` (idempotent on ``slug``).
  2. Resolve the COA template via ``agents._shared.coa_templates.get_accounts``.
  3. Apply industry-specific overrides (additive — never replaces base).
  4. Insert COA rows into ``chart_of_account`` (idempotent on ``(entity_id,
     account_number)``).
  5. Append a ``decision_audit`` row capturing the provisioning decision.

Role floor: ``accountant`` (provisioning is a write op, but stays below admin
because customers should be able to add their own entities without escalation).

Idempotent — re-running with the same ``display_name`` returns the existing
entity rather than creating a duplicate. COA inserts skip rows whose
``(entity_id, account_number)`` already exists.
"""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from agents._shared.coa_templates import (
    get_accounts,
    list_kinds,
    template_summary,
)
from app.db import SessionLocal
from app.journal.accounts import canonical_form, normal_balance_for
from app.mcp_auth import authorize_workspace
from app.tools._audit import write_decision_audit
from app.workspace_session import workspace_session


# Mapping of *template kind* -> *entity_type enum value*. Most are 1:1; a few
# template aliases route to the canonical DB enum.
_KIND_TO_ENTITY_TYPE: dict[str, str] = {
    "c_corp": "c_corp",
    "s_corp": "s_corp",
    "llc": "llc",
    "partnership": "partnership",
    "sole_prop": "sole_proprietorship",
    "sole_proprietorship": "sole_proprietorship",
    "personal": "personal",
    "personal_1040": "personal",
    "nonprofit": "non_profit",
    "non_profit": "non_profit",
    "trust": "trust",
    "estate": "estate",
    "other": "other",
}


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s[:96] or "entity"


def _resolve_entity_type(kind: str) -> str:
    key = (kind or "").strip().lower()
    if key not in _KIND_TO_ENTITY_TYPE:
        raise ValueError(
            f"Unknown entity kind {kind!r}. Valid: {sorted(_KIND_TO_ENTITY_TYPE.keys())}"
        )
    return _KIND_TO_ENTITY_TYPE[key]


def _resolve_template_kind(kind: str, coa_template: str | None) -> str:
    """Pick a COA template kind. Falls back to ``kind`` if ``coa_template`` is None.

    Some entity kinds don't have their own template (e.g. ``trust``, ``estate``,
    ``other``) — they fall back to the closest sensible default:
      - trust / estate -> sole_prop (single-entity reporting)
      - other          -> sole_prop (minimum-viable starting chart)
    """
    chosen = (coa_template or kind or "").strip().lower()
    if chosen in ("trust", "estate", "other"):
        chosen = "sole_prop"
    known = set(list_kinds()) | set(_KIND_TO_ENTITY_TYPE.keys())
    if chosen not in known:
        raise ValueError(
            f"Unknown COA template {chosen!r}. Valid: {sorted(known)}"
        )
    # Resolve synonym (e.g. personal -> personal_1040, sole_proprietorship -> sole_prop)
    return chosen


async def create_entity_with_coa(
    workspace_id: UUID,
    display_name: str,
    legal_name: str,
    kind: str,
    country_code: str = "US",
    state_of_incorporation: str | None = None,
    incorporated_at: str | None = None,
    tax_id_encrypted: str | None = None,
    registered_agent: str | None = None,
    industry: str | None = None,
    coa_template: str | None = None,
) -> dict[str, Any]:
    """Create a new entity with a tailored chart of accounts.

    Args:
        workspace_id: UUID of the workspace.
        display_name: Human-readable entity name (e.g. "Acme Inc").
            Slug is auto-derived; re-using the same display_name returns the
            existing entity.
        legal_name: Legal name as registered with the state (e.g.
            "Magic Unicorn LLC").
        kind: One of ``c_corp / s_corp / llc / partnership /
            sole_proprietorship / personal / non_profit / trust / estate / other``.
            Synonyms accepted (``sole_prop`` -> ``sole_proprietorship``,
            ``personal_1040`` -> ``personal``, ``nonprofit`` -> ``non_profit``).
        country_code: ISO-3166-1 alpha-2 (default "US").
        state_of_incorporation: US state postal code (e.g. "SC"), null OK.
        incorporated_at: ISO date (YYYY-MM-DD) of formation, null OK.
        tax_id_encrypted: Pre-encrypted EIN bytes (caller is responsible for
            encryption; this column is bytes only). Pass null and add the EIN
            via a separate flow after provisioning.
        registered_agent: Name of the registered agent on file.
        industry: Optional industry sub-template (e.g. "tech_saas") used to
            extend the base COA with industry-specific accounts. Sub-templates
            are listed in each template's ``TEMPLATE_META["industry_overrides"]``.
        coa_template: Optional explicit COA template kind. Defaults to ``kind``.
            Use this to e.g. give an LLC a sole_prop COA when filing as a
            disregarded entity.

    Returns:
        Dict with: ``entity_id``, ``slug``, ``display_name``, ``kind``,
        ``coa_count``, ``coa_inserted``, ``coa_skipped``, ``template_used``,
        ``industry``, ``existed_before``.

    Role floor: accountant.
    """
    await authorize_workspace(workspace_id, None, min_role="accountant")

    # ---- Input validation ----
    display_name = (display_name or "").strip()
    legal_name = (legal_name or display_name or "").strip()
    if not display_name:
        raise ValueError("display_name is required")
    if not legal_name:
        raise ValueError("legal_name is required (defaults to display_name)")

    db_kind = _resolve_entity_type(kind)
    template_kind = _resolve_template_kind(kind, coa_template)

    if state_of_incorporation is not None:
        state_of_incorporation = state_of_incorporation.upper().strip()[:2] or None

    country_code = (country_code or "US").upper()[:2]

    # Industry must be valid for this template if specified
    if industry is not None:
        summary = template_summary(template_kind)
        valid = set(summary.get("industries_supported", []))
        if industry not in valid:
            # Be forgiving — log it as a "no override" rather than error.
            # Bad industry is a no-op rather than a hard failure.
            industry = None

    slug = _slugify(display_name)

    # ---- 1. Resolve / create the entity row ----
    existed_before = False
    async with workspace_session(SessionLocal, workspace_id) as session:
        existing = (
            await session.execute(
                text(
                    "SELECT id::text AS id, kind::text AS kind, display_name "
                    "FROM entity WHERE slug = :slug"
                ),
                {"slug": slug},
            )
        ).mappings().first()
        if existing:
            entity_id = UUID(existing["id"])
            existed_before = True
        else:
            entity_id = uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO entity (
                        id, workspace_id, slug, display_name, legal_name,
                        kind, country_code,
                        state_of_incorporation, incorporated_at,
                        registered_agent, tax_id_encrypted,
                        status
                    )
                    VALUES (
                        :id, :wid, :slug, :display_name, :legal_name,
                        CAST(:kind AS entity_type), :country,
                        :soi, CAST(:incorp AS date),
                        :ra, :tax_id,
                        'active'
                    )
                    """
                ),
                {
                    "id": str(entity_id),
                    "wid": str(workspace_id),
                    "slug": slug,
                    "display_name": display_name,
                    "legal_name": legal_name,
                    "kind": db_kind,
                    "country": country_code,
                    "soi": state_of_incorporation,
                    "incorp": incorporated_at,
                    "ra": registered_agent,
                    "tax_id": tax_id_encrypted.encode("utf-8") if isinstance(tax_id_encrypted, str) else tax_id_encrypted,
                },
            )

        # ---- 2-4. Seed COA ----
        accounts = get_accounts(template_kind, industry=industry)
        inserted = 0
        skipped = 0
        for acct in accounts:
            number = str(acct["number"]).strip()
            name = str(acct["name"]).strip()
            atype = str(acct["type"]).strip().lower()
            nb = acct.get("normal_balance") or normal_balance_for(atype)
            notes = acct.get("notes")
            tax_line = acct.get("tax_form_line")
            # Combine tax_form_line + notes into the single notes column
            combined_notes_parts: list[str] = []
            if tax_line:
                combined_notes_parts.append(f"Tax line: {tax_line}")
            if notes:
                combined_notes_parts.append(notes)
            combined_notes = " | ".join(combined_notes_parts) if combined_notes_parts else None

            # Idempotent: skip if (entity_id, account_number) already exists
            dup = (
                await session.execute(
                    text(
                        "SELECT 1 FROM chart_of_account "
                        "WHERE entity_id = :eid AND account_number = :num"
                    ),
                    {"eid": str(entity_id), "num": number},
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
                    "eid": str(entity_id),
                    "num": number,
                    "name": name,
                    "canonical": canonical_form(number, name),
                    "atype": atype,
                    "nb": nb,
                    "notes": combined_notes,
                },
            )
            inserted += 1

        # ---- 5. Decision-audit row ----
        try:
            # Re-resolve to get the membership ctx for decision_audit.
            ctx = await authorize_workspace(
                workspace_id, entity_id, min_role="accountant"
            )
            await write_decision_audit(
                session,
                workspace_id=workspace_id,
                entity_id=entity_id,
                ctx=ctx,
                tool_name="create_entity_with_coa",
                request_payload={
                    "display_name": display_name,
                    "legal_name": legal_name,
                    "kind": kind,
                    "country_code": country_code,
                    "state_of_incorporation": state_of_incorporation,
                    "incorporated_at": incorporated_at,
                    "industry": industry,
                    "coa_template": template_kind,
                },
                response_payload={
                    "entity_id": str(entity_id),
                    "coa_inserted": inserted,
                    "coa_skipped": skipped,
                    "existed_before": existed_before,
                },
                reasoning=(
                    f"Setup-assistant provisioned entity "
                    f"{display_name!r} ({db_kind}) with "
                    f"{template_kind} COA template" +
                    (f" + {industry} industry overrides" if industry else "")
                ),
                outcome="created" if not existed_before else "extended",
            )
        except Exception:
            # decision_audit failures must not block provisioning
            pass

    return {
        "entity_id": str(entity_id),
        "slug": slug,
        "display_name": display_name,
        "legal_name": legal_name,
        "kind": db_kind,
        "coa_count": len(accounts),
        "coa_inserted": inserted,
        "coa_skipped": skipped,
        "template_used": template_kind,
        "industry": industry,
        "country_code": country_code,
        "state_of_incorporation": state_of_incorporation,
        "incorporated_at": incorporated_at,
        "existed_before": existed_before,
    }
