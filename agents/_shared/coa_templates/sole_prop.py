"""Sole Proprietorship chart of accounts (Schedule C filer on Form 1040).

Also serves as the default for single-member LLCs that are disregarded for
tax purposes (SMLLC -> Schedule C).

Simpler than C-Corp:
  - No officer comp (owner is not an employee — takes draws)
  - Owner's draw / owner's contribution equity accounts
  - Lines map to Schedule C
  - Self-employment tax tracking
"""
from __future__ import annotations

from typing import Any


ACCOUNTS: list[dict[str, Any]] = [
    # ------ 1xxx ASSETS ------
    {"number": "1010", "name": "Cash · Business Checking", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C (informational, no Schedule L for sole prop)"},
    {"number": "1020", "name": "Cash · Business Savings", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C"},
    {"number": "1100", "name": "Accounts Receivable", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1200", "name": "Undeposited Funds", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1210", "name": "Stripe Receivable", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1300", "name": "Inventory", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 33-41 (COGS)"},
    {"number": "1500", "name": "Fixed Assets", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 4562"},
    {"number": "1550", "name": "Accumulated Depreciation", "type": "asset",
     "normal_balance": "debit"},

    # ------ 2xxx LIABILITIES ------
    {"number": "2010", "name": "Accounts Payable", "type": "liability",
     "normal_balance": "credit"},
    {"number": "2100", "name": "Accrued Expenses", "type": "liability",
     "normal_balance": "credit"},
    {"number": "2200", "name": "Credit Card", "type": "liability",
     "normal_balance": "credit"},
    {"number": "2300", "name": "Sales Tax Payable", "type": "liability",
     "normal_balance": "credit"},
    {"number": "2400", "name": "Notes Payable", "type": "liability",
     "normal_balance": "credit"},

    # ------ 3xxx EQUITY (Sole-prop specific) ------
    {"number": "3010", "name": "Owner's Equity", "type": "equity",
     "normal_balance": "credit",
     "notes": "Catch-all equity. Sole-prop has no stock; this represents net invested capital."},
    {"number": "3020", "name": "Owner's Contribution", "type": "equity",
     "normal_balance": "credit",
     "notes": "Money/property contributed by the owner to the business."},
    {"number": "3030", "name": "Owner's Draw", "type": "equity",
     "normal_balance": "debit",
     "notes": "Owner withdrawals. NOT a deductible expense — it's a return of capital."},
    {"number": "3050", "name": "Retained Earnings", "type": "equity",
     "normal_balance": "credit",
     "notes": "Cumulative undistributed profit. Closes to Owner's Equity at year-end."},

    # ------ 4xxx REVENUE ------
    {"number": "4010", "name": "Gross Receipts · Services", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Schedule C Line 1"},
    {"number": "4020", "name": "Gross Receipts · Products", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Schedule C Line 1"},
    {"number": "4090", "name": "Other Income", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Schedule C Line 6"},

    # ------ 5xxx COGS (Schedule C Part III) ------
    {"number": "5010", "name": "Purchases", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 36"},
    {"number": "5020", "name": "Materials & Supplies", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 38"},
    {"number": "5030", "name": "Other COGS", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 39"},

    # ------ 5xxx OPERATING EXPENSES (Schedule C lines) ------
    {"number": "5100", "name": "Advertising", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 8"},
    {"number": "5200", "name": "Car & Truck Expenses", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 9",
     "notes": "Use standard mileage rate or actuals (not both)."},
    {"number": "5210", "name": "Commissions & Fees", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 10"},
    {"number": "5220", "name": "Contract Labor (1099-NEC)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 11"},
    {"number": "5230", "name": "Depletion", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 12"},
    {"number": "5300", "name": "Depreciation & §179", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 13 / Form 4562"},
    {"number": "5400", "name": "Employee Benefits", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 14"},
    {"number": "5500", "name": "Insurance (non-health)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 15"},
    {"number": "5510", "name": "Bank/Merchant Fees", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 17 (legal/prof) or 27a (other)"},
    {"number": "5520", "name": "Mortgage Interest (business)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 16a"},
    {"number": "5530", "name": "Other Interest", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 16b"},
    {"number": "5610", "name": "Legal & Professional Services", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 17"},
    {"number": "5620", "name": "Office Expense", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 18"},
    {"number": "5630", "name": "Pension & Profit-sharing Plans", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 19"},
    {"number": "5640", "name": "Rent — Vehicles/Machinery/Equipment", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 20a"},
    {"number": "5650", "name": "Rent — Other Business Property", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 20b"},
    {"number": "5660", "name": "Repairs & Maintenance", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 21"},
    {"number": "5670", "name": "Supplies", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 22"},
    {"number": "5680", "name": "Taxes & Licenses", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 23"},
    {"number": "5710", "name": "Travel", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 24a"},
    {"number": "5720", "name": "Meals (50%)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 24b"},
    {"number": "5730", "name": "Utilities", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 25"},
    {"number": "5740", "name": "Wages (employees, non-owner)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 26"},

    # SaaS / Cloud (Other Expenses — Line 27a/48)
    {"number": "5810", "name": "SaaS · AI", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 48 (Other Expense)"},
    {"number": "5820", "name": "SaaS · Productivity & Office", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 48"},
    {"number": "5830", "name": "Cloud Infrastructure", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 48"},
    {"number": "5840", "name": "Domains & DNS", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 48"},

    # Home office (separate calc on Form 8829)
    {"number": "5910", "name": "Home Office Use (Form 8829)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 30 / Form 8829",
     "notes": "Use simplified method ($5/sqft up to 300 sqft = $1500 cap) or actuals via Form 8829."},

    # ------ 7xxx OTHER INCOME ------
    {"number": "7010", "name": "Interest Income (business)", "type": "other_income",
     "normal_balance": "credit",
     "tax_form_line": "Schedule C Line 6"},

    # ------ 8xxx OTHER EXPENSE ------
    {"number": "8030", "name": "Bad Debts (specific charge-off)", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule C Line 27a / 48"},

    {"number": "9990", "name": "Suspense — Uncategorized", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "NONE — must zero at close"},

    # ------ 9xxx TAX ------
    {"number": "9100", "name": "Self-employment Tax (estimated)", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "Schedule SE",
     "notes": "Tracking only — not a Schedule C deduction. ½ SE tax goes on 1040 Schedule 1."},
    {"number": "9050", "name": "Federal Estimated Tax", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "Form 1040-ES"},
]


TEMPLATE_META: dict[str, Any] = {
    "kind": "sole_prop",
    "description": (
        "Sole Proprietorship (Schedule C on Form 1040). Simplest entity. Also "
        "default for single-member LLCs disregarded for tax. Owner's draw is "
        "NOT a deduction — track it but don't expense it."
    ),
    "min_entity_count": 1,
    "typical_use_cases": [
        "freelancer", "side_hustle", "smllc_disregarded",
        "consulting_solopreneur", "creator_economy",
    ],
    "industry_overrides": {
        "creator_economy": [
            {"number": "4030", "name": "Patreon/Substack/Sponsored", "type": "revenue",
             "normal_balance": "credit",
             "tax_form_line": "Schedule C Line 1"},
            {"number": "5850", "name": "Content Production Tools", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Schedule C Line 48"},
        ],
        "consulting_solopreneur": [
            {"number": "5860", "name": "Continuing Education", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Schedule C Line 48"},
        ],
        "trades_construction": [
            {"number": "5870", "name": "Tools & Equipment (under $2500)", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Schedule C Line 22",
             "notes": "De minimis safe harbor under $2500/item."},
            {"number": "5880", "name": "Job Site / Field Expenses", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Schedule C Line 48"},
        ],
    },
}
