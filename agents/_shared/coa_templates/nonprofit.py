"""Nonprofit / 501(c)(3) chart of accounts (Form 990 filer).

Nonprofit accounting differs structurally from for-profit:
  - Net Assets without/with donor restrictions (FASB ASC 958)
  - Functional expense categories (program / management / fundraising)
  - Contribution revenue (cash, non-cash, in-kind)
  - No equity in the traditional sense

This is a SMALL starter chart — full Form 990 prep typically needs additional
program-specific revenue and expense categories. The Setup Assistant should
flag this for Mosin CPA review during nonprofit setup.
"""
from __future__ import annotations

from typing import Any


ACCOUNTS: list[dict[str, Any]] = [
    # ------ 1xxx ASSETS ------
    {"number": "1010", "name": "Cash · Operating Checking", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part X Line 1"},
    {"number": "1020", "name": "Cash · Savings (Reserve)", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part X Line 1"},
    {"number": "1030", "name": "Cash · Restricted (Donor)", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part X Line 1",
     "notes": "Funds held subject to donor restrictions. Track separately for stewardship."},
    {"number": "1100", "name": "Pledges Receivable (current)", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part X Line 3"},
    {"number": "1110", "name": "Grants Receivable", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part X Line 4"},
    {"number": "1500", "name": "Fixed Assets", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part X Line 10a"},
    {"number": "1550", "name": "Accumulated Depreciation", "type": "asset",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part X Line 10b"},

    # ------ 2xxx LIABILITIES ------
    {"number": "2010", "name": "Accounts Payable", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part X Line 17"},
    {"number": "2100", "name": "Accrued Expenses", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part X Line 18"},
    {"number": "2200", "name": "Credit Card", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part X Line 17"},
    {"number": "2300", "name": "Payroll Liabilities", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part X Line 19"},
    {"number": "2400", "name": "Deferred Revenue (Conditional Promises)", "type": "liability",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part X Line 19"},

    # ------ 3xxx NET ASSETS (FASB ASC 958) ------
    {"number": "3010", "name": "Net Assets — Without Donor Restrictions", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part X Line 27"},
    {"number": "3020", "name": "Net Assets — With Donor Restrictions (Time/Purpose)", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part X Line 28"},
    {"number": "3030", "name": "Net Assets — With Donor Restrictions (Permanent)", "type": "equity",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part X Line 28",
     "notes": "Endowment principal — corpus must be maintained per donor restriction."},

    # ------ 4xxx REVENUE (contributions + program + investment) ------
    {"number": "4010", "name": "Contributions — Cash (Unrestricted)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Line 1f"},
    {"number": "4011", "name": "Contributions — Cash (Restricted)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Line 1f"},
    {"number": "4020", "name": "Contributions — Non-cash (In-kind)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part VIII Line 1g"},
    {"number": "4030", "name": "Grants — Government", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part VIII Line 1e"},
    {"number": "4040", "name": "Grants — Foundation", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part VIII Line 1c"},
    {"number": "4050", "name": "Program Service Revenue", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part VIII Line 2"},
    {"number": "4060", "name": "Membership Dues", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part VIII Line 1c or 2 (depending on substance)"},
    {"number": "4070", "name": "Investment Income", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part VIII Line 3-4"},
    {"number": "4080", "name": "Fundraising Event Revenue (Gross)", "type": "revenue",
     "normal_balance": "credit",
     "tax_form_line": "Form 990 Part VIII Line 8a"},

    # ------ 5xxx PROGRAM EXPENSES ------
    {"number": "5100", "name": "Program — Services", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Column B (Program services)"},
    {"number": "5110", "name": "Program — Materials & Supplies", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col B"},
    {"number": "5120", "name": "Program — Salaries & Wages", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col B"},

    # ------ 6xxx MANAGEMENT & GENERAL ------
    {"number": "6100", "name": "M&G — Officer/Director Compensation", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col C / Schedule J"},
    {"number": "6110", "name": "M&G — Salaries & Wages", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col C"},
    {"number": "6200", "name": "M&G — Professional Services", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col C"},
    {"number": "6300", "name": "M&G — Office Expenses", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col C"},
    {"number": "6400", "name": "M&G — Insurance", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col C"},
    {"number": "6500", "name": "M&G — Rent & Utilities", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col C"},
    {"number": "6600", "name": "M&G — Technology & SaaS", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col C"},

    # ------ 7xxx FUNDRAISING ------
    {"number": "7100", "name": "FR — Fundraising Salaries", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col D"},
    {"number": "7200", "name": "FR — Event Direct Expenses", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part VIII Line 8b"},
    {"number": "7300", "name": "FR — Advertising & Promotion", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col D"},
    {"number": "7400", "name": "FR — Donor Cultivation", "type": "expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Col D"},

    # ------ 8xxx OTHER ------
    {"number": "8030", "name": "Depreciation Expense", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "Form 990 Part IX Line 22"},
    {"number": "9990", "name": "Suspense — Uncategorized", "type": "other_expense",
     "normal_balance": "debit",
     "tax_form_line": "NONE — must zero at close"},
]


TEMPLATE_META: dict[str, Any] = {
    "kind": "nonprofit",
    "description": (
        "Nonprofit / 501(c)(3) (Form 990). Net Assets with vs without donor "
        "restrictions per FASB ASC 958. Functional expense categories "
        "(program / management / fundraising). CPA REVIEW STRONGLY ADVISED — "
        "nonprofit accounting has many edge cases this starter chart does "
        "not cover (split-interest agreements, endowment investment income "
        "allocation, UBI, joint costs)."
    ),
    "min_entity_count": 1,
    "typical_use_cases": [
        "501c3_small", "community_foundation", "fiscal_sponsor",
        "religious_organization", "membership_org",
    ],
    "industry_overrides": {
        "501c3_arts": [
            {"number": "4090", "name": "Ticket Revenue", "type": "revenue",
             "normal_balance": "credit",
             "tax_form_line": "Form 990 Part VIII Line 2"},
            {"number": "5130", "name": "Program — Artist Fees & Royalties", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 990 Part IX Col B"},
        ],
        "501c3_education": [
            {"number": "4091", "name": "Tuition & Fees", "type": "revenue",
             "normal_balance": "credit",
             "tax_form_line": "Form 990 Part VIII Line 2"},
            {"number": "5140", "name": "Program — Scholarships & Awards", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 990 Part IX Line 3"},
        ],
    },
}
