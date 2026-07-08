"""S-Corporation chart of accounts (Form 1120-S filer).

Similar to C-Corp but with:
  - Shareholder distribution accounts (not dividends)
  - AAA / OAA equity tracking for pass-through basis
  - No §1202 QSBS treatment (pass-through entity)
  - K-1 line mapping rather than 1120 lines

Industry overrides additive.
"""
from __future__ import annotations

from typing import Any


ACCOUNTS: list[dict[str, Any]] = [
    # ------ 1xxx ASSETS ------
    {"number": "1010", "name": "Cash · Primary Checking", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Schedule L Line 1"},
    {"number": "1020", "name": "Cash · Savings", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Schedule L Line 1"},
    {"number": "1100", "name": "Accounts Receivable", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Schedule L Line 2a"},
    {"number": "1200", "name": "Undeposited Funds", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Schedule L Line 1"},
    {"number": "1210", "name": "Stripe Receivable", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Schedule L Line 2a"},
    {"number": "1300", "name": "Prepaid Expenses", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Schedule L Line 6"},
    {"number": "1500", "name": "Fixed Assets", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Schedule L Line 10a"},
    {"number": "1550", "name": "Accumulated Depreciation", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Schedule L Line 10b"},

    # ------ 2xxx LIABILITIES ------
    {"number": "2010", "name": "Accounts Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 16"},
    {"number": "2100", "name": "Accrued Expenses", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 18"},
    {"number": "2200", "name": "Credit Card", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 17"},
    {"number": "2300", "name": "Sales Tax Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 17"},
    {"number": "2310", "name": "Accrued Payroll Taxes", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 17"},
    {"number": "2400", "name": "Notes Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 17"},
    {"number": "2500", "name": "Loans from Shareholders", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 19",
     "notes": "Debt-basis impact: tracks basis-for-loss for shareholders. §385 indicia required."},

    # ------ 3xxx EQUITY (S-Corp specific) ------
    {"number": "3010", "name": "Capital Stock", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 22"},
    {"number": "3020", "name": "Additional Paid-in Capital", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 23"},
    {"number": "3040", "name": "Accumulated Adjustments Account (AAA)", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule M-2 AAA",
     "notes": "Tracks S-Corp earnings since election. Distributions ordering: AAA -> AE&P -> OAA -> basis."},
    {"number": "3045", "name": "Other Adjustments Account (OAA)", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule M-2 OAA",
     "notes": "Tax-exempt income + related expenses."},
    {"number": "3050", "name": "Retained Earnings", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule L Line 25"},
    {"number": "3110", "name": "Shareholder Distributions", "type": "equity",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Schedule K-1 Line 16d",
     "notes": "Contra-equity. Distinct from W-2 wages. Watch reasonable-comp rules."},

    # ------ 4xxx REVENUE ------
    {"number": "4010", "name": "Revenue · Services", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Line 1a"},
    {"number": "4020", "name": "Revenue · Products", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Line 1a"},
    {"number": "4030", "name": "Revenue · Subscriptions", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Line 1a"},
    {"number": "4090", "name": "Revenue · Other", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule K Line 10"},

    # ------ 5xxx COGS ------
    {"number": "5010", "name": "COGS · Inventory", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 2 / Form 1125-A"},
    {"number": "5020", "name": "COGS · Marketplace Fees", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 2"},
    {"number": "5030", "name": "COGS · Shipping", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 2"},

    # ------ 5xxx OPERATING EXPENSES ------
    {"number": "5210", "name": "SaaS · AI · Production", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5215", "name": "SaaS · AI · Productivity", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5220", "name": "SaaS · Dev Tools", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5225", "name": "SaaS · Productivity & Office", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5230", "name": "SaaS · Communications", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5310", "name": "Cloud Infrastructure", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5320", "name": "Domains & DNS", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},

    # Banking & processors
    {"number": "5510", "name": "Banking Fees", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5520", "name": "Payment Processor Fees", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},

    # Professional services
    {"number": "5610", "name": "Professional Services — Legal", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19",
     "notes": "1099-NEC for unincorporated counsel ≥ $600/yr."},
    {"number": "5620", "name": "Professional Services — CPA / Tax", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5630", "name": "Contractor · 1099-NEC", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},

    # Officer/Shareholder compensation (REQUIRED for S-Corp owner-operators)
    {"number": "5650", "name": "Officer/Shareholder Wages (W-2)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 7",
     "notes": "REQUIRED — S-Corp shareholders providing services must take reasonable compensation."},
    {"number": "5660", "name": "Salaries & Wages (non-officer)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 8"},
    {"number": "5670", "name": "Employee Benefits", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 18"},
    {"number": "5680", "name": "Payroll Taxes", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 12"},

    # Office / supplies / rent
    {"number": "5710", "name": "Office Supplies", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5720", "name": "Rent", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 11"},
    {"number": "5730", "name": "Utilities", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5740", "name": "Insurance", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},

    # Meals (§274 split)
    {"number": "5810", "name": "Meals — 50% Deductible", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19 (M-1 50% add-back)"},
    {"number": "5820", "name": "Meals — 100% Deductible (de minimis)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},

    # Travel
    {"number": "5910", "name": "Travel — Lodging", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},
    {"number": "5920", "name": "Travel — Transportation", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19"},

    # Marketing
    {"number": "5940", "name": "Marketing & Advertising", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 16"},

    # ------ 7xxx OTHER INCOME ------
    {"number": "7010", "name": "Interest Income", "type": "other_income",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120-S Schedule K Line 4"},

    # ------ 8xxx OTHER EXPENSE ------
    {"number": "8010", "name": "Interest Expense", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 13"},
    {"number": "8030", "name": "Depreciation Expense", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 14 / Form 4562"},

    {"number": "9990", "name": "Suspense — Uncategorized", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "NONE — must zero at close"},

    # ------ 9xxx TAX ------
    {"number": "9020", "name": "State Income Tax (entity-level)", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-S Line 19 (state)",
     "notes": "PTET elections route here. Most states impose entity-level tax via SALT-cap workaround."},
    {"number": "9030", "name": "Franchise / Privilege Tax", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "State"},
]


TEMPLATE_META: dict[str, Any] = {
    "kind": "s_corp",
    "description": (
        "S-Corporation (Form 1120-S). Pass-through entity — income flows to "
        "shareholder K-1s. Strong for owner-operators wanting payroll-tax savings "
        "on distributions vs salary. NOT eligible for §1202 QSBS exclusion."
    ),
    "min_entity_count": 1,
    "typical_use_cases": [
        "owner_operator", "professional_services", "smb_with_payroll",
        "established_smb",
    ],
    "industry_overrides": {
        "tech_saas": [
            {"number": "5240", "name": "SaaS · Analytics", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120-S Line 19"},
            {"number": "4050", "name": "Revenue · SaaS Subscriptions (MRR)", "type": "revenue",
             "normal_balance": "credit",
             "tax_form_line": "Form 1120-S Line 1a"},
        ],
        "consulting_services": [
            {"number": "5635", "name": "Subcontractor Services", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120-S Line 19"},
        ],
        "professional_services": [
            {"number": "4015", "name": "Revenue · Retainer / Subscription", "type": "revenue",
             "normal_balance": "credit",
             "tax_form_line": "Form 1120-S Line 1a"},
            {"number": "5645", "name": "Continuing Education", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120-S Line 19"},
        ],
    },
}
