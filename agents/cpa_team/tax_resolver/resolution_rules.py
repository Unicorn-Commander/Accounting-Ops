"""CPA Tax Resolver — deterministic resolution rules.

``resolve_item(item)`` is the pure function at the heart of the CPA Resolver
agent. It receives one ``tax_review_queue`` row (as a dict, exactly as the
REST API returns it) and returns a resolution decision.

Decision shape::

    {
        "decision":  "resolved" | "reviewed" | "rejected" | "escalate",
        "notes":     str,            # mandatory explanation of the rule that fired
        "rule_id":   str,            # short identifier for the rule (for reporting)
        "skip":      bool,           # True if item is already resolved — caller should skip
    }

When ``decision == "escalate"`` the caller MUST NOT POST to ``/resolve``.
It should log the escalation and continue to the next item.

No side-effects, no I/O, no LLM calls. This module is deliberately pure so
it can be unit-tested offline and called from both the runner and future
Brigade actions.
"""
from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _confidence(item: dict[str, Any]) -> str:
    """Normalise confidence to 'high' | 'medium' | 'low' (default 'medium')."""
    raw = (item.get("confidence") or "medium").strip().lower()
    if raw not in ("high", "medium", "low"):
        return "medium"
    return raw


def _form_ref(item: dict[str, Any]) -> str:
    """Normalise form_reference to lower-stripped string."""
    return (item.get("form_reference") or "").strip().lower()


def _item_type(item: dict[str, Any]) -> str:
    """Normalise item_type."""
    return (item.get("item_type") or "").strip().lower()


def _content_text(item: dict[str, Any]) -> str:
    """Flatten the content field to a searchable string.

    content may be a dict with keys like 'issue', 'context', 'recommendation',
    or it may already be a plain string (older queue rows).
    """
    content = item.get("content")
    if isinstance(content, dict):
        return " ".join(str(v) for v in content.values()).lower()
    return str(content or "").lower()


def _content_dict(item: dict[str, Any]) -> dict[str, Any]:
    """Return content as a dict; returns {} if it's a plain string."""
    content = item.get("content")
    if isinstance(content, dict):
        return content
    return {}


def _contains(haystack: str, *needles: str) -> bool:
    """Return True if ALL needles appear in haystack (case-insensitive)."""
    h = haystack.lower()
    return all(n.lower() in h for n in needles)


def _contains_any(haystack: str, *needles: str) -> bool:
    """Return True if ANY needle appears in haystack (case-insensitive)."""
    h = haystack.lower()
    return any(n.lower() in h for n in needles)


def _already_resolved(item: dict[str, Any]) -> bool:
    """Return True if the item is not in a resolvable state."""
    return (item.get("status") or "pending").lower() not in ("pending", "reviewed")


# ---------------------------------------------------------------------------
# Rule helpers
# ---------------------------------------------------------------------------

def _make(decision: str, notes: str, rule_id: str) -> dict[str, Any]:
    return {"decision": decision, "notes": notes, "rule_id": rule_id, "skip": False}


def _skip(reason: str) -> dict[str, Any]:
    return {"decision": "skip", "notes": reason, "rule_id": "SKIP", "skip": True}


def _escalate(notes: str, rule_id: str) -> dict[str, Any]:
    return {"decision": "escalate", "notes": notes, "rule_id": rule_id, "skip": False}


# ---------------------------------------------------------------------------
# Per-item-type resolution logic
# ---------------------------------------------------------------------------


def _resolve_form_line_proposal(item: dict[str, Any]) -> dict[str, Any]:
    """Rules for item_type == 'form_line_proposal'.

    | Confidence | Action                                              | Status    |
    |------------|-----------------------------------------------------|-----------|
    | high       | ACCEPT                                              | resolved  |
    | medium     | ACCEPT with confirmation note                       | resolved  |
    | low        | ESCALATE — true human judgment needed               | pending   |
    """
    conf = _confidence(item)
    form_ref = _form_ref(item)

    if conf == "high":
        return _make(
            "resolved",
            (
                f"CPA Resolver: form_line_proposal accepted (confidence=high). "
                f"Specialist's proposed line ({form_ref}) and amount confirmed via "
                f"deterministic rule FLP-HIGH. No material ambiguity detected."
            ),
            "FLP-HIGH",
        )

    if conf == "medium":
        return _make(
            "resolved",
            (
                f"CPA Resolver: form_line_proposal accepted (confidence=medium). "
                f"Confirmed via deterministic rule FLP-MED. "
                f"Note: CPA Resolver confirmed via deterministic rule — "
                f"lead should spot-check the cited journal entries before filing."
            ),
            "FLP-MED",
        )

    # low confidence
    return _escalate(
        (
            f"CPA Resolver: ESCALATION — form_line_proposal has confidence=low "
            f"(form_reference={form_ref!r}). "
            f"Low-confidence line proposals require true human CPA judgment before "
            f"the amount can be accepted or rejected. Rule FLP-LOW."
        ),
        "FLP-LOW",
    )


def _resolve_flagged_position(item: dict[str, Any]) -> dict[str, Any]:
    """Rules for item_type == 'flagged_position'.

    Category is inferred from the combination of form_reference and content
    (the specialist writes descriptive content, not a structured category enum).
    """
    conf = _confidence(item)
    form_ref = _form_ref(item)
    ct = _content_text(item)

    # ------------------------------------------------------------------
    # Banking · Interest
    # Requires explicit banking/interest signal in EITHER form_ref OR content,
    # but "line 5" alone is not enough (too generic — 1120 line 5 is interest
    # but many other forms use line 5 for other things).
    # ------------------------------------------------------------------
    if _contains_any(ct, "banking · interest", "banking.interest", "interest income",
                     "interest expense", "§163(j)") or \
       _contains_any(form_ref, "banking", "§163"):
        # Sign rule: positive net → Line 5 (income); negative → Line 18 (expense).
        # The agent always resolves this; §163(j) limitation note is included.
        positive_signals = ["line 5", "income", "positive", "received", "earned", "credit"]
        negative_signals = ["line 18", "expense", "negative", "paid", "deductible", "debit"]
        positive_hit = _contains_any(ct, *positive_signals)
        negative_hit = _contains_any(ct, *negative_signals)

        if positive_hit and not negative_hit:
            placement = "Line 5 (Interest income)"
        elif negative_hit and not positive_hit:
            placement = "Line 18 (Interest expense)"
        else:
            placement = "Line 5 or Line 18 (sign ambiguous — see note)"

        return _make(
            "resolved",
            (
                f"CPA Resolver: Banking·Interest — {placement}. "
                f"Sign rule applied: positive net interest → Line 5 income; "
                f"negative → Line 18 expense. "
                f"§163(j) limitation may apply if substantial interest expense exists; "
                f"CPA should confirm whether the small-business exemption applies "
                f"(average annual gross receipts ≤ $30M over 3 prior years, §163(j)(3)). "
                f"Rule FP-BANKING-INTEREST."
            ),
            "FP-BANKING-INTEREST",
        )

    # ------------------------------------------------------------------
    # SaaS · AI / Form 1125-A
    # Must match on SaaS/AI-specific signals, NOT on generic "line 26".
    # "Form 1125-A" in form_ref is distinct from "Form 1125-E" (officer comp).
    # ------------------------------------------------------------------
    if _contains_any(form_ref, "saas", "1125-a", "1125a") or \
       _contains_any(ct, "saas · ai", "saas.ai", "artificial intelligence",
                     "form 1125-a", "form 1125a", "cogs", "software subscription"):
        # Only route to Form 1125-A if explicitly tagged COGS-eligible.
        is_cogs_tagged = _contains_any(ct, "cogs-eligible", "cogs eligible",
                                       "cogs_eligible", "form 1125-a required",
                                       "1125-a required")
        if is_cogs_tagged:
            return _make(
                "resolved",
                (
                    f"CPA Resolver: SaaS·AI — COGS-eligible tag detected. "
                    f"Routed to Form 1125-A (Cost of Goods Sold). "
                    f"CPA must confirm the vendor/service is directly tied to revenue "
                    f"product before including on Form 1125-A. "
                    f"Rule FP-SAAS-AI-COGS."
                ),
                "FP-SAAS-AI-COGS",
            )
        return _make(
            "resolved",
            (
                f"CPA Resolver: SaaS·AI — default placement Line 26 (Other deductions). "
                f"No COGS-eligible tag found; specialist did not mark this as Form 1125-A "
                f"material. CPA should confirm no customer-facing AI product was delivered "
                f"in the tax year that would support COGS reclassification. "
                f"Rule FP-SAAS-AI-LINE26."
            ),
            "FP-SAAS-AI-LINE26",
        )

    # ------------------------------------------------------------------
    # Inter-entity transfer — ALWAYS ESCALATE
    # Match on explicit inter-entity vocabulary; "related party" in the
    # form_ref is specific enough; avoid false positives from generic
    # "line 26" form references.
    # ------------------------------------------------------------------
    if _contains_any(form_ref, "inter-entity", "inter_entity", "intercompany") or \
       _contains_any(ct, "inter-entity transfer", "inter_entity", "intercompany",
                     "management fee", "related party", "related-party"):
        return _escalate(
            (
                f"CPA Resolver: ESCALATION — Inter-entity transfer detected "
                f"(form_reference={form_ref!r}). "
                f"Could be management fee, intercompany loan, or elimination item; "
                f"CPA must classify each transaction before any amount can be placed "
                f"on the return. Rule FP-INTER-ENTITY."
            ),
            "FP-INTER-ENTITY",
        )

    # ------------------------------------------------------------------
    # Contractor · 1099-NEC officer
    # Triggered by explicit 1099-NEC vocabulary in content OR "1099" in form_ref.
    # "Form 1125-E" in form_ref alone (without 1099 context) is handled by the
    # Form 1125-E threshold rule below — don't conflate.
    # ------------------------------------------------------------------
    if _contains_any(ct, "1099-nec", "1099 nec") or \
       _contains_any(form_ref, "1099"):
        # Escalate if officer; else resolve as Line 26.
        # "not an officer" / "not officer" beats a bare officer keyword match.
        not_officer = _contains_any(ct, "not an officer", "not officer", "non-officer",
                                    "non officer", "not a statutory officer")
        is_officer = not not_officer and _contains_any(
            ct, "officer", "president", "ceo", "cfo", "secretary",
            "director", "form 1125-e"
        )
        if is_officer:
            return _escalate(
                (
                    f"CPA Resolver: ESCALATION — Contractor·1099-NEC with officer indicator "
                    f"(form_reference={form_ref!r}). "
                    f"Officer vs. non-officer distinction changes both the form (1125-E) "
                    f"and the disclosure. CPA must confirm whether this individual is a "
                    f"statutory officer per §3401(d) and whether Form 1125-E applies. "
                    f"Rule FP-1099-OFFICER."
                ),
                "FP-1099-OFFICER",
            )
        return _make(
            "resolved",
            (
                f"CPA Resolver: Contractor·1099-NEC — non-officer determination. "
                f"No officer indicator detected in content. "
                f"Resolved as Form 1120 Line 26 (Other deductions) for contractor compensation. "
                f"CPA should confirm 1099-NEC was or will be filed for payees ≥ $600. "
                f"Rule FP-1099-NONOFF."
            ),
            "FP-1099-NONOFF",
        )

    # ------------------------------------------------------------------
    # Reason for non-filing narrative — check BEFORE SC FTF because both
    # may contain "SC1120" and "non-filing" is more specific.
    # ------------------------------------------------------------------
    if _contains_any(ct, "reason for non-filing", "non-filing", "non filing",
                     "reason for not filing") or \
       _contains_any(form_ref, "non-filing", "non filing"):
        return _make(
            "reviewed",
            (
                f"CPA Resolver: Reason-for-non-filing narrative accepted. "
                f"Specialist's narrative noted and marked reviewed. "
                f"Lead CPA verification required before this narrative is submitted "
                f"as part of any late-filing package or VDA application. "
                f"Rule FP-NONFILING."
            ),
            "FP-NONFILING",
        )

    # ------------------------------------------------------------------
    # SC1120 FTF / VDA / SCDOR notice — ACKNOWLEDGE
    # ------------------------------------------------------------------
    if _contains_any(ct, "failure to file", "failure-to-file", "ftf", "vda",
                     "voluntary disclosure", "scdor", "sc1120", "cure letter",
                     "sc ftf", "scdor notice") or \
       _contains_any(form_ref, "sc1120", "scdor", "ftf", "vda", "cure"):
        return _make(
            "reviewed",
            (
                f"CPA Resolver: SC1120 failure-to-file / VDA / SCDOR notice acknowledged. "
                f"The cure letter or voluntary disclosure package is the action item, "
                f"not this flag itself. Marked 'reviewed' — CPA must review the drafted "
                f"cure letter and submit it to SCDOR. Rule FP-SC-FTF."
            ),
            "FP-SC-FTF",
        )

    # ------------------------------------------------------------------
    # §382 ownership-change — ALWAYS ESCALATE
    # ------------------------------------------------------------------
    if _contains_any(ct, "§382", "section 382", "ownership change", "ownership-change",
                     "ownership history", "nol limitation", "382 limitation") or \
       _contains_any(form_ref, "§382", "382"):
        return _escalate(
            (
                f"CPA Resolver: ESCALATION — §382 ownership-change detected "
                f"(form_reference={form_ref!r}). "
                f"Lead CPA must confirm complete ownership history and calculate the "
                f"§382 annual NOL limitation before any NOL deduction can be accepted. "
                f"Rule FP-382."
            ),
            "FP-382",
        )

    # ------------------------------------------------------------------
    # Schedule L empty / reconstruction
    # ------------------------------------------------------------------
    if _contains_any(ct, "schedule l", "balance sheet reconstruction", "opening balance",
                     "reconstruction", "best-effort", "prior-year return") or \
       _contains_any(form_ref, "schedule l", "sched l", "schedule-l"):
        return _make(
            "resolved",
            (
                f"CPA Resolver: Schedule L reconstruction — accepted with note. "
                f"Schedule L reconstruction is best-effort from the posted journal; "
                f"CPA should verify opening balances against bank statements and any "
                f"prior-year returns on file. Beginning-of-year columns for the first "
                f"year of operations are expected to be blank. Rule FP-SCHED-L."
            ),
            "FP-SCHED-L",
        )

    # ------------------------------------------------------------------
    # Form 1125-E threshold check
    # ------------------------------------------------------------------
    if _contains_any(ct, "form 1125-e", "form 1125e", "1125-e threshold",
                     "officer compensation", "total receipts") or \
       _contains_any(form_ref, "1125-e", "1125e"):
        cd = _content_dict(item)
        content_str = ct

        # Try to detect whether total receipts < $500K is mentioned.
        below_threshold = _contains_any(
            content_str,
            "not required", "below threshold", "less than $500", "under $500",
            "pre-revenue", "no revenue", "$0 revenue", "zero revenue",
            "below 500", "< $500", "total receipts < $500",
        )
        if below_threshold:
            return _make(
                "resolved",
                (
                    f"CPA Resolver: Form 1125-E threshold — total receipts < $500K. "
                    f"Form 1125-E not required when total receipts are below $500,000 "
                    f"per Form 1120 instructions (page 17). "
                    f"Rule FP-1125E-BELOW."
                ),
                "FP-1125E-BELOW",
            )
        # Above threshold or ambiguous — accept specialist's proposal.
        return _make(
            "resolved",
            (
                f"CPA Resolver: Form 1125-E threshold — total receipts may be ≥ $500K. "
                f"Accepted specialist's proposal for Form 1125-E. "
                f"CPA must verify total receipts against the entity's books and confirm "
                f"whether Form 1125-E attachment is required. Rule FP-1125E-ABOVE."
            ),
            "FP-1125E-ABOVE",
        )

    # ------------------------------------------------------------------
    # Form 941 / W-2 payroll
    # ------------------------------------------------------------------
    if _contains_any(ct, "form 941", "w-2", "w2", "payroll", "941 obligation",
                     "941 filing", "payroll tax") or \
       _contains_any(form_ref, "941", "w-2", "w2", "payroll"):
        no_payroll = _contains_any(
            ct,
            "no w-2", "no payroll", "no 941", "no wages", "no salary",
            "not required", "pre-revenue", "no compensation paid",
            "0 w-2", "zero w-2",
        )
        if no_payroll:
            return _make(
                "resolved",
                (
                    f"CPA Resolver: Form 941 / W-2 — no payroll obligation detected. "
                    f"No W-2 wages paid in the tax year; no Form 941 filing obligation. "
                    f"CPA should confirm no payroll was run by checking bank statements "
                    f"for any payroll processor ACH debits. Rule FP-941-NONE."
                ),
                "FP-941-NONE",
            )
        return _escalate(
            (
                f"CPA Resolver: ESCALATION — Form 941 / W-2 with potential payroll "
                f"obligation detected (form_reference={form_ref!r}). "
                f"CPA must verify whether W-2 wages were paid and whether Form 941 "
                f"quarterly deposits were made. Rule FP-941-EXISTS."
            ),
            "FP-941-EXISTS",
        )

    # ------------------------------------------------------------------
    # SC SOS status check
    # ------------------------------------------------------------------
    if _contains_any(ct, "sos", "secretary of state", "annual report", "good standing",
                     "dissolution", "forfeiture", "sc sos") or \
       _contains_any(form_ref, "sos", "secretary of state"):
        # "NOT in good standing" must NOT be mistaken for good standing.
        negated = _contains_any(ct, "not in good standing", "not in good", "not good standing",
                                "not current", "not compliant", "bad standing",
                                "annual report overdue", "overdue", "forfeited",
                                "dissolution risk", "forfeiture", "suspended")
        good_standing = not negated and _contains_any(
            ct, "good standing", "current", "compliant", "in good standing", "verified", "active"
        )
        if good_standing:
            # Extract verification date if mentioned.
            date_match = re.search(
                r'\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\w+ \d{1,2},? \d{4})\b', ct
            )
            date_note = f" (SOS verification date: {date_match.group(0)})" if date_match else ""
            return _make(
                "resolved",
                (
                    f"CPA Resolver: SC SOS status — entity is in good standing{date_note}. "
                    f"No dissolution risk detected. Resolved per SOS verification. "
                    f"Rule FP-SOS-GOOD."
                ),
                "FP-SOS-GOOD",
            )
        # Bad standing or unknown — escalate.
        return _escalate(
            (
                f"CPA Resolver: ESCALATION — SC SOS status unclear or entity NOT in good "
                f"standing (form_reference={form_ref!r}). "
                f"CPA must verify current SOS standing at sc.gov and cure any overdue "
                f"annual reports before filing. Rule FP-SOS-BAD."
            ),
            "FP-SOS-BAD",
        )

    # ------------------------------------------------------------------
    # Out-of-Scope flag
    # ------------------------------------------------------------------
    if _contains_any(ct, "out of scope", "out-of-scope", "out_of_scope",
                     "not in scope", "route to appropriate") or \
       _contains_any(form_ref, "out-of-scope", "out of scope"):
        return _make(
            "resolved",
            (
                f"CPA Resolver: Out-of-scope item acknowledged. "
                f"Noted — this item is out of scope for the producing specialist; "
                f"routed to the appropriate specialist or CPA for handling. "
                f"Rule FP-OUTOFSCOPE."
            ),
            "FP-OUTOFSCOPE",
        )

    # ------------------------------------------------------------------
    # Catch-all for other flagged_position items: confidence-gated
    # ------------------------------------------------------------------
    if conf == "high":
        return _make(
            "resolved",
            (
                f"CPA Resolver: flagged_position (unclassified category) — "
                f"accepted (confidence=high). "
                f"No specific deterministic rule matched; high confidence accepted per "
                f"catch-all rule FP-OTHER-HIGH. "
                f"Form reference: {form_ref!r}."
            ),
            "FP-OTHER-HIGH",
        )

    if conf == "medium":
        return _make(
            "reviewed",
            (
                f"CPA Resolver: flagged_position (unclassified category) — "
                f"marked reviewed (confidence=medium). "
                f"No specific deterministic rule matched; medium-confidence item requires "
                f"CPA verification before final acceptance. Rule FP-OTHER-MED. "
                f"Form reference: {form_ref!r}."
            ),
            "FP-OTHER-MED",
        )

    # low confidence, unclassified
    return _escalate(
        (
            f"CPA Resolver: ESCALATION — flagged_position (unclassified category, "
            f"confidence=low). "
            f"No deterministic rule matched and confidence is low. "
            f"True human CPA judgment required. Rule FP-OTHER-LOW. "
            f"Form reference: {form_ref!r}."
        ),
        "FP-OTHER-LOW",
    )


def _resolve_memo(item: dict[str, Any]) -> dict[str, Any]:
    """Rules for item_type == 'memo'.

    | Sub-category                     | Action      | Status   |
    |----------------------------------|-------------|----------|
    | reviewer memo / cpa_resolver     | ACKNOWLEDGE | reviewed |
    | cure letter / VDA / doc checklist| ACCEPT      | reviewed |
    | any other memo                   | ACKNOWLEDGE | reviewed |
    """
    ct = _content_text(item)
    form_ref = _form_ref(item)

    # Deliverable memos (cure letters, VDA analysis, document checklists).
    if _contains_any(ct, "cure letter", "vda analysis", "voluntary disclosure analysis",
                     "document checklist", "checklist", "penalty abatement", "vda package",
                     "cure package") or \
       _contains_any(form_ref, "cure letter", "vda", "checklist"):
        return _make(
            "reviewed",
            (
                f"CPA Resolver: Memo deliverable accepted — marked reviewed for CPA handoff. "
                f"This is a cure letter draft, VDA analysis, or document checklist produced "
                f"by a specialist. Marked 'reviewed' so the actual CPA can locate it, "
                f"review the content, and send/submit as appropriate. "
                f"Rule MEMO-DELIVERABLE."
            ),
            "MEMO-DELIVERABLE",
        )

    # Summary memo written by cpa_resolver itself — acknowledge idempotently.
    if _contains_any(ct, "cpa_resolver_summary", "cpa resolver summary", "cpa-resolver"):
        return _make(
            "reviewed",
            (
                f"CPA Resolver: Resolver summary memo acknowledged. Rule MEMO-RESOLVER."
            ),
            "MEMO-RESOLVER",
        )

    # Generic reviewer memo.
    return _make(
        "reviewed",
        (
            f"CPA Resolver: Reviewer memo acknowledged and marked reviewed. "
            f"The narrative in this memo was produced by the tax specialist team. "
            f"CPA should read the memo before finalising the return. "
            f"Rule MEMO-GENERIC."
        ),
        "MEMO-GENERIC",
    )


def _resolve_question(item: dict[str, Any]) -> dict[str, Any]:
    """Rules for item_type == 'question'.

    If a deterministic answer exists from cited evidence → answer + resolve.
    Otherwise → ESCALATE.

    Deterministic questions we handle:
    - Is Form 1125-E required? (total receipts threshold)
    - Is there a 941 / payroll obligation?
    - Is Form 1125-A required?
    - Is there an inter-entity transfer that needs CPA classification?
    """
    ct = _content_text(item)
    form_ref = _form_ref(item)

    # 1125-E threshold question.
    if _contains_any(ct, "1125-e", "officer compensation", "total receipts") and \
       _contains_any(ct, "required", "need", "should"):
        below = _contains_any(ct, "below", "less than", "under", "pre-revenue",
                               "no revenue", "not required", "zero revenue")
        if below:
            return _make(
                "resolved",
                (
                    f"CPA Resolver: Q — Form 1125-E required? Answer: No. "
                    f"Total receipts appear to be below the $500,000 threshold. "
                    f"Form 1125-E not required per Form 1120 instructions (page 17). "
                    f"CPA should verify total receipts independently. Rule Q-1125E."
                ),
                "Q-1125E",
            )

    # 941 / payroll obligation question.
    if _contains_any(ct, "941", "payroll", "w-2", "w2") and \
       _contains_any(ct, "required", "obligation", "filed", "owe"):
        no_payroll = _contains_any(ct, "no payroll", "no wages", "no w-2",
                                    "not required", "pre-revenue")
        if no_payroll:
            return _make(
                "resolved",
                (
                    f"CPA Resolver: Q — 941 / payroll obligation? Answer: None detected. "
                    f"No W-2 wages paid in the period; no Form 941 filing obligation. "
                    f"CPA should confirm via bank statement review. Rule Q-941."
                ),
                "Q-941",
            )

    # All other questions — escalate (no deterministic answer available).
    return _escalate(
        (
            f"CPA Resolver: ESCALATION — question item has no deterministic answer "
            f"in the cited evidence (form_reference={form_ref!r}). "
            f"This question requires true human CPA judgment or information not "
            f"available to the rule engine. Rule Q-ESCALATE."
        ),
        "Q-ESCALATE",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_item(item: dict[str, Any]) -> dict[str, Any]:
    """Determine the resolution decision for one tax_review_queue row.

    Args:
        item: A dict with the fields returned by
              GET /api/workspaces/{wid}/tax-review-queue.
              Required keys: item_type, confidence, form_reference, content, status.

    Returns:
        {
            "decision":  "resolved" | "reviewed" | "rejected" | "escalate" | "skip",
            "notes":     str,
            "rule_id":   str,
            "skip":      bool,
        }

    When ``skip`` is True the caller must not attempt to POST to /resolve.
    When ``decision`` is 'escalate' the caller must not POST to /resolve and
    should instead log the escalation and move on.
    """
    # ------------------------------------------------------------------
    # Guard: skip items that are already in a terminal state.
    # ------------------------------------------------------------------
    status = (item.get("status") or "pending").lower()
    if status not in ("pending", "reviewed"):
        return _skip(
            f"Item status='{status}' is already terminal; skipping (idempotent). "
            f"Re-running the resolver on a resolved queue is a clean no-op."
        )

    itype = _item_type(item)

    if itype == "form_line_proposal":
        return _resolve_form_line_proposal(item)

    if itype == "flagged_position":
        return _resolve_flagged_position(item)

    if itype == "memo":
        return _resolve_memo(item)

    if itype == "question":
        return _resolve_question(item)

    # Unknown item_type — escalate conservatively.
    return _escalate(
        (
            f"CPA Resolver: ESCALATION — unknown item_type '{itype}'. "
            f"No rule defined for this item type. Conservative escalation to human CPA. "
            f"Rule UNKNOWN-TYPE."
        ),
        "UNKNOWN-TYPE",
    )
