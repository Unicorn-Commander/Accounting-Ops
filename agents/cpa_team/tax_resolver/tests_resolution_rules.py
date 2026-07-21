"""Unit tests for resolution_rules.resolve_item.

Pure-Python, no I/O, no DB, no LLM. Run with:

    python -m pytest agents/cpa_team/tax_resolver/tests_resolution_rules.py -v

Or from inside the container:

    python /app/agents/cpa_team/tax_resolver/tests_resolution_rules.py

The test module is self-contained so it can also be executed as a plain script
(the bottom ``if __name__ == "__main__"`` block counts pass/fail and exits).
"""
from __future__ import annotations

import sys
import traceback
from typing import Any

from agents.cpa_team.tax_resolver.resolution_rules import resolve_item


# ---------------------------------------------------------------------------
# Minimal item factories
# ---------------------------------------------------------------------------


def _item(
    item_type: str,
    form_reference: str = "",
    content: Any = "",
    confidence: str = "medium",
    status: str = "pending",
) -> dict[str, Any]:
    return {
        "id": "test-uuid",
        "item_type": item_type,
        "form_reference": form_reference,
        "content": content,
        "confidence": confidence,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_PASS = []
_FAIL = []


def _test(name: str, fn):
    try:
        fn()
        _PASS.append(name)
        print(f"  PASS  {name}")
    except AssertionError as exc:
        _FAIL.append(name)
        print(f"  FAIL  {name}")
        traceback.print_exc(limit=3)


# ===========================================================================
# form_line_proposal tests
# ===========================================================================


def test_flp_high_resolved():
    r = resolve_item(_item("form_line_proposal", "Form 1120, Line 5", confidence="high"))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FLP-HIGH"
    assert not r["skip"]


def test_flp_medium_resolved():
    r = resolve_item(_item("form_line_proposal", "Form 1120, Line 26", confidence="medium"))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FLP-MED"
    assert "deterministic rule" in r["notes"]


def test_flp_low_escalates():
    r = resolve_item(_item("form_line_proposal", "Form 1120, Line 26", confidence="low"))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "FLP-LOW"
    assert not r["skip"]


# ===========================================================================
# flagged_position — Banking · Interest
# ===========================================================================


def test_fp_banking_interest_income():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Line 5",
        content={"issue": "Banking · Interest income positive — $412", "context": "received credit"},
        confidence="high",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-BANKING-INTEREST"
    assert "Line 5" in r["notes"]


def test_fp_banking_interest_expense():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Line 18",
        content={"issue": "§163(j) interest expense negative", "context": "deductible paid"},
        confidence="medium",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-BANKING-INTEREST"
    assert "Line 18" in r["notes"]


def test_fp_163j_content_trigger():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Line 18",
        content="§163(j) limitation analysis required if any interest expense",
        confidence="high",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-BANKING-INTEREST"


# ===========================================================================
# flagged_position — SaaS · AI
# ===========================================================================


def test_fp_saas_ai_default_line26():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Line 26 / Form 1125-A",
        content={"issue": "SaaS · AI expenses $1,240 placed on line 26"},
        confidence="medium",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-SAAS-AI-LINE26"
    assert "Line 26" in r["notes"]


def test_fp_saas_ai_cogs_tagged():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1125-A",
        content={"issue": "SaaS · AI COGS-eligible — tied to customer deliverable"},
        confidence="high",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-SAAS-AI-COGS"
    assert "Form 1125-A" in r["notes"]


# ===========================================================================
# flagged_position — Inter-entity transfer (always escalate)
# ===========================================================================


def test_fp_inter_entity_escalates():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Line 26",
        content={"issue": "Inter-entity transfer from MU LLC — classification needed"},
        confidence="high",
    ))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "FP-INTER-ENTITY"
    assert not r["skip"]


def test_fp_management_fee_escalates():
    r = resolve_item(_item(
        "flagged_position",
        content="management fee to related party entity",
        confidence="medium",
    ))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "FP-INTER-ENTITY"


# ===========================================================================
# flagged_position — Contractor · 1099-NEC
# ===========================================================================


def test_fp_1099_officer_escalates():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Line 12 / Form 1125-E",
        content={"issue": "Example Owner (CEO / officer) paid $18,000 via 1099-NEC"},
        confidence="high",
    ))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "FP-1099-OFFICER"


def test_fp_1099_nonoffice_resolved():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Line 26",
        content={"issue": "Contractor John Smith paid $3,500 via 1099-NEC. Not an officer."},
        confidence="high",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-1099-NONOFF"


# ===========================================================================
# flagged_position — SC1120 FTF / SCDOR
# ===========================================================================


def test_fp_sc_ftf_reviewed():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="SC1120, Failure-to-File",
        content={"issue": "Failure to file SC1120 for 2023 — SCDOR notice received"},
        confidence="high",
    ))
    assert r["decision"] == "reviewed", r
    assert r["rule_id"] == "FP-SC-FTF"


def test_fp_scdor_vda_reviewed():
    r = resolve_item(_item(
        "flagged_position",
        content="VDA analysis: voluntary disclosure available for 2021-2023",
        confidence="medium",
    ))
    assert r["decision"] == "reviewed", r
    assert r["rule_id"] == "FP-SC-FTF"


# ===========================================================================
# flagged_position — §382 ownership change (always escalate)
# ===========================================================================


def test_fp_382_escalates():
    r = resolve_item(_item(
        "flagged_position",
        content={"issue": "§382 ownership-change analysis required — investor round Q2"},
        confidence="high",
    ))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "FP-382"


def test_fp_nol_limitation_382_escalates():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="§382",
        content="382 limitation may apply; NOL carryforward at risk",
        confidence="medium",
    ))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "FP-382"


# ===========================================================================
# flagged_position — Schedule L reconstruction
# ===========================================================================


def test_fp_schedule_l_resolved():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Schedule L, Line 22a",
        content={"issue": "Schedule L reconstruction — best-effort from journal"},
        confidence="medium",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-SCHED-L"


# ===========================================================================
# flagged_position — Form 1125-E threshold
# ===========================================================================


def test_fp_1125e_below_threshold():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1125-E",
        content={"issue": "Form 1125-E threshold check", "context": "pre-revenue year, not required"},
        confidence="medium",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-1125E-BELOW"


def test_fp_1125e_above_threshold():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1125-E",
        content={"issue": "Form 1125-E required — total receipts $520,000 in 2024"},
        confidence="medium",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-1125E-ABOVE"


# ===========================================================================
# flagged_position — Form 941 / W-2 payroll
# ===========================================================================


def test_fp_941_no_payroll_resolved():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 941",
        content={"issue": "No 941 obligation — no W-2 wages paid in 2023", "context": "no payroll"},
        confidence="high",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-941-NONE"


def test_fp_941_payroll_exists_escalates():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 941",
        content={"issue": "Payroll detected — W-2 wages paid to the owner. 941 obligation exists."},
        confidence="high",
    ))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "FP-941-EXISTS"


# ===========================================================================
# flagged_position — Reason for non-filing
# ===========================================================================


def test_fp_nonfiling_narrative_reviewed():
    r = resolve_item(_item(
        "flagged_position",
        content={"issue": "Reason for non-filing 2023 SC1120 — company in startup phase"},
        confidence="medium",
    ))
    assert r["decision"] == "reviewed", r
    assert r["rule_id"] == "FP-NONFILING"


# ===========================================================================
# flagged_position — SC SOS status
# ===========================================================================


def test_fp_sos_good_standing_resolved():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="SC SOS",
        content={"issue": "SOS check", "context": "entity in good standing as of 2024-03-01"},
        confidence="high",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-SOS-GOOD"


def test_fp_sos_bad_standing_escalates():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="SC SOS",
        content={"issue": "Entity NOT in good standing — annual report overdue"},
        confidence="high",
    ))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "FP-SOS-BAD"


# ===========================================================================
# flagged_position — Out-of-scope
# ===========================================================================


def test_fp_out_of_scope_resolved():
    r = resolve_item(_item(
        "flagged_position",
        content={"issue": "Out-of-scope: state return for GA — route to appropriate specialist"},
        confidence="high",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-OUTOFSCOPE"


# ===========================================================================
# flagged_position — catch-all rules
# ===========================================================================


def test_fp_other_high_resolved():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Schedule M-1",
        content={"issue": "Book-to-tax difference for deferred revenue"},
        confidence="high",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "FP-OTHER-HIGH"


def test_fp_other_medium_reviewed():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Schedule M-1",
        content={"issue": "Book-to-tax timing difference"},
        confidence="medium",
    ))
    assert r["decision"] == "reviewed", r
    assert r["rule_id"] == "FP-OTHER-MED"


def test_fp_other_low_escalates():
    r = resolve_item(_item(
        "flagged_position",
        form_reference="Form 1120, Schedule M-1",
        content={"issue": "Unclear book-to-tax item"},
        confidence="low",
    ))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "FP-OTHER-LOW"


# ===========================================================================
# memo tests
# ===========================================================================


def test_memo_reviewer_reviewed():
    r = resolve_item(_item(
        "memo",
        form_reference="1120_reviewer_memo",
        content={"topic": "1120_reviewer_memo", "body": "Summary of 2023 1120 positions"},
    ))
    assert r["decision"] == "reviewed", r
    assert r["rule_id"] in ("MEMO-GENERIC", "MEMO-DELIVERABLE")


def test_memo_cure_letter_reviewed():
    r = resolve_item(_item(
        "memo",
        content={"topic": "cure_letter", "body": "Cure letter draft for SCDOR FTF 2023"},
    ))
    assert r["decision"] == "reviewed", r
    assert r["rule_id"] == "MEMO-DELIVERABLE"


def test_memo_vda_analysis_reviewed():
    r = resolve_item(_item(
        "memo",
        content="VDA analysis: penalties estimated at $3,200 for 2021-2022",
    ))
    assert r["decision"] == "reviewed", r
    assert r["rule_id"] == "MEMO-DELIVERABLE"


def test_memo_document_checklist_reviewed():
    r = resolve_item(_item(
        "memo",
        content={"topic": "document checklist for 2023 1120"},
    ))
    assert r["decision"] == "reviewed", r
    assert r["rule_id"] == "MEMO-DELIVERABLE"


# ===========================================================================
# question tests
# ===========================================================================


def test_question_1125e_below_threshold_resolved():
    r = resolve_item(_item(
        "question",
        content="Is Form 1125-E required? Total receipts are pre-revenue, below threshold.",
        confidence="medium",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "Q-1125E"


def test_question_941_no_payroll_resolved():
    r = resolve_item(_item(
        "question",
        content="Is there a 941 obligation? No payroll, no W-2 wages paid.",
        confidence="medium",
    ))
    assert r["decision"] == "resolved", r
    assert r["rule_id"] == "Q-941"


def test_question_open_escalates():
    r = resolve_item(_item(
        "question",
        content="Should we elect §179 expensing for the equipment purchase?",
        confidence="medium",
    ))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "Q-ESCALATE"


# ===========================================================================
# Idempotency: already-resolved items are skipped
# ===========================================================================


def test_skip_already_resolved():
    r = resolve_item(_item("form_line_proposal", status="resolved"))
    assert r["skip"] is True
    assert r["decision"] == "skip"


def test_skip_already_rejected():
    r = resolve_item(_item("flagged_position", status="rejected"))
    assert r["skip"] is True


def test_pending_not_skipped():
    r = resolve_item(_item("memo", status="pending"))
    assert r["skip"] is False


def test_reviewed_not_skipped():
    """'reviewed' items can still be re-resolved (e.g., escalated after first pass)."""
    r = resolve_item(_item("memo", status="reviewed"))
    assert r["skip"] is False


# ===========================================================================
# Unknown item_type is escalated conservatively
# ===========================================================================


def test_unknown_type_escalates():
    r = resolve_item(_item("bizarre_new_type"))
    assert r["decision"] == "escalate", r
    assert r["rule_id"] == "UNKNOWN-TYPE"


# ===========================================================================
# Runner (plain-script mode + pytest discovery)
# ===========================================================================

_ALL_TESTS = [
    # form_line_proposal
    ("FLP-HIGH resolved", test_flp_high_resolved),
    ("FLP-MED resolved", test_flp_medium_resolved),
    ("FLP-LOW escalates", test_flp_low_escalates),
    # Banking · Interest
    ("FP-BANKING income→Line5", test_fp_banking_interest_income),
    ("FP-BANKING expense→Line18", test_fp_banking_interest_expense),
    ("FP-163j content trigger", test_fp_163j_content_trigger),
    # SaaS · AI
    ("FP-SAAS-AI default Line26", test_fp_saas_ai_default_line26),
    ("FP-SAAS-AI COGS tagged", test_fp_saas_ai_cogs_tagged),
    # Inter-entity
    ("FP-INTER-ENTITY escalates", test_fp_inter_entity_escalates),
    ("FP-MGMT-FEE escalates", test_fp_management_fee_escalates),
    # 1099
    ("FP-1099-OFFICER escalates", test_fp_1099_officer_escalates),
    ("FP-1099-NONOFF resolved", test_fp_1099_nonoffice_resolved),
    # SC FTF
    ("FP-SC-FTF reviewed", test_fp_sc_ftf_reviewed),
    ("FP-SCDOR-VDA reviewed", test_fp_scdor_vda_reviewed),
    # §382
    ("FP-382 escalates", test_fp_382_escalates),
    ("FP-NOL-382 escalates", test_fp_nol_limitation_382_escalates),
    # Schedule L
    ("FP-SCHED-L resolved", test_fp_schedule_l_resolved),
    # 1125-E
    ("FP-1125E below threshold", test_fp_1125e_below_threshold),
    ("FP-1125E above threshold", test_fp_1125e_above_threshold),
    # 941
    ("FP-941 no payroll resolved", test_fp_941_no_payroll_resolved),
    ("FP-941 payroll exists escalates", test_fp_941_payroll_exists_escalates),
    # Non-filing narrative
    ("FP-NONFILING reviewed", test_fp_nonfiling_narrative_reviewed),
    # SOS
    ("FP-SOS good standing resolved", test_fp_sos_good_standing_resolved),
    ("FP-SOS bad standing escalates", test_fp_sos_bad_standing_escalates),
    # Out-of-scope
    ("FP-OUTOFSCOPE resolved", test_fp_out_of_scope_resolved),
    # Catch-all
    ("FP-OTHER-HIGH resolved", test_fp_other_high_resolved),
    ("FP-OTHER-MED reviewed", test_fp_other_medium_reviewed),
    ("FP-OTHER-LOW escalates", test_fp_other_low_escalates),
    # Memos
    ("MEMO-reviewer reviewed", test_memo_reviewer_reviewed),
    ("MEMO-cure-letter reviewed", test_memo_cure_letter_reviewed),
    ("MEMO-vda-analysis reviewed", test_memo_vda_analysis_reviewed),
    ("MEMO-doc-checklist reviewed", test_memo_document_checklist_reviewed),
    # Questions
    ("Q-1125E resolved", test_question_1125e_below_threshold_resolved),
    ("Q-941 resolved", test_question_941_no_payroll_resolved),
    ("Q-OPEN escalates", test_question_open_escalates),
    # Idempotency
    ("SKIP already resolved", test_skip_already_resolved),
    ("SKIP already rejected", test_skip_already_rejected),
    ("PENDING not skipped", test_pending_not_skipped),
    ("REVIEWED not skipped", test_reviewed_not_skipped),
    # Unknown type
    ("UNKNOWN-TYPE escalates", test_unknown_type_escalates),
]


if __name__ == "__main__":
    print("=" * 60)
    print("resolution_rules unit tests")
    print("=" * 60)
    for name, fn in _ALL_TESTS:
        _test(name, fn)
    print()
    print(f"Results: {len(_PASS)} passed, {len(_FAIL)} failed out of {len(_ALL_TESTS)}")
    if _FAIL:
        print(f"FAILED: {_FAIL}")
        sys.exit(1)
    print("All tests passed.")
    sys.exit(0)
