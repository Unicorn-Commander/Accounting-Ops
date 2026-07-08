"""Personal 1040 (household) chart of accounts.

Tracks household-level cash flow, investment income, charitable contributions,
and itemized-deduction-eligible expenses. Designed for an "active" personal
finance dashboard — not just retroactive tax accounting.

NOT a business entity. The Setup Assistant routes any business-income questions
to the appropriate business template.

Military / VA disability has top-level rows so the categorizer routes them
correctly — VA disability is generally federally non-taxable; military
retirement may have state exclusions (e.g., SC fully excludes military retirement
income from state tax for age 65+).
"""
from __future__ import annotations

from typing import Any


ACCOUNTS: list[dict[str, Any]] = [
    # ------ 1xxx ASSETS (household balance sheet) ------
    {"number": "1010", "name": "Cash · Checking", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1020", "name": "Cash · Savings", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1030", "name": "Cash · Money Market", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1100", "name": "Brokerage — Taxable", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1110", "name": "IRA — Traditional", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1120", "name": "IRA — Roth", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1130", "name": "401(k) / 403(b)", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1140", "name": "HSA", "type": "asset",
     "normal_balance": "debit",
     "notes": "Triple-tax-advantaged. Contributions deductible Schedule 1 Line 13."},
    {"number": "1150", "name": "529 Plan", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1200", "name": "Primary Residence (cost basis)", "type": "asset",
     "normal_balance": "debit"},
    {"number": "1210", "name": "Vehicles", "type": "asset",
     "normal_balance": "debit"},

    # ------ 2xxx LIABILITIES ------
    {"number": "2010", "name": "Credit Cards", "type": "liability",
     "normal_balance": "credit"},
    {"number": "2100", "name": "Mortgage", "type": "liability",
     "normal_balance": "credit"},
    {"number": "2200", "name": "Auto Loans", "type": "liability",
     "normal_balance": "credit"},
    {"number": "2300", "name": "Student Loans", "type": "liability",
     "normal_balance": "credit",
     "notes": "Interest deductible up to $2500/yr (above-line) — phase-out applies."},
    {"number": "2400", "name": "Other Personal Debt", "type": "liability",
     "normal_balance": "credit"},

    # ------ 3xxx EQUITY ------
    {"number": "3010", "name": "Net Worth", "type": "equity",
     "normal_balance": "credit",
     "notes": "Calculated; not directly journaled."},

    # ------ 4xxx INCOME ------
    {"number": "4010", "name": "Wages — W-2", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "1040 Line 1a"},
    {"number": "4015", "name": "Wages — Spouse W-2", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "1040 Line 1a"},
    {"number": "4020", "name": "Self-employment Income (1099-NEC)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Schedule C"},
    {"number": "4030", "name": "Interest Income (1099-INT)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "1040 Line 2b / Schedule B"},
    {"number": "4040", "name": "Dividend Income — Ordinary (1099-DIV)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "1040 Line 3b"},
    {"number": "4045", "name": "Dividend Income — Qualified (1099-DIV)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "1040 Line 3a",
     "notes": "Eligible for LTCG rates (0/15/20%)."},
    {"number": "4050", "name": "Capital Gains — Short-term", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Schedule D"},
    {"number": "4055", "name": "Capital Gains — Long-term", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Schedule D"},
    {"number": "4060", "name": "Rental Income", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Schedule E"},
    {"number": "4070", "name": "Social Security Benefits", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "1040 Line 6a/b",
     "notes": "0-85% taxable depending on combined income."},

    # MILITARY / VA - special treatment
    {"number": "4080", "name": "Military Retirement", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "1040 Line 5a/b",
     "notes": "Federally taxable. State varies — SC excludes all military retirement; check state."},
    {"number": "4081", "name": "VA Disability Compensation", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "NOT reportable — federally excludable",
     "notes": "IRC §104(a)(4) — federally non-taxable. Do not include in 1040 income."},
    {"number": "4082", "name": "Combat-Related Special Compensation (CRSC)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "NOT reportable",
     "notes": "Non-taxable per §104."},

    # SSDI / Disability
    {"number": "4085", "name": "SSDI Benefits", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "1040 Line 6a/b",
     "notes": "Taxability follows Social Security rules. CONSTRAINT: SSDI earnings limits if recipient continues to work."},

    # Other
    {"number": "4090", "name": "Other Income", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "1040 Schedule 1 Line 8"},

    # ------ 5xxx HOUSEHOLD EXPENSES (cash flow, not directly tax-deductible) ------
    {"number": "5100", "name": "Housing — Mortgage Principal", "type": "expense",
     "normal_balance": "debit",
     "notes": "Reduces liability — track to expense for budgeting; tax-deductible portion is interest only."},
    {"number": "5110", "name": "Housing — Mortgage Interest", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule A Line 8a (if itemizing)"},
    {"number": "5120", "name": "Housing — Property Tax", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule A Line 5b ($10K SALT cap with state income tax)"},
    {"number": "5130", "name": "Housing — Utilities", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5140", "name": "Housing — Insurance (homeowner)", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5150", "name": "Housing — Maintenance & Repairs", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5160", "name": "Housing — HOA / Condo Fees", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5200", "name": "Transportation — Fuel", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5210", "name": "Transportation — Insurance", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5220", "name": "Transportation — Maintenance", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5300", "name": "Food — Groceries", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5310", "name": "Food — Restaurants", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5400", "name": "Healthcare — Premiums", "type": "expense",
     "normal_balance": "debit",
     "notes": "Schedule A Line 1 if itemizing AND exceeds 7.5% AGI."},
    {"number": "5410", "name": "Healthcare — Out-of-pocket", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule A Line 1 (if itemizing + >7.5% AGI)"},
    {"number": "5420", "name": "Healthcare — HSA Eligible", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5500", "name": "Insurance — Life", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5510", "name": "Insurance — Umbrella", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5600", "name": "Charitable Contributions — Cash", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule A Line 11 (if itemizing; 60% AGI limit on cash gifts)"},
    {"number": "5610", "name": "Charitable Contributions — Non-cash", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule A Line 12 / Form 8283 (if >$500)"},
    {"number": "5700", "name": "Education", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5800", "name": "Entertainment / Recreation", "type": "expense",
     "normal_balance": "debit"},
    {"number": "5900", "name": "Other Personal", "type": "expense",
     "normal_balance": "debit"},

    # ------ 7xxx TRANSFERS ------
    {"number": "7010", "name": "Savings Transfer", "type": "other_income",
     "normal_balance": "credit",
     "notes": "Between accounts — not income. Filter out of P&L."},
    {"number": "7020", "name": "Retirement Contribution (pre-tax)", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Schedule 1 (above-line) for IRA"},
    {"number": "7030", "name": "Retirement Contribution (Roth)", "type": "other_expense",
     "normal_balance": "debit"},

    # ------ 9xxx TAX (estimated tax tracking) ------
    {"number": "9010", "name": "Federal Income Tax Withheld", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "1040 Line 25a (W-2) / 25b (1099)"},
    {"number": "9020", "name": "State Income Tax Withheld", "type": "tax",
     "normal_balance": "debit"},
    {"number": "9050", "name": "Federal Estimated Tax Paid", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "1040 Line 26"},
    {"number": "9060", "name": "State Estimated Tax Paid", "type": "tax",
     "normal_balance": "debit"},
    {"number": "9100", "name": "Self-employment Tax", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "Schedule SE"},
    {"number": "9990", "name": "Suspense — Uncategorized", "type": "other_expense",
     "normal_balance": "debit"},
]


TEMPLATE_META: dict[str, Any] = {
    "kind": "personal_1040",
    "description": (
        "Personal 1040 household. Tracks W-2 + investment income + budgeting. "
        "Handles military retirement and VA disability with the correct tax "
        "treatment baked in. Routes business income to a separate entity "
        "if the user has one."
    ),
    "min_entity_count": 1,
    "typical_use_cases": [
        "household_w2", "household_w2_plus_side_income",
        "military_retiree", "va_disability_recipient",
        "household_with_investments", "household_mfj", "household_single",
    ],
    "industry_overrides": {
        "military_retiree": [
            {"number": "5410", "name": "Tricare / VA Healthcare", "type": "expense",
             "normal_balance": "debit"},
        ],
        "high_income_household": [
            {"number": "4046", "name": "Dividend Income — Qualified Foreign", "type": "revenue",
             "normal_balance": "credit",
             "tax_form_line": "1040 Line 3a (also foreign tax credit Form 1116)"},
            {"number": "5620", "name": "Investment Advisory Fees", "type": "expense",
             "normal_balance": "debit",
             "notes": "Not deductible 2018-2025 under TCJA. Track for budgeting only."},
        ],
        "household_with_rental": [
            {"number": "4061", "name": "Rental Income — Property A", "type": "revenue",
             "normal_balance": "credit",
             "tax_form_line": "Schedule E"},
            {"number": "5125", "name": "Rental — Depreciation", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Schedule E / Form 4562"},
        ],
    },
}
