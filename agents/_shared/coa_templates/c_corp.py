"""C-Corporation chart-of-accounts template (Form 1120 filer).

Modeled on the production Acme LLC C-Corp catalog
(``agents/_shared/coa_catalogs/mu_inc_c_corp.md``) which has 76 accounts.
This module exposes the generic 46-account system baseline + selected
entity-helpful refinements (R&D split, meals §274 split, payment processor
split) as the C-Corp default.

Industry overrides add small numbers of accounts on top.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Base C-Corp accounts (form 1120)
# ---------------------------------------------------------------------------

ACCOUNTS: list[dict[str, Any]] = [
    # ------ 1xxx ASSETS ------
    {"number": "1010", "name": "Cash · Primary Checking", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 1",
     "notes": "Primary operating bank account. Connect to a bank feed via /connectors."},
    {"number": "1011", "name": "Cash · Secondary Checking", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 1",
     "notes": "Optional second operating account."},
    {"number": "1020", "name": "Cash · Savings", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 1"},
    {"number": "1100", "name": "Accounts Receivable", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 2a"},
    {"number": "1200", "name": "Undeposited Funds", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 1"},
    {"number": "1210", "name": "Stripe Receivable", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 2a",
     "notes": "Payouts in flight (Stripe holds 2-7 days)."},
    {"number": "1220", "name": "eBay Payouts Receivable", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 2a",
     "notes": "eBay funds earned but not yet swept to bank. Mirror of Stripe Receivable for the eBay channel; settles when the /payout API record posts."},
    {"number": "1300", "name": "Prepaid Expenses", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 6"},
    {"number": "1500", "name": "Fixed Assets", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 10a"},
    {"number": "1550", "name": "Accumulated Depreciation", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 10b",
     "notes": "Contra-asset; cumulative §168/§179 depreciation."},

    # ------ 2xxx LIABILITIES ------
    {"number": "2010", "name": "Accounts Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 16"},
    {"number": "2100", "name": "Accrued Expenses", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 18"},
    {"number": "2200", "name": "Credit Card", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 17"},
    {"number": "2300", "name": "Sales Tax Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 17"},
    {"number": "2310", "name": "Accrued Professional Fees", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 18"},
    {"number": "2400", "name": "Notes Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 17"},
    {"number": "2500", "name": "Loans from Shareholders", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 19",
     "notes": "DEBT (not equity) per IRC §385. Must have written note + AFR rate."},

    # ------ 3xxx EQUITY ------
    {"number": "3010", "name": "Common Stock", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 22"},
    {"number": "3020", "name": "Additional Paid-in Capital", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 23"},
    {"number": "3050", "name": "Retained Earnings", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Schedule L Line 25 / Schedule M-2"},
    {"number": "3100", "name": "Treasury Stock", "type": "equity",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Schedule L Line 27",
     "notes": "Contra-equity; stock the corp has repurchased."},

    # ------ 4xxx REVENUE ------
    {"number": "4010", "name": "Revenue · Services", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Line 1a"},
    {"number": "4020", "name": "Revenue · Products", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Line 1a"},
    {"number": "4030", "name": "Revenue · Subscriptions", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Line 1a"},
    {"number": "4040", "name": "Revenue · Royalties", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Line 6"},
    {"number": "4150", "name": "eBay Resale Revenue", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Line 1a",
     "notes": "Gross sales captured by the eBay Sell-Finances connector. Per-transaction SALE rows credit here; FEEs flow to 5520; net settles to 1220 then 1010 on payout."},
    {"number": "4090", "name": "Revenue · Other", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Line 10"},

    # ------ 5xxx COGS ------
    {"number": "5010", "name": "COGS · Inventory", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 2 / Schedule A"},
    {"number": "5020", "name": "COGS · Marketplace Fees", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 2"},
    {"number": "5030", "name": "COGS · Shipping", "type": "cogs",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 2"},

    # ------ 5xxx OPERATING EXPENSES (refined) ------
    {"number": "5210", "name": "SaaS · AI · Production", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26 (Other deductions)",
     "notes": "AI APIs serving customer-facing output. Potential §41 R&D credit candidate."},
    {"number": "5215", "name": "SaaS · AI · Productivity", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26",
     "notes": "Internal AI tools. Ordinary §162 — NOT §41-eligible."},
    {"number": "5220", "name": "SaaS · Dev Tools", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "5225", "name": "SaaS · Productivity & Office", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "5230", "name": "SaaS · Communications", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "5310", "name": "Cloud Infrastructure", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "5320", "name": "Domains & DNS", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},

    # R&D — §174 capitalize-and-amortize candidates
    {"number": "5410", "name": "R&D — Materials & Cloud", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26 (M-1 add-back for §174)",
     "notes": "§174 candidate — capitalize and amortize over 5y US / 15y foreign."},
    {"number": "5420", "name": "R&D — Software Licenses", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26",
     "notes": "§174 capitalize-and-amortize for research-related licenses."},

    # Banking & processors
    {"number": "5510", "name": "Banking Fees", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "5520", "name": "Payment Processor Fees", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},

    # Professional services
    {"number": "5610", "name": "Professional Services — Legal", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26",
     "notes": "1099-NEC reportable for unincorporated counsel ≥ $600/yr."},
    {"number": "5620", "name": "Professional Services — CPA / Tax", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26",
     "notes": "1099-NEC for sole-prop/LLC providers (incorporated firms generally exempt)."},
    {"number": "5630", "name": "Contractor · 1099-NEC", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},

    # Officer compensation
    {"number": "5650", "name": "Officer Compensation", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 12 / Form 1125-E"},
    {"number": "5660", "name": "Salaries & Wages (non-officer)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 13"},
    {"number": "5670", "name": "Employee Benefits", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 24"},
    {"number": "5680", "name": "Payroll Taxes", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 17"},

    # Office / supplies / rent
    {"number": "5710", "name": "Office Supplies", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "5720", "name": "Rent", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 16"},
    {"number": "5730", "name": "Utilities", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "5740", "name": "Insurance", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},

    # Meals (§274 split)
    {"number": "5810", "name": "Meals — 50% Deductible", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26 (M-1 50% add-back)",
     "notes": "IRC §274(n)(1). Receipt + business purpose required regardless of amount."},
    {"number": "5820", "name": "Meals — 100% Deductible (de minimis)", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26",
     "notes": "IRC §274(n)(2) narrow exceptions only. Use rarely."},

    # Travel
    {"number": "5910", "name": "Travel — Lodging", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "5920", "name": "Travel — Transportation", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "5930", "name": "Travel — Per Diem", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26",
     "notes": "Not available to >10% shareholders — actuals only for those."},

    # Marketing
    {"number": "5940", "name": "Marketing & Advertising", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 22"},

    # Inter-entity
    {"number": "5990", "name": "Inter-entity Transfer · Out", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 (CPA-classified)",
     "notes": "ESCALATE: CPA must classify (mgmt fee / loan / equity)."},

    # ------ 7xxx OTHER INCOME ------
    {"number": "7010", "name": "Interest Income", "type": "other_income",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Line 5"},
    {"number": "7020", "name": "FX Gain", "type": "other_income",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Line 10"},
    {"number": "7900", "name": "Inter-entity Transfer · In", "type": "other_income",
     "normal_balance": "credit",
     "tax_form_line": "Form 1120 Line 10 (CPA-classified)"},

    # ------ 8xxx OTHER EXPENSE ------
    {"number": "8010", "name": "Interest Expense", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 18"},
    {"number": "8020", "name": "FX Loss", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 26"},
    {"number": "8030", "name": "Depreciation Expense", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 20 / Form 4562"},

    # Non-deductible + suspense
    {"number": "9510", "name": "Credit Card Principal (NON-DEDUCTIBLE)", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "NONE — Schedule M-1 add-back",
     "notes": "Catch bucket for misclassified card principal repayments. Must zero at close."},
    {"number": "9990", "name": "Suspense — Uncategorized", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "NONE — must be zero at close",
     "notes": "Temporary parking. Non-zero balance blocks 1120 filing."},

    # ------ 9xxx TAX ------
    {"number": "9010", "name": "Federal Income Tax Expense", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 31"},
    {"number": "9020", "name": "State Income Tax Expense", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 17 (state)"},
    {"number": "9030", "name": "Franchise / Privilege Tax", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120 Line 17"},
    {"number": "9050", "name": "Federal Estimated Tax", "type": "tax",
     "normal_balance": "debit",
     "tax_form_line": "Form 1120-W"},
]


# ---------------------------------------------------------------------------
# Template metadata + industry overrides (additive)
# ---------------------------------------------------------------------------

TEMPLATE_META: dict[str, Any] = {
    "kind": "c_corp",
    "description": (
        "C-Corporation (Form 1120). Strong for raising venture capital, "
        "issuing classes of stock, and §1202 QSBS-eligible founders. "
        "Double taxation applies (corp + dividends)."
    ),
    "min_entity_count": 1,
    "typical_use_cases": [
        "startup", "tech_business", "operating_company",
        "venture_backed", "qsbs_eligible",
    ],
    "industry_overrides": {
        "tech_saas": [
            {"number": "5240", "name": "SaaS · Analytics", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120 Line 26"},
            {"number": "5250", "name": "SaaS · Customer Support", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120 Line 26"},
            {"number": "4050", "name": "Revenue · SaaS Subscriptions (MRR)", "type": "revenue",
             "normal_balance": "credit",
             "tax_form_line": "Form 1120 Line 1a",
             "notes": "Recurring revenue. Watch §451 advance-payment rules for deferred recognition."},
        ],
        "consulting_services": [
            {"number": "5635", "name": "Subcontractor Services", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120 Line 26"},
            {"number": "5640", "name": "Client Pass-through Expenses", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120 Line 26"},
        ],
        "ecommerce": [
            {"number": "5040", "name": "COGS · Returns & Refunds", "type": "cogs",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120 Line 2"},
            {"number": "5050", "name": "COGS · Fulfillment Fees", "type": "cogs",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120 Line 2"},
            {"number": "5060", "name": "COGS · Storage / Warehousing", "type": "cogs",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120 Line 2"},
        ],
        "ai_infrastructure": [
            {"number": "4200", "name": "Revenue · AI Infrastructure", "type": "revenue",
             "normal_balance": "credit",
             "tax_form_line": "Form 1120 Line 1a",
             "notes": "Managed AI / inference / training revenue stream."},
            {"number": "5430", "name": "R&D — Model Training Compute", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1120 Line 26",
             "notes": "§174 candidate."},
        ],
    },
}
