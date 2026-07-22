"""Chart of accounts helpers + seed.

`canonical_form` is the string used in journal_entry.debit_account / credit_account
columns and matched against chart_of_account.canonical_form for joins.

Seed data (called by /api/workspaces/{wid}/journal/seed-coa or on first
entity creation) installs the standard chart per-entity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

AccountType = Literal[
    "asset", "liability", "equity", "revenue", "cogs",
    "expense", "other_income", "other_expense", "tax",
]


def canonical_form(account_number: str, account_name: str) -> str:
    """Join number and name with the canonical separator '·' (middle dot, U+00B7).

    This is the exact string stored in journal_entry.debit_account /
    credit_account and chart_of_account.canonical_form. Trailing whitespace
    is stripped; embedded ·  in account names is allowed but rare.
    """
    return f"{account_number.strip()}·{account_name.strip()}"


def normal_balance_for(account_type: AccountType) -> Literal["debit", "credit"]:
    """Per accounting conventions: assets + expenses normally debit; the rest credit."""
    if account_type in ("asset", "expense", "cogs", "other_expense", "tax"):
        return "debit"
    return "credit"


@dataclass(frozen=True)
class CoaTemplate:
    number: str
    name: str
    account_type: AccountType
    is_system: bool = True
    # If this account represents a connected bank account, the source +
    # external_account_id is filled in during entity-specific seeding.
    needs_external_link: bool = False
    notes: str | None = None


# Canonical chart-of-accounts template. Every entity gets the same shape,
# then per-entity seeding links specific cash accounts to specific connected
# bank accounts (1010 for the entity's primary checking, 2200 for any
# credit card on file, etc.).
DEFAULT_COA: list[CoaTemplate] = [
    # 1xxx — Assets
    CoaTemplate("1010", "Cash · Primary Checking", "asset", needs_external_link=True,
                notes="Connected to the entity's primary bank account via account_entity_mapping."),
    CoaTemplate("1011", "Cash · Secondary Checking", "asset", needs_external_link=True),
    CoaTemplate("1020", "Cash · Savings", "asset", needs_external_link=True),
    CoaTemplate("1100", "Accounts Receivable", "asset"),
    CoaTemplate("1200", "Undeposited Funds", "asset"),
    CoaTemplate("1300", "Prepaid Expenses", "asset"),
    CoaTemplate("1500", "Fixed Assets", "asset"),
    CoaTemplate("1550", "Accumulated Depreciation", "asset",
                notes="Contra-asset; normal balance is technically credit but stored as asset for SECT 1245."),
    # 2xxx — Liabilities
    CoaTemplate("2010", "Accounts Payable", "liability"),
    CoaTemplate("2100", "Accrued Expenses", "liability"),
    CoaTemplate("2200", "Credit Card", "liability", needs_external_link=True),
    CoaTemplate("2300", "Sales Tax Payable", "liability"),
    CoaTemplate("2400", "Notes Payable", "liability"),
    # 3xxx — Equity
    CoaTemplate("3010", "Owner Contribution", "equity"),
    CoaTemplate("3020", "Owner Distribution", "equity"),
    CoaTemplate("3050", "Retained Earnings", "equity"),
    # 4xxx — Revenue
    CoaTemplate("4010", "Revenue · Marketplace", "revenue"),
    CoaTemplate("4020", "Revenue · Music Royalty", "revenue"),
    CoaTemplate("4030", "Revenue · Services", "revenue"),
    CoaTemplate("4040", "Revenue · Subscriptions", "revenue"),
    CoaTemplate("4090", "Revenue · Other", "revenue"),
    # 5xxx — COGS
    CoaTemplate("5010", "COGS · Inventory", "cogs"),
    CoaTemplate("5020", "COGS · Marketplace Fees", "cogs",
                notes="eBay/Stripe/PayPal transaction fees on revenue."),
    CoaTemplate("5030", "COGS · Shipping", "cogs"),
    # 6xxx — Operating Expenses
    CoaTemplate("6100", "Infrastructure · CDN", "expense"),
    CoaTemplate("6110", "Infrastructure · VPS", "expense"),
    CoaTemplate("6120", "Infrastructure · Network", "expense"),
    CoaTemplate("6200", "SaaS · Productivity", "expense"),
    CoaTemplate("6210", "SaaS · AI", "expense"),
    CoaTemplate("6230", "SaaS · Email", "expense"),
    CoaTemplate("6300", "Contractor · 1099-NEC", "expense"),
    CoaTemplate("6400", "Office · Supplies", "expense"),
    CoaTemplate("6500", "Travel · Lodging", "expense"),
    CoaTemplate("6510", "Travel · Transport", "expense"),
    CoaTemplate("6520", "Meals", "expense",
                notes="Subject to IRS §274(d): requires receipt link to post regardless of materiality."),
    CoaTemplate("6350", "Payroll · Wages", "expense"),
    CoaTemplate("6600", "Rent · Facilities", "expense"),
    CoaTemplate("6610", "Utilities", "expense"),
    CoaTemplate("6620", "Vehicle · Repairs & Maintenance", "expense"),
    CoaTemplate("6700", "Bank Fees", "expense"),
    CoaTemplate("6800", "Insurance", "expense"),
    CoaTemplate("6900", "Inter-entity Transfer · Out", "expense"),
    # 7xxx — Other income
    CoaTemplate("7010", "Interest Income", "other_income"),
    CoaTemplate("7020", "FX Gain", "other_income"),
    CoaTemplate("7900", "Inter-entity Transfer · In", "other_income"),
    # 8xxx — Other expense
    CoaTemplate("8010", "Interest Expense", "other_expense"),
    CoaTemplate("8020", "FX Loss", "other_expense"),
    # 9xxx — Tax
    CoaTemplate("9010", "SC Franchise Tax", "tax"),
    CoaTemplate("9050", "Federal Estimated Tax", "tax"),
    CoaTemplate("9100", "Self-employment Tax", "tax"),
]


async def seed_coa_for_entity(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
) -> int:
    """Insert the default COA for one entity. Idempotent.

    Returns number of rows inserted (0 if already seeded).
    """
    existing = (
        await session.execute(
            text(
                "SELECT count(*) FROM chart_of_account WHERE entity_id = :eid"
            ),
            {"eid": entity_id},
        )
    ).scalar_one()
    if existing > 0:
        return 0

    inserted = 0
    for tmpl in DEFAULT_COA:
        await session.execute(
            text(
                """
                INSERT INTO chart_of_account (
                    workspace_id, entity_id, account_number, account_name,
                    canonical_form, account_type, normal_balance,
                    is_system, notes
                )
                VALUES (
                    :workspace_id, :entity_id, :number, :name,
                    :canonical, :account_type, :normal_balance,
                    true, :notes
                )
                """
            ),
            {
                "workspace_id": workspace_id,
                "entity_id": entity_id,
                "number": tmpl.number,
                "name": tmpl.name,
                "canonical": canonical_form(tmpl.number, tmpl.name),
                "account_type": tmpl.account_type,
                "normal_balance": normal_balance_for(tmpl.account_type),
                "notes": tmpl.notes,
            },
        )
        inserted += 1
    return inserted


async def link_external_account(
    session: AsyncSession,
    *,
    entity_id: str,
    account_number: str,
    source: str,
    external_account_id: str,
) -> bool:
    """Connect a COA account (e.g. 1010) to a connected bank account."""
    result = await session.execute(
        text(
            """
            UPDATE chart_of_account
            SET linked_external_source = :source,
                linked_external_account_id = :ext_id
            WHERE entity_id = :eid AND account_number = :num
            """
        ),
        {
            "source": source,
            "ext_id": external_account_id,
            "eid": entity_id,
            "num": account_number,
        },
    )
    return result.rowcount > 0
