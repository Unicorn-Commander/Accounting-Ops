"""COA template library — starter charts of accounts per entity kind.

Each template module exposes:
  - ACCOUNTS: list[dict] — list of seed COA rows with metadata
  - TEMPLATE_META: dict   — kind, typical_use_cases, industry_overrides

These templates are CONSUMED by ``app.tools.create_entity_with_coa`` which
the Setup Assistant persona uses to provision a new entity end-to-end.

Each account row dict shape::

    {
      "number": "1000",             # account number — 4-8 chars
      "name": "Cash",               # human label
      "type": "asset",              # asset / liability / equity / revenue /
                                    # cogs / expense / other_income /
                                    # other_expense / tax
      "normal_balance": "debit",    # debit | credit (derived if omitted)
      "tax_form_line": "Form 1120 Line 1",  # narrative annotation
      "notes": "..."                # optional CPA note
    }

Industry overrides are ADDITIVE — they extend (never replace) the base template.
"""
from __future__ import annotations

from typing import Any

from agents._shared.coa_templates import (
    c_corp,
    llc,
    nonprofit,
    partnership,
    personal_1040,
    s_corp,
    sole_prop,
)

# Registry: kind -> module
_REGISTRY: dict[str, Any] = {
    "c_corp": c_corp,
    "s_corp": s_corp,
    "llc": llc,
    "sole_prop": sole_prop,
    "sole_proprietorship": sole_prop,  # synonym
    "personal": personal_1040,
    "personal_1040": personal_1040,
    "partnership": partnership,
    "nonprofit": nonprofit,
    "non_profit": nonprofit,  # synonym
}


def list_kinds() -> list[str]:
    """Canonical kind keys (unique modules)."""
    seen: set[str] = set()
    out: list[str] = []
    for k, m in _REGISTRY.items():
        kind = m.TEMPLATE_META["kind"]
        if kind in seen:
            continue
        seen.add(kind)
        out.append(kind)
    return out


def get_template(kind: str) -> Any:
    """Return the module for a kind, raising KeyError if unknown.

    Synonyms (e.g. ``sole_proprietorship`` -> ``sole_prop``) are resolved.
    """
    key = (kind or "").strip().lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown COA template kind {kind!r}. "
            f"Valid kinds: {sorted(list_kinds())}"
        )
    return _REGISTRY[key]


def get_accounts(kind: str, *, industry: str | None = None) -> list[dict[str, Any]]:
    """Return the full account list for a kind, with optional industry overrides.

    Industry overrides are ADDITIVE: the base account list is returned,
    followed by any industry-specific extra accounts. Duplicate account_numbers
    are de-duplicated by keeping the first occurrence (base wins) so the
    industry list cannot silently override a base account.
    """
    module = get_template(kind)
    accounts: list[dict[str, Any]] = list(module.ACCOUNTS)
    if industry is not None:
        overrides = module.TEMPLATE_META.get("industry_overrides", {}).get(
            industry, []
        )
        seen_numbers = {a["number"] for a in accounts}
        for extra in overrides:
            if extra["number"] not in seen_numbers:
                accounts.append(extra)
                seen_numbers.add(extra["number"])
    return accounts


def template_summary(kind: str) -> dict[str, Any]:
    """High-level summary an agent can show before provisioning."""
    module = get_template(kind)
    return {
        "kind": module.TEMPLATE_META["kind"],
        "account_count": len(module.ACCOUNTS),
        "typical_use_cases": module.TEMPLATE_META.get("typical_use_cases", []),
        "industries_supported": sorted(
            module.TEMPLATE_META.get("industry_overrides", {}).keys()
        ),
        "description": module.TEMPLATE_META.get("description", ""),
    }


__all__ = (
    "get_accounts",
    "get_template",
    "list_kinds",
    "template_summary",
)
