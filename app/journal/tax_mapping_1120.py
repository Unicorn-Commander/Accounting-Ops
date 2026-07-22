"""Form 1120 (U.S. Corporation Income Tax Return) line mapping.

Maps the platform's canonical COA category strings (CATEGORY_TO_ACCOUNT keys
from app.journal.posting) and COA account-type buckets to IRS Form 1120
line numbers and Schedule L (Balance Sheet) line numbers.

All mappings reference the 2024 revision of Form 1120 and its instructions
(https://www.irs.gov/pub/irs-pdf/f1120.pdf,
 https://www.irs.gov/pub/irs-pdf/i1120.pdf).

IMPORTANT: This file encodes defensible defaults for CPA review. It is NOT
a substitute for professional tax advice. A CPA must review every line before
filing. Confidence levels:
  high   — statutory / definitional match; there is only one reasonable line.
  medium — the most common placement; alternative placements exist and the
            CPA should confirm based on the entity's facts.
  low    — the mapping is a placeholder; the category is broad enough that
            transactions within it could land on multiple lines. CPA must
            classify per-transaction before filing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Confidence = Literal["high", "medium", "low"]


# ---------------------------------------------------------------------------
# Core mapping dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class F1120Mapping:
    """A single COA category → Form 1120 line placement.

    Fields
    ------
    section : "income" | "deductions" | "tax" | "other_income" | "excluded"
        Which part of Form 1120 page 1 this lands on. ``"excluded"`` means the
        category does NOT belong on page 1 at all — e.g. balance-sheet equity
        (owner contributions/draws) which are not income or deductible expenses;
        they appear on Schedule L, not the income statement, and must be left
        OUT of any income/deduction total.
    line : str
        The 1120 line reference (e.g. "1a", "26", "30"). Lines with sub-parts
        use the lettered form matching the 2024 revision ("1a" = gross receipts,
        "2" = cost of goods sold, etc.).
    label : str
        The IRS line label verbatim from the 2024 Form 1120 instructions,
        truncated for readability.
    confidence : Confidence
        See module docstring.
    notes : str
        Rationale for the mapping + any CPA override guidance.
    """

    section: Literal["income", "deductions", "tax", "other_income", "excluded"]
    line: str
    label: str
    confidence: Confidence
    notes: str


# ---------------------------------------------------------------------------
# P&L / income-statement category mapping
# CATEGORY_TO_ACCOUNT keys from app.journal.posting — must stay in sync.
# ---------------------------------------------------------------------------

CATEGORY_TO_1120: dict[str, F1120Mapping] = {
    # ------------------------------------------------------------------
    # REVENUE
    # ------------------------------------------------------------------

    "Revenue · Services": F1120Mapping(
        section="income",
        line="1a",
        label="Gross receipts or sales",
        confidence="high",
        notes=(
            "Service revenue is gross receipts from the corporation's principal "
            "trade — the core of line 1a (Form 1120 instructions p. 8). This is "
            "the operating revenue of a services C-corp; without it line 1a is "
            "materially understated. Returns/allowances (if any) go on line 1b."
        ),
    ),

    "Revenue · Marketplace": F1120Mapping(
        section="income",
        line="1a",
        label="Gross receipts or sales",
        confidence="high",
        notes=(
            "Marketplace sales (eBay, Etsy, etc.) are gross receipts from the "
            "corporation's principal trade. Line 1a is the correct home. "
            "Returns/allowances go on line 1b; Form 1120 instructions p. 8. "
            "Note: if the entity also receives 1099-K forms, gross 1099-K "
            "amounts must reconcile to line 1a; CPA should verify."
        ),
    ),

    "Revenue · Music": F1120Mapping(
        section="income",
        line="1a",
        label="Gross receipts or sales",
        confidence="high",
        notes=(
            "Music royalty income is ordinary business income when received in "
            "the regular course of a music-licensing trade. Line 1a per "
            "Form 1120 instructions p. 8. "
            "If the entity is merely an investor in music rights (not actively "
            "exploiting them), a CPA should consider line 10 (other income) "
            "instead — but for an active music-licensing C-corp, 1a is correct."
        ),
    ),

    # ------------------------------------------------------------------
    # COGS (line 2, supported by Form 1125-A)
    # ------------------------------------------------------------------

    "COGS · Inventory": F1120Mapping(
        section="income",
        line="2",
        label="Cost of goods sold (Form 1125-A)",
        confidence="high",
        notes=(
            "Inventory cost of goods sold is reported on Form 1120 line 2 and "
            "computed on Form 1125-A. Although line 2 sits in the page-1 Income "
            "section, it is a subtractive amount used to calculate gross profit "
            "on line 3; it must not be treated as positive income or as a page-1 "
            "operating deduction. CPA should reconcile beginning inventory, "
            "purchases, and ending inventory to Form 1125-A."
        ),
    ),

    # ------------------------------------------------------------------
    # EXPENSES — Infrastructure
    # ------------------------------------------------------------------

    "Infrastructure · CDN": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "CDN costs (e.g. Cloudflare, Fastly) are a cost of running "
            "the business's technology infrastructure. There is no dedicated "
            "1120 line for hosting/CDN; line 26 (Other deductions, supported "
            "by an attached statement) is the standard home per 1120 "
            "instructions pp. 14-16. "
            "CPA should verify the statement descriptor: 'Content delivery "
            "network / hosting fees'. "
            "If CDN is directly tied to revenue delivery it could arguably "
            "flow through COGS (line 2/3) — CPA call."
        ),
    ),

    "Infrastructure · VPS": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "Virtual private server / cloud compute costs. Same analysis as "
            "Infrastructure · CDN. No dedicated 1120 line; line 26 with an "
            "attached statement labeled 'Server / cloud hosting fees'. "
            "If a material portion is directly attributable to revenue "
            "production, CPA may reclassify as COGS."
        ),
    ),

    "Infrastructure · Network": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "Network/internet expenses (e.g. Tailscale, dedicated circuits). "
            "Line 26 with statement descriptor 'Network / internet services'. "
            "Under IRS regulations, ordinary and necessary business "
            "communication costs are deductible; the form has no dedicated "
            "line for them — line 26 is correct."
        ),
    ),

    # ------------------------------------------------------------------
    # EXPENSES — SaaS
    # ------------------------------------------------------------------

    "SaaS · AI": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "AI API and model subscription costs (e.g. Anthropic, OpenAI). "
            "No specific 1120 line exists for AI tooling. Line 26 with "
            "statement label 'Artificial intelligence / software subscriptions'. "
            "If the AI cost is directly tied to generating revenue (e.g. "
            "powering a product sold to customers) the CPA should consider "
            "whether some or all of this belongs in COGS rather than "
            "Other deductions — that is a facts-and-circumstances call."
        ),
    ),

    "SaaS · Email": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "Email delivery platform costs (e.g. Postmark, SendGrid). "
            "Line 26 with statement label 'Email infrastructure / delivery'. "
            "Ordinary business communication expense — no dedicated 1120 line."
        ),
    ),

    "SaaS · Productivity": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "Business productivity software subscriptions (e.g. Google "
            "Workspace, Notion, Slack). "
            "IRS Pub. 535 classifies software subscriptions as ordinary "
            "business expenses. Most tax practitioners place miscellaneous "
            "software on line 26. An alternative is line 19 (Other costs, "
            "which relates to Form 1125-A), but that is only appropriate if "
            "the software is an input to manufacturing/COGS. For a services "
            "or tech company, line 26 is more defensible."
        ),
    ),

    # ------------------------------------------------------------------
    # EXPENSES — Contractor
    # ------------------------------------------------------------------

    "Contractor · 1099-NEC": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "Payments to independent contractors (1099-NEC recipients). "
            "The 1120 does NOT have a dedicated 'contract labor' line "
            "(unlike Schedule C which has line 11). The standard placement "
            "is line 26 (Other deductions) with an attached statement. "
            "Some preparers use line 14 (Depreciation) — DO NOT. Others "
            "use line 12 (Compensation of officers) — only applicable if "
            "the contractor is actually an officer. Line 26 with statement "
            "label 'Contract labor / 1099-NEC payments' is correct for "
            "non-officer independent contractors."
        ),
    ),

    # ------------------------------------------------------------------
    # EXPENSES — Office
    # ------------------------------------------------------------------

    "Office · Supplies": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "Office supply purchases. Form 1120 dropped the separate "
            "'Supplies' line from earlier versions; these now live on "
            "line 26. Statement label: 'Office supplies'. "
            "Under the de minimis safe harbor (Reg. §1.263(a)-1(f)), "
            "amounts ≤ $2,500 per item may be expensed immediately — "
            "this mapping assumes that election has been or will be made."
        ),
    ),

    # ------------------------------------------------------------------
    # EXPENSES — Travel & Meals
    # ------------------------------------------------------------------

    "Travel · Lodging": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "Business travel lodging costs. 1120 doesn't have a dedicated "
            "travel line (unlike Schedule C). Line 26 with statement label "
            "'Business travel — lodging'. "
            "Full deduction for ordinary/necessary travel under §162. "
            "No 50% haircut applies to lodging (only meals); CPA should "
            "confirm no personal-benefit issue."
        ),
    ),

    "Travel · Transport": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "Business travel transportation (airfare, train, rideshare). "
            "Line 26 with statement label 'Business travel — transportation'. "
            "Full deduction under §162. If vehicle use is involved, "
            "§179 / MACRS depreciation and the business-use percentage "
            "apply — CPA should separate vehicle from non-vehicle transport."
        ),
    ),

    "Vehicle · Repairs": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="medium",
        notes=(
            "Ordinary repair and maintenance costs for a business-use vehicle. "
            "Consistent with this module's convention for operating expenses, "
            "report the deductible business-use portion on line 26 with an "
            "attached statement labeled 'Vehicle repairs and maintenance'. "
            "A CPA may reclassify the amount to line 14 (Repairs and maintenance). "
            "Capital improvements, personal-use costs, and amounts reimbursable "
            "under an accountable plan require separate treatment."
        ),
    ),

    "Depreciation": F1120Mapping(
        section="deductions",
        line="20",
        label="Depreciation from Form 4562 not claimed on Form 1125-A or elsewhere on return",
        confidence="high",
        notes=(
            "Book depreciation posted to COA 8030 'Depreciation Expense'. Form "
            "1120 line 20 is the dedicated depreciation line and is the ONLY "
            "correct home for it — before this mapping existed the 8030 account "
            "fell through the unmapped-account catch-all to line 26 'Other "
            "deductions', which is the wrong line (and left line 20 blank on "
            "every C-corp return the platform produced).\n\n"
            "IMPORTANT — book vs TAX depreciation. The amount that belongs on "
            "line 20 is the TAX deduction computed on Form 4562 (MACRS + §179 + "
            "§168(k)), NOT the book depreciation expense in the ledger. Those "
            "differ for essentially every asset, and the difference is a "
            "Schedule M-1 reconciling item. The authoritative figure is Form "
            "4562 line 22, produced by app.tax.engines.depreciation; the ledger "
            "balance in 8030 is only a fallback when no fixed-asset records "
            "exist. See app/tax/compute_4562.py and docs/PROFESSIONAL-SUBSTITUTE.md.\n\n"
            "Depreciation capitalized into inventory under §263A is claimed on "
            "Form 1125-A (COGS) instead — line 20 is explicitly the depreciation "
            "'not claimed on Form 1125-A or elsewhere on return'."
        ),
    ),

    "Meals": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="high",
        notes=(
            "Business meal expenses. Under §274(n) (as amended by TCJA), "
            "business meals are 50% deductible. Our platform flags Meals "
            "for receipt-link proof before posting (IRS §274(d)). "
            "Line 26 with statement label 'Business meals (50% deductible)'. "
            "The platform should carry the GROSS amount to line 26 and "
            "include the 50% haircut note so the CPA can verify — some "
            "preparers prefer to book only the deductible 50% in the "
            "system; CPA should standardize the approach at engagement start. "
            "Confidence = high because 50% meals-to-line-26 is settled law."
        ),
    ),

    # ------------------------------------------------------------------
    # EXPENSES — Occupancy, payroll & insurance
    # ------------------------------------------------------------------

    "Rent · Facilities": F1120Mapping(
        section="deductions",
        line="16",
        label="Rents",
        confidence="high",
        notes=(
            "Rent paid for business premises / facilities. Form 1120 line 16 "
            "is the dedicated 'Rents' line. Statement label: 'Facility rent'. "
            "Confidence = high: dedicated line, settled placement. Rent paid to "
            "a related party (e.g. an owner-controlled entity) must be at fair "
            "market value — CPA should confirm §267 / reasonableness."
        ),
    ),

    "Utilities": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="high",
        notes=(
            "Electricity, gas, water, internet, and telephone for business "
            "premises. No dedicated 1120 line; line 26 with statement label "
            "'Utilities'. Confidence = high: settled practice."
        ),
    ),

    "Payroll · Wages": F1120Mapping(
        section="deductions",
        line="13",
        label="Salaries and wages (less employment credits)",
        confidence="high",
        notes=(
            "Wages and salaries paid to NON-officer employees. Form 1120 line "
            "13 is the dedicated line; it must be reported NET of any "
            "employment credits (e.g. Work Opportunity Credit). Officer "
            "compensation belongs on line 12 (Form 1125-E), NOT here — a CPA "
            "should confirm no owner-officer wages were mis-posted to 6350. "
            "Amounts capitalized into inventory under §263A go to Form 1125-A."
        ),
    ),

    "Insurance": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="high",
        notes=(
            "Business insurance premiums (general liability, property, E&O, "
            "commercial auto). No dedicated 1120 line; line 26 with statement "
            "label 'Insurance'. Employee health/benefit insurance instead "
            "belongs on line 24 (Employee benefit programs) — CPA should split "
            "if 6800 commingles employee-benefit premiums."
        ),
    ),

    # ------------------------------------------------------------------
    # EXPENSES — Banking
    # ------------------------------------------------------------------

    "Banking · Fees": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="high",
        notes=(
            "Bank service charges, wire fees, merchant processing fees not "
            "already netted in revenue. "
            "No dedicated 1120 line; line 26 is universally used for "
            "bank fees. Statement label: 'Bank and payment processing fees'. "
            "Confidence = high: this is settled practice with no "
            "alternative placements."
        ),
    ),

    # NOTE: "Banking · Interest" in CATEGORY_TO_ACCOUNT points to account 7010
    # (Interest Income — other_income). The sign at post time determines whether
    # it's income (positive → line 9) or an expense (negative → line 18).
    # This mapping covers the INCOME case; see INCOME_STATEMENT_OVERRIDES below
    # for context. The expense case is handled by the account-type routing.
    "Banking · Interest": F1120Mapping(
        section="other_income",
        line="5",
        label="Interest",
        confidence="high",
        notes=(
            "Bank interest received (positive amount). Form 1120 line 5 is "
            "'Interest' under the Income section (p. 1). "
            "If this category appears with a NEGATIVE amount (interest "
            "charged by the bank, not earned), it belongs on line 18 "
            "(Interest) under Deductions — the 1120 export engine must "
            "check the sign at export time and route accordingly. "
            "Confidence = high: line 5 is the only correct home for "
            "interest income on the 1120 page 1."
        ),
    ),

    # ------------------------------------------------------------------
    # EXPENSES — Inter-entity
    # ------------------------------------------------------------------

    "Inter-entity transfer": F1120Mapping(
        section="deductions",
        line="26",
        label="Other deductions",
        confidence="low",
        notes=(
            "Inter-entity transfers posted to the expense side (account 6900). "
            "These transactions are often ELIMINATIONS on a consolidated "
            "return or management fees between related entities. "
            "Confidence = low because the correct treatment depends heavily "
            "on facts: "
            "(a) If this is a management fee from a subsidiary, it may be "
            "deductible on line 26 with an arm's-length pricing analysis. "
            "(b) If this is an intercompany loan repayment, it is NOT "
            "deductible at all. "
            "(c) On a consolidated return the amounts may eliminate. "
            "CPA MUST classify each inter-entity transaction before filing. "
            "Line 26 is a placeholder to prevent silent drops — not an "
            "assertion of deductibility."
        ),
    ),

    # ------------------------------------------------------------------
    # EQUITY — owner contributions / draws (NOT on Form 1120 page 1)
    # ------------------------------------------------------------------

    "Owner's Contribution": F1120Mapping(
        section="excluded",
        line="—",
        label="Not on Form 1120 page 1 (balance-sheet equity)",
        confidence="high",
        notes=(
            "Capital the owner/shareholder pays INTO the corporation (account "
            "3010). This is a balance-sheet equity event — paid-in capital on "
            "Schedule L (line 22/23), NOT income. It must NOT be included in "
            "gross receipts or any income total on page 1. Excluded by design."
        ),
    ),

    "Owner's Draw": F1120Mapping(
        section="excluded",
        line="—",
        label="Not on Form 1120 page 1 (balance-sheet equity)",
        confidence="high",
        notes=(
            "Money paid OUT to the owner/shareholder or spent personally "
            "(account 3020). A distribution/draw is a reduction of equity on "
            "Schedule L, NOT a deductible business expense. It must NOT appear "
            "in any deduction total on page 1. For a C-corp a shareholder "
            "distribution may also implicate dividend treatment — CPA reviews. "
            "Excluded from page 1 by design."
        ),
    ),
}


# ---------------------------------------------------------------------------
# BALANCE SHEET — Schedule L line mapping
# Maps COA account_type values → Schedule L line numbers
# Schedule L of Form 1120 (2024 revision), lines 1–24.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScheduleLMapping:
    """A COA account type → Schedule L (Balance Sheet) line placement."""

    line: str
    label: str
    side: Literal["assets", "liabilities_equity"]
    confidence: Confidence
    notes: str


BALANCE_SHEET_1120: dict[str, ScheduleLMapping] = {
    # ------------------------------------------------------------------
    # ASSETS (Schedule L, lines 1–14)
    # Reference: Form 1120 Schedule L (2024 revision), assets section.
    # ------------------------------------------------------------------

    "asset_cash": ScheduleLMapping(
        line="1",
        label="Cash",
        side="assets",
        confidence="high",
        notes=(
            "Cash in bank accounts. Schedule L line 1. "
            "Covers COA accounts 1010–1020 (Primary/Secondary Checking, Savings). "
            "Begin-of-year and end-of-year balances are both required on "
            "Schedule L; the platform currently tracks end-of-period only."
        ),
    ),

    "asset_receivable": ScheduleLMapping(
        line="2a",
        label="Trade notes and accounts receivable",
        side="assets",
        confidence="high",
        notes=(
            "Accounts receivable from customers (COA 1100). "
            "Schedule L line 2a. Line 2b is the allowance for bad debts "
            "(contra); the platform does not currently track an allowance "
            "account — CPA should add one if material receivables exist."
        ),
    ),

    "asset_undeposited": ScheduleLMapping(
        line="6",
        label="Other current assets",
        side="assets",
        confidence="medium",
        notes=(
            "Undeposited funds / funds in transit (COA 1200). "
            "Schedule L line 6 (Other current assets) is the standard home "
            "for items that don't fit the named lines 1–5. "
            "CPA should verify these clear before the balance sheet date."
        ),
    ),

    "asset_prepaid": ScheduleLMapping(
        line="6",
        label="Other current assets",
        side="assets",
        confidence="medium",
        notes=(
            "Prepaid expenses (e.g. prepaid insurance, prepaid software, "
            "COA 1300). Schedule L line 6 (Other current assets). "
            "Alternative: if prepaid items relate to inventory (not typical "
            "for a services/tech entity) they could go on line 3."
        ),
    ),

    "asset_fixed": ScheduleLMapping(
        line="10a",
        label="Buildings and other depreciable assets",
        side="assets",
        confidence="medium",
        notes=(
            "Fixed assets — equipment, computers, furniture (COA 1500). "
            "Schedule L line 10a (gross cost) with accumulated depreciation "
            "on line 10b. If the entity has real estate (buildings), CPA "
            "should separate real vs personal property and use line 9 "
            "for land and line 10a for buildings/equipment."
        ),
    ),

    "asset_depreciation": ScheduleLMapping(
        line="10b",
        label="Less accumulated depreciation",
        side="assets",
        confidence="high",
        notes=(
            "Accumulated depreciation (contra-asset, COA 1550). "
            "Schedule L line 10b. Reported as a parenthetical deduction "
            "from line 10a to arrive at net book value."
        ),
    ),

    "asset_other": ScheduleLMapping(
        line="14",
        label="Other assets",
        side="assets",
        confidence="medium",
        notes=(
            "Catch-all for assets not fitting lines 1–13. "
            "Schedule L line 14. CPA should attach a supporting schedule "
            "itemizing the components."
        ),
    ),

    # ------------------------------------------------------------------
    # LIABILITIES (Schedule L, lines 16–21)
    # Reference: Form 1120 Schedule L (2024 revision), liabilities section.
    # Note: the 2024 revision renumbered some lines vs prior years —
    # line 16 = accounts payable, 17 = mortgages/notes < 1yr, 18 = other
    # current liabilities, 19 = loans from shareholders, 20 = long-term debt,
    # 21 = other liabilities.
    # ------------------------------------------------------------------

    "liability_payable": ScheduleLMapping(
        line="16",
        label="Accounts payable",
        side="liabilities_equity",
        confidence="high",
        notes=(
            "Trade accounts payable (COA 2010). Schedule L line 16. "
            "Amounts owed to vendors for goods/services received but not yet paid."
        ),
    ),

    "liability_accrued": ScheduleLMapping(
        line="18",
        label="Other current liabilities",
        side="liabilities_equity",
        confidence="medium",
        notes=(
            "Accrued expenses not separately called out (COA 2100). "
            "Schedule L line 18 (Other current liabilities). "
            "If accrued payroll is material, CPA may break it out; "
            "line 18 is the standard catch-all for current accruals."
        ),
    ),

    "liability_credit_card": ScheduleLMapping(
        line="18",
        label="Other current liabilities",
        side="liabilities_equity",
        confidence="medium",
        notes=(
            "Credit card balances outstanding (COA 2200). "
            "No dedicated Schedule L line for credit cards. "
            "Line 18 (Other current liabilities) is standard practice."
        ),
    ),

    "liability_sales_tax": ScheduleLMapping(
        line="18",
        label="Other current liabilities",
        side="liabilities_equity",
        confidence="medium",
        notes=(
            "Sales tax payable (COA 2300). Line 18. "
            "Some preparers use line 21 (Other liabilities) for tax payables; "
            "either is defensible — be consistent year-to-year."
        ),
    ),

    "liability_shareholder_loan": ScheduleLMapping(
        line="19",
        label="Loans from shareholders",
        side="liabilities_equity",
        confidence="high",
        notes=(
            "The 'Loan from Jason Allen' category is a loan from a shareholder "
            "recorded in COA 2400. Schedule L line 19 is the specific line for "
            "loans from shareholders, regardless of whether a non-shareholder "
            "note with the same maturity would otherwise use line 17 or 20. "
            "CPA should confirm the debt is bona fide and reconcile its terms "
            "and year-end balance; a capital contribution belongs on line 23."
        ),
    ),

    "liability_notes": ScheduleLMapping(
        line="20",
        label="Mortgages, notes, bonds payable in 1 year or more",
        side="liabilities_equity",
        confidence="medium",
        notes=(
            "Notes payable / long-term debt (COA 2400). "
            "Current portion (due within 12 months) → line 17 "
            "(Mortgages, notes, bonds payable in less than 1 year). "
            "Long-term portion → line 20. "
            "Platform does not track maturity dates; CPA must split "
            "current vs long-term and route to the correct lines."
        ),
    ),

    # ------------------------------------------------------------------
    # EQUITY (Schedule L, lines 22–28)
    # Reference: Form 1120 Schedule L (2024 revision), equity section.
    # ------------------------------------------------------------------

    "equity_capital": ScheduleLMapping(
        line="23",
        label="Additional paid-in capital",
        side="liabilities_equity",
        confidence="medium",
        notes=(
            "The 'Owner Contribution' category records a C-corporation "
            "shareholder contribution in COA 3010. It is additional paid-in "
            "capital on Schedule L line 23, not page-1 income. CPA should "
            "reconcile the contribution to the cap table and separate any "
            "stated par value to common or preferred stock on line 22b or 22a."
        ),
    ),

    "equity_distributions": ScheduleLMapping(
        line="25",
        label="Retained earnings — unappropriated",
        side="liabilities_equity",
        confidence="low",
        notes=(
            "The 'Owner Distribution' category records shareholder "
            "distributions in COA 3020. "
            "Distributions reduce retained earnings but do NOT appear as a "
            "standalone line on Schedule L — they flow through the retained "
            "earnings reconciliation (line 25). CPA must prepare an equity "
            "rollforward: opening RE + net income − distributions = "
            "closing RE (line 25). "
            "Confidence = low: direct line assignment for distributions is "
            "not straightforward; CPA reconciliation is required."
        ),
    ),

    "equity_retained": ScheduleLMapping(
        line="25",
        label="Retained earnings — unappropriated",
        side="liabilities_equity",
        confidence="high",
        notes=(
            "Retained earnings (COA 3050). Schedule L line 25. "
            "Must equal prior-year line 25 + current-year net income "
            "(Form 1120 line 30) − distributions. CPA reconciles."
        ),
    ),
}


# CATEGORY_TO_ACCOUNT keys that are balance-sheet activity, intentionally
# excluded from the Form 1120 page-1 P&L package. Each category has an explicit
# route to a Schedule L bucket above so exclusion can never become a silent drop.
BALANCE_SHEET_CATEGORY_TO_KEY: dict[str, str] = {
    "Owner Contribution": "equity_capital",
    "Owner Distribution": "equity_distributions",
    "Loan from Jason Allen": "liability_shareholder_loan",
}

NON_1120_PL_CATEGORIES: frozenset[str] = frozenset(BALANCE_SHEET_CATEGORY_TO_KEY)


# ---------------------------------------------------------------------------
# COA account-type → simplified bucket key used in BALANCE_SHEET_1120
# (maps the CoaTemplate.account_type values to the keys above)
# ---------------------------------------------------------------------------

ACCOUNT_TYPE_TO_BALANCE_SHEET_KEY: dict[str, str | None] = {
    # Asset sub-buckets are determined by account number prefix at export time;
    # this dict provides the fallback bucket per account_type.
    "asset":         "asset_other",       # default; exporter refines by acct #
    "liability":     "liability_payable", # default; exporter refines by acct #
    "equity":        "equity_retained",   # default; exporter refines by acct #
    # income-statement types don't appear on Schedule L directly:
    "revenue":       None,
    "cogs":          None,
    "expense":       None,
    "other_income":  None,
    "other_expense": None,
    "tax":           None,
}


# ---------------------------------------------------------------------------
# Valid 1120 line references (2024 revision)
# Used by tests to assert no mapping references a non-existent line.
# Source: Form 1120 (2024), page 1 Income section lines 1a–11,
# Deductions lines 12–29, Tax and Payments lines 30–34.
# ---------------------------------------------------------------------------

VALID_1120_LINES: frozenset[str] = frozenset({
    # Income (page 1, lines 1–11)
    "1a", "1b", "1c",   # Gross receipts, returns, net
    "2",                 # Cost of goods sold (Form 1125-A)
    "3",                 # Gross profit
    "4",                 # Dividends and inclusions (Schedule C)
    "5",                 # Interest income
    "6",                 # Gross rents
    "7",                 # Gross royalties
    "8",                 # Capital gain net income (Schedule D)
    "9",                 # Net gain or (loss) from Form 4797
    "10",                # Other income (attach statement)
    "11",                # Total income (add lines 3-10)
    # Deductions (page 1, lines 12–29)
    "12",   # Compensation of officers (Form 1125-E)
    "13",   # Salaries and wages
    "14",   # Repairs and maintenance
    "15",   # Bad debts
    "16",   # Rents
    "17",   # Taxes and licenses
    "18",   # Interest (see instructions)
    "19",   # Charitable contributions
    "20",   # Depreciation from Form 4562
    "21",   # Depletion
    "22",   # Advertising
    "23",   # Pension, profit-sharing, etc., plans
    "24",   # Employee benefit programs
    "25",   # Reserved for future use
    "26",   # Other deductions (attach statement)
    "27",   # Total deductions
    "28",   # Taxable income before NOL deduction and special deductions
    "29a",  # NOL deduction
    "29b",  # Special deductions (Schedule C)
    "29c",  # Total
    # Tax, refundable credits, and payments (lines 30–34)
    "30",   # Taxable income
    "31",   # Total tax (Schedule J, Part I, line 11)
    "32",   # Reserved
    "33",   # Total payments and credits (Schedule J, Part II, line 21)
    "34",   # Estimated tax penalty
    "35",   # Amount owed
    "36",   # Overpayment
    "37",   # Credited to next year's estimated tax
    "38",   # Refunded
})

# Valid Schedule L line references (2024 Form 1120 revision)
# Source: Form 1120, Schedule L (Balance Sheet per Books), 2024 revision.
# Lines 1–15 are assets; lines 16–28 are liabilities + equity.
VALID_SCHEDULE_L_LINES: frozenset[str] = frozenset({
    # Assets (lines 1–15)
    "1",     # Cash
    "2a",    # Trade notes and accounts receivable
    "2b",    # Less allowance for bad debts
    "3",     # Inventories
    "4",     # U.S. government obligations
    "5",     # Tax-exempt securities
    "6",     # Other current assets
    "7",     # Loans to shareholders
    "8",     # Mortgage and real estate loans
    "9",     # Other investments
    "10a",   # Buildings and other depreciable assets
    "10b",   # Less accumulated depreciation
    "11a",   # Depletable assets
    "11b",   # Less accumulated depletion
    "12",    # Land (net of any amortization)
    "13a",   # Intangible assets (amortizable only)
    "13b",   # Less accumulated amortization
    "14",    # Other assets
    "15",    # Total assets
    # Liabilities and stockholders' equity (lines 16–28)
    "16",    # Accounts payable
    "17",    # Mortgages, notes, bonds payable in less than 1 year
    "18",    # Other current liabilities
    "19",    # Loans from shareholders
    "20",    # Mortgages, notes, bonds payable in 1 year or more
    "21",    # Other liabilities
    "22a",   # Capital stock: preferred stock
    "22b",   # Capital stock: common stock
    "23",    # Additional paid-in capital
    "24",    # Retained earnings — appropriated
    "25",    # Retained earnings — unappropriated
    "26",    # Adjustments to shareholders' equity
    "27",    # Less cost of treasury stock
    "28",    # Total liabilities and stockholders' equity
})
