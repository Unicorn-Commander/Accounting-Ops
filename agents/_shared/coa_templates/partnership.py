"""General/Limited Partnership chart of accounts (Form 1065 filer).

Partnerships and multi-member LLCs both file Form 1065, so this template is
nearly identical to ``llc.py``. The differences are mostly legal (recourse
debt allocations differ for general partners vs LP).

For most users, the Setup Assistant should recommend ``llc`` instead unless
the entity is a true partnership (e.g., a law firm in a state that requires
PLLP, or an investment LP). LLCs offer broader liability protection.
"""
from __future__ import annotations

from agents._shared.coa_templates.llc import ACCOUNTS as LLC_ACCOUNTS


# Partnerships use the same Form 1065 framework as multi-member LLCs.
# We reuse the LLC account list verbatim and rename equity accounts to reflect
# partner vs member nomenclature.
ACCOUNTS = []
for _acct in LLC_ACCOUNTS:
    acct = dict(_acct)
    # Re-label member -> partner in equity-account names + notes.
    if acct.get("type") == "equity":
        acct["name"] = (
            acct["name"]
            .replace("Member ", "Partner ")
            .replace("Member's", "Partners'")
            .replace("Members'", "Partners'")
            .replace("Member ", "Partner ")
        )
    if "Members'" in (acct.get("name") or ""):
        acct["name"] = acct["name"].replace("Members'", "Partners'")
    if "Loans from Members" in (acct.get("name") or ""):
        acct["name"] = acct["name"].replace("Loans from Members", "Loans from Partners")
    notes = acct.get("notes")
    if isinstance(notes, str):
        acct["notes"] = (
            notes.replace("member", "partner")
            .replace("Member", "Partner")
            .replace("§752 basis allocations", "§752 basis allocations (recourse vs nonrecourse — general vs limited partner)")
        )
    ACCOUNTS.append(acct)


# Override the §704(b) allocation note for guaranteed payments
for _acct in ACCOUNTS:
    if _acct["number"] in ("5100", "5110"):
        _acct["notes"] = (
            _acct.get("notes", "")
            + " For partnerships, distinguish general partner (active) from "
            "limited partner (passive) treatment under §469 PAL rules."
        )


TEMPLATE_META = {
    "kind": "partnership",
    "description": (
        "General/Limited Partnership (Form 1065). Same tax form as multi-member "
        "LLC but with partner vs member nomenclature and recourse-debt "
        "allocation differences (general vs limited partner). For most users, "
        "an LLC offers broader liability protection — use this template only "
        "when the legal form is a true partnership."
    ),
    "min_entity_count": 1,
    "typical_use_cases": [
        "true_partnership", "investment_lp", "pllp_law_firm",
        "joint_venture_general_partnership",
    ],
    "industry_overrides": {
        "investment_partnership": [
            {"number": "7030", "name": "Capital Gain — Short-term", "type": "other_income",
             "normal_balance": "credit",
             "tax_form_line": "Form 1065 Schedule K Line 8"},
            {"number": "7040", "name": "Capital Gain — Long-term", "type": "other_income",
             "normal_balance": "credit",
             "tax_form_line": "Form 1065 Schedule K Line 9a"},
            {"number": "7050", "name": "Carried Interest", "type": "other_income",
             "normal_balance": "credit",
             "tax_form_line": "Form 1065 Schedule K — special allocation",
             "notes": "§1061 3-year holding requirement for LTCG treatment."},
        ],
        "professional_practice": [
            {"number": "5645", "name": "Continuing Education", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1065 Line 20"},
            {"number": "5750", "name": "Malpractice Insurance", "type": "expense",
             "normal_balance": "debit",
             "tax_form_line": "Form 1065 Line 20"},
        ],
    },
}
