"""Multi-member LLC chart of accounts (Form 1065 partnership filer).

For single-member LLCs disregarded for tax purposes, use the ``sole_prop``
template (LLC is the legal wrapper; the SMLLC files Schedule C with the
member's 1040).

This template assumes:
  - 2+ members (multi-member LLC)
  - Partnership tax treatment (Form 1065 + K-1s)
  - Member capital accounts (per-member 3xxx accounts)
  - Guaranteed payments tracking
"""
from __future__ import annotations

from typing import Any


ACCOUNTS: list[dict[str, Any]] = [
    # ------ 1xxx ASSETS ------
    {"number": "1010", "name": "Cash · Primary Checking", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Schedule L Line 1"},
    {"number": "1020", "name": "Cash · Savings", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Schedule L Line 1"},
    {"number": "1100", "name": "Accounts Receivable", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Schedule L Line 2a"},
    {"number": "1200", "name": "Undeposited Funds", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Schedule L Line 1"},
    {"number": "1210", "name": "Stripe Receivable", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Schedule L Line 2a"},
    {"number": "1300", "name": "Prepaid Expenses", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Schedule L Line 6"},
    {"number": "1500", "name": "Fixed Assets", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Schedule L Line 9a"},
    {"number": "1550", "name": "Accumulated Depreciation", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Schedule L Line 9b"},

    # ------ 2xxx LIABILITIES ------
    {"number": "2010", "name": "Accounts Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule L Line 15"},
    {"number": "2100", "name": "Accrued Expenses", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule L Line 17"},
    {"number": "2200", "name": "Credit Card", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule L Line 16"},
    {"number": "2300", "name": "Sales Tax Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule L Line 16"},
    {"number": "2400", "name": "Notes Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule L Line 16"},
    {"number": "2500", "name": "Loans from Members (recourse)", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule L Line 19",
     "notes": "Member loans — track recourse vs nonrecourse for §752 basis allocations."},

    # ------ 3xxx MEMBER CAPITAL ------
    # Generic placeholders. The Setup Assistant should ask for the member list
    # and create per-member accounts (3100 Member A, 3110 Member B, etc.)
    {"number": "3010", "name": "Member Contributions", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule M-2",
     "notes": "Aggregate member contributions. Split per member in subsidiary ledger."},
    {"number": "3020", "name": "Member Distributions", "type": "equity",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Schedule M-2 Line 6b",
     "notes": "Distributions to members. Distinct from guaranteed payments."},
    {"number": "3050", "name": "Members' Capital", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule L Line 21",
     "notes": "Aggregate capital. Per-member capital tracked on K-1 Schedule L."},
    {"number": "3100", "name": "Member A Capital", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 K-1 Part II Item L",
     "notes": "Per-member capital. Rename to reflect actual member name."},
    {"number": "3110", "name": "Member B Capital", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 K-1 Part II Item L",
     "notes": "Per-member capital. Rename to reflect actual member name."},

    # ------ 4xxx REVENUE ------
    {"number": "4010", "name": "Revenue · Services", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Line 1a"},
    {"number": "4020", "name": "Revenue · Products", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Line 1a"},
    {"number": "4030", "name": "Revenue · Subscriptions", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Line 1a"},
    {"number": "4040", "name": "Revenue · Rental (passive)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule K Line 3 / Form 8825"},
    {"number": "4090", "name": "Revenue · Other", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Line 7"},

    # ------ 5xxx COGS ------
    {"number": "5010", "name": "COGS · Inventory", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 2 / Form 1125-A"},
    {"number": "5020", "name": "COGS · Marketplace Fees", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 2"},
    {"number": "5030", "name": "COGS · Shipping", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 2"},

    # ------ 5xxx OPERATING EXPENSES ------
    # GUARANTEED PAYMENTS — partnership-specific
    {"number": "5100", "name": "Guaranteed Payments — Services", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 10",
     "notes": "§707(c). Treated as compensation for member tax purposes. K-1 Line 4a."},
    {"number": "5110", "name": "Guaranteed Payments — Capital", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 10",
     "notes": "Interest-like payments. K-1 Line 4b."},

    # SaaS / Cloud
    {"number": "5210", "name": "SaaS · AI · Production", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5215", "name": "SaaS · AI · Productivity", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5220", "name": "SaaS · Dev Tools", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5225", "name": "SaaS · Productivity & Office", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5230", "name": "SaaS · Communications", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5310", "name": "Cloud Infrastructure", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},

    # Banking & processors
    {"number": "5510", "name": "Banking Fees", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5520", "name": "Payment Processor Fees", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},

    # Professional services
    {"number": "5610", "name": "Professional Services — Legal", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5620", "name": "Professional Services — CPA / Tax", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5630", "name": "Contractor · 1099-NEC", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},

    # Employees (non-member)
    {"number": "5660", "name": "Salaries & Wages", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 9"},
    {"number": "5670", "name": "Employee Benefits", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 19"},
    {"number": "5680", "name": "Payroll Taxes", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 14"},

    # Office / supplies / rent
    {"number": "5710", "name": "Office Supplies", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5720", "name": "Rent", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 13"},
    {"number": "5730", "name": "Utilities", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5740", "name": "Insurance", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},

    # Meals
    {"number": "5810", "name": "Meals — 50% Deductible", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20 (M-1 50%)"},
    {"number": "5820", "name": "Meals — 100% Deductible (de minimis)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},

    # Travel
    {"number": "5910", "name": "Travel — Lodging", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},
    {"number": "5920", "name": "Travel — Transportation", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},

    # Marketing
    {"number": "5940", "name": "Marketing & Advertising", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 20"},

    # ------ 7xxx OTHER INCOME ------
    {"number": "7010", "name": "Interest Income", "type": "other_income",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule K Line 5"},
    {"number": "7020", "name": "Dividend Income", "type": "other_income",
     "normal_balance": "credit",
     "tax_form_line": "Form 1065 Schedule K Line 6a"},

    # ------ 8xxx OTHER EXPENSE ------
    {"number": "8010", "name": "Interest Expense", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 15"},
    {"number": "8030", "name": "Depreciation Expense", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1065 Line 16a / Form 4562"},

    {"number": "9990", "name": "Suspense — Uncategorized", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "NONE — must zero at close"},

    # ------ 9xxx TAX ------
    {"number": "9020", "name": "State Income Tax (entity-level)", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "State PTET if elected"},
    {"number": "9030", "name": "Franchise / Privilege Tax", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "State"},
]


TEMPLATE_META: dict[str, Any] = {
    "kind": "llc",
    "description": (
        "Multi-member LLC (Form 1065 partnership). Member capital accounts + "
        "guaranteed payments are the partnership-specific bits. For single-member "
        "LLCs disregarded for tax, use the sole_prop template."
    ),
    "min_entity_count": 1,
    "typical_use_cases": [
        "joint_venture", "investment_partnership", "real_estate_holding",
        "consulting_partnership", "professional_practice_2plus",
    ],
    "industry_overrides": {
        "real_estate": [
            {"number": "1600", "name": "Real Property", "type": "asset",
             "normal_balance": "debit",
             "tax_form_line": "Form 1065 Schedule L Line 9a / Form 8825"},
            {"number": "1610", "name": "Accumulated Depreciation — Real Property", "type": "asset",
             "normal_balance": "debit",
             "tax_form_line": "Form 1065 Schedule L Line 9b"},
            {"number": "5750", "name": "Property Tax", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 8825"},
            {"number": "5760", "name": "Property Management Fees", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 8825"},
        ],
        "consulting_services": [
            {"number": "5635", "name": "Subcontractor Services", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1065 Line 20"},
        ],
        "investment_partnership": [
            {"number": "7030", "name": "Capital Gain — Short-term", "type": "other_income",
             "normal_balance": "credit",
             "tax_form_line": "Form 1065 Schedule K Line 8"},
            {"number": "7040", "name": "Capital Gain — Long-term", "type": "other_income",
             "normal_balance": "credit",
             "tax_form_line": "Form 1065 Schedule K Line 9a"},
        ],
    },
}
