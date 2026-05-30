"""Open-items aggregator: one polished JSON payload for the
/open-items dashboard.

The web dashboard lives at /open-items and is the "what's on Aaron's
plate" page. Rather than have the React layer pull from many endpoints
and do the sorting/bucketing client-side, this endpoint does the work:

  Section 1 — Time-sensitive (red/orange)
  Section 2 — Documents Aaron needs to provide (hard-coded checklist
              augmented with completion state stored in tax_review_queue)
  Section 3 — Decisions awaiting Aaron (with recommendation + accept/reject)
  Section 4 — Items waiting on Mosin (CPA)
  Section 5 — Items waiting on attorney
  Section 6 — Resolved this week (rollup of recent wins)

Source of truth for sections 1, 3, 4, 5 is the ``tax_review_queue`` table
in the workspace tenant schema. Section 2 is a static catalogue with
"completion" tracked via memo rows of item_type='memo' with
form_reference starting with 'DOC_RECEIVED: '. Section 6 is
``tax_review_queue.resolved_at >= now() - 7 days``.

Routing rules from form_reference + submitter_id + content topic:

  * submitter_id LIKE '%loader%' -> usually INFORMATIONAL (corrections,
    captured facts) — bucket as "context" unless prefixed CRITICAL.
  * submitter_id == 'tax-1120-preparer' or LIKE '*specialist%' ->
    typically routed to Mosin (CPA-review territory).
  * form_reference starts with 'CRITICAL: ' -> Section 1 (time-sensitive).
  * form_reference starts with 'Decision: ' or 'Strategy — ' (specific
    enumerated strategies) -> Section 3 (Aaron decides).
  * form_reference contains 'Attorney' or 'Fabrikam' or 'LLC formation' ->
    Section 5.
  * else -> Section 4 (waiting on Mosin).

The hard list of "things Aaron has to physically go pull" lives in
``_AARON_DOCUMENT_CHECKLIST`` below — bumping this list is the only
edit needed to add a new doc-checklist item.

Read floor: viewer. Entity-scope enforced.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["open-items"])


# ---------------------------------------------------------------------------
# Static catalogues
# ---------------------------------------------------------------------------

# Hard list of documents Aaron must physically pull from the source system.
# Each item maps to a deep-link where the doc lives. "needed_for" is human-
# readable downstream context. "tax_years" tags which years it unblocks.
#
# Completion is tracked by writing a memo row to tax_review_queue with
# form_reference = f"DOC_RECEIVED: {key}" — that way the same table holds
# both state (received timestamp + reviewer) and audit trail. No new table
# needed for the v1 dashboard.
_AARON_DOCUMENT_CHECKLIST: list[dict[str, Any]] = [
    {
        "key": "scdor_notice_2025_06_16",
        "name": "SCDOR Notice (2025-06-16)",
        "description": (
            "Original SCDOR notice (mailed June 2025) — needed to draft "
            "the reasonable-cause cure letter and respond before the "
            "administrative-dissolution clock runs out."
        ),
        "where_to_get": "Physical mail / SCDOR MyDORWAY login",
        "where_to_get_url": "https://mydorway.dor.sc.gov/",
        "needed_for": "SC1120 reasonable-cause letter (P-00014)",
        "tax_years": [2022, 2023, 2024],
        "priority": "time_sensitive",
    },
    {
        "key": "dfas_1099r_2022",
        "name": "DFAS 1099-R — TY 2022",
        "description": "Military retirement income reporting",
        "where_to_get": "myPay (DFAS)",
        "where_to_get_url": "https://mypay.dfas.mil/",
        "needed_for": "Form 1040 Line 5b — Aaron Personal TY 2022",
        "tax_years": [2022],
        "priority": "high",
    },
    {
        "key": "dfas_1099r_2023",
        "name": "DFAS 1099-R — TY 2023",
        "description": "Military retirement income reporting",
        "where_to_get": "myPay (DFAS)",
        "where_to_get_url": "https://mypay.dfas.mil/",
        "needed_for": "Form 1040 Line 5b — Aaron Personal TY 2023",
        "tax_years": [2023],
        "priority": "high",
    },
    {
        "key": "dfas_1099r_2024",
        "name": "DFAS 1099-R — TY 2024",
        "description": "Military retirement income reporting",
        "where_to_get": "myPay (DFAS)",
        "where_to_get_url": "https://mypay.dfas.mil/",
        "needed_for": "Form 1040 Line 5b — Aaron Personal TY 2024",
        "tax_years": [2024],
        "priority": "high",
    },
    {
        "key": "va_award_letter",
        "name": "VA Award Letter",
        "description": (
            "Current VA disability rating letter. Establishes combat-zone "
            "and disability-compensation exclusion under §112."
        ),
        "where_to_get": "VA.gov — Letters & Documents",
        "where_to_get_url": "https://www.va.gov/records/download-va-letters/",
        "needed_for": "Form 1040 — combat-zone §112 exclusion + SC §12-37-220 property tax memo",
        "tax_years": [2022, 2023, 2024, 2025],
        "priority": "high",
    },
    {
        "key": "filing_status_confirm",
        "name": "Filing Status — MFJ confirmation",
        "description": (
            "Confirm filing status (Married Filing Jointly assumed) for "
            "all open personal tax years. Affects bracketing and "
            "deduction-floor math throughout."
        ),
        "where_to_get": "Self-attestation",
        "where_to_get_url": None,
        "needed_for": "Form 1040 — all open personal years",
        "tax_years": [2022, 2023, 2024, 2025],
        "priority": "high",
    },
    {
        "key": "crdp_crsc_election",
        "name": "CRDP vs CRSC election status",
        "description": (
            "Which election is currently active? CRDP (concurrent receipt "
            "of retirement & disability pay) or CRSC (combat-related "
            "special compensation). Affects 1099-R Box 5 treatment."
        ),
        "where_to_get": "myPay > Concurrent Receipt Election",
        "where_to_get_url": "https://mypay.dfas.mil/",
        "needed_for": "Form 1040 Line 5b interpretation + reasonable-cause memo",
        "tax_years": [2022, 2023, 2024, 2025],
        "priority": "medium",
    },
    {
        "key": "paypal_credit_2024",
        "name": "PayPal Credit statements — TY 2024",
        "description": (
            "Statement year-end balances + interest paid (Schedule A or "
            "business interest if charged to MU Inc accounts)."
        ),
        "where_to_get": "PayPal > Activity > Statements",
        "where_to_get_url": "https://www.paypal.com/myaccount/transactions/statements",
        "needed_for": "MU Inc Form 1120 interest deduction (TY 2024)",
        "tax_years": [2024],
        "priority": "medium",
    },
    {
        "key": "bank_statements_2022_opening",
        "name": "Bank statements — TY 2022 opening balances",
        "description": (
            "First-of-year balances for every MU Inc bank account. "
            "Needed to build Schedule L starting balance sheet."
        ),
        "where_to_get": "Mercury, Chase, etc. (each FI's online portal)",
        "where_to_get_url": None,
        "needed_for": "MU Inc Form 1120 Schedule L (TY 2022)",
        "tax_years": [2022],
        "priority": "medium",
    },
    {
        "key": "skybehind_structure",
        "name": "SkyBehind eBay — vendor vs partner clarification",
        "description": (
            "Need David's written confirmation: is SkyBehind a vendor "
            "Aaron resells for (1099-NEC territory) or a JV partner "
            "(K-1 / 1065 territory)? Drives whether the entire eBay "
            "P&L moves to MU Inc or stays personal Schedule C."
        ),
        "where_to_get": "Email / text David (SkyBehind owner)",
        "where_to_get_url": None,
        "needed_for": "SkyBehind eBay structure + Schedule C / Form 1120 routing",
        "tax_years": [2023, 2024, 2025],
        "priority": "medium",
    },
    {
        "key": "ebay_account_transfer",
        "name": "Move SkyBehind eBay account to MU Inc EIN",
        "description": (
            "Once structure is decided: physically re-title the eBay "
            "seller account from Aaron's SSN to MU Inc's EIN to clean "
            "up 1099-K reporting going forward."
        ),
        "where_to_get": "eBay Seller Hub > Account Settings",
        "where_to_get_url": "https://www.ebay.com/sh/ovw",
        "needed_for": "Clean 1099-K issuance for TY 2026+",
        "tax_years": [2026],
        "priority": "low",
    },
]


# Decisions awaiting Aaron — these are the questions where the engine has
# a recommendation but needs Aaron's go/no-go. Each row creates a
# clickable card; "Accept recommendation" / "Different choice" persist as
# memos to tax_review_queue.
_AARON_DECISION_CHECKLIST: list[dict[str, Any]] = [
    {
        "key": "sec195_treatment",
        "question": (
            "§195 startup-cost treatment: deduct currently or elect to "
            "amortize over 180 months?"
        ),
        "recommendation": "Deduct currently — no §195 election.",
        "rationale": (
            "MU Inc's pre-revenue spending was modest and you had taxable "
            "income to absorb the deduction in the year incurred. The "
            "180-month amortization election only helps if you're starting "
            "with a loss carryforward you can't use. Skip the election; "
            "treat as ordinary §162 expense."
        ),
        "tax_years": [2022, 2023, 2024],
        "form_reference_tag": "Decision: §195 startup costs",
    },
    {
        "key": "sec41_credit_skip",
        "question": (
            "§41 R&D credit — claim for TY 2022-2024 or skip?"
        ),
        "recommendation": "Skip for TY 2022-2024; revisit for TY 2025+.",
        "rationale": (
            "The §41 four-part test documentation burden (process of "
            "experimentation, technological uncertainty, etc.) outweighs "
            "the credit value at MU Inc's spending level pre-product. "
            "Once you have a customer-facing productized line in TY 2025+, "
            "the credit becomes worth the contemporaneous documentation "
            "discipline."
        ),
        "tax_years": [2022, 2023, 2024],
        "form_reference_tag": "Decision: §41 R&D credit",
    },
    {
        "key": "filing_status_mfj",
        "question": "Confirm filing status — Married Filing Jointly?",
        "recommendation": "Married Filing Jointly (MFJ).",
        "rationale": (
            "MFJ is almost always more favorable for a household with one "
            "active W-2/self-employed earner plus one SSDI-only spouse. "
            "Confirm so we can lock the bracket assumptions across all "
            "open personal years."
        ),
        "tax_years": [2022, 2023, 2024, 2025],
        "form_reference_tag": "Decision: Filing Status",
    },
]


# ---------------------------------------------------------------------------
# Section bucketing logic
# ---------------------------------------------------------------------------


def _bucket_for_item(row: dict[str, Any]) -> str:
    """Map a tax_review_queue row to one of:

      "time_sensitive" | "decision" | "mosin" | "attorney" | "info"

    Implements the rules documented at the top of this module.
    """
    form_ref = (row.get("form_reference") or "").strip()
    submitter = (row.get("submitter_id") or "").lower()
    item_type = (row.get("item_type") or "").lower()

    # 1. Explicit critical / time-sensitive — always wins
    if form_ref.startswith("CRITICAL"):
        return "time_sensitive"

    # 2. Decision tag prefix wins next
    if form_ref.startswith("Decision:") or form_ref.startswith("Decision "):
        return "decision"

    # 3. Attorney-territory keywords
    attorney_keywords = ("attorney", "fabrikam", "llc formation", "registered agent")
    fr_lower = form_ref.lower()
    if any(kw in fr_lower for kw in attorney_keywords):
        return "attorney"

    # 4. Loader-sourced items default to informational unless they were
    #    flagged_position (which always merit human-eye on the queue).
    if "loader" in submitter:
        if item_type == "flagged_position":
            return "mosin"
        return "info"

    # 5. Specialist-sourced items typically need Mosin's review
    if "specialist" in submitter or "preparer" in submitter or "drafter" in submitter:
        return "mosin"

    if "narrative-generator" in submitter or "resolver" in submitter:
        return "mosin"

    # 6. Default fallback
    return "mosin"


def _summarize_content(content: Any) -> str:
    """Extract a one-line summary from a queue item's content jsonb."""
    if isinstance(content, dict):
        # 1120 preparer memos carry "text" + "topic"
        text_blob = content.get("text") or content.get("summary") or ""
        if text_blob:
            # First non-blank line, strip leading markdown chrome
            for line in str(text_blob).splitlines():
                s = line.strip().lstrip("#").strip()
                if s and not s.startswith("---"):
                    return s[:240]
        topic = content.get("topic")
        if topic:
            return str(topic)[:240]
    if content:
        return str(content)[:240]
    return ""


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class OpenItemsResponse(BaseModel):
    workspace_id: str
    entity_id: str | None  # None == consolidated across entities
    as_of: str
    counts: dict[str, int]
    time_sensitive: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    waiting_on_mosin: list[dict[str, Any]]
    waiting_on_attorney: list[dict[str, Any]]
    resolved_this_week: list[dict[str, Any]]
    # Tax submissions in flight (paper/MyDORWAY/efile records from
    # tax_filing_submission). Sourced from app/api/tax_filing_submissions.py.
    submissions_in_flight: list[dict[str, Any]] = []


@router.get("/api/workspaces/{workspace_id}/open-items")
async def get_open_items(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    tax_year: int | None = Query(default=None),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> OpenItemsResponse:
    """Return the aggregated open-items dashboard payload.

    Filters:
      * entity_id (optional): restrict to a single entity. If omitted,
        spans all entities the caller has access to.
      * tax_year (optional): restrict to one tax year. If omitted, spans
        all years in the queue.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["1=1"]
        params: dict[str, Any] = {}

        if entity_id:
            where.append("trq.entity_id = :eid")
            params["eid"] = str(entity_id)

        if scope_restriction is not None:
            where.append("trq.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]

        if tax_year:
            where.append("trq.tax_year = :tax_year")
            params["tax_year"] = tax_year

        where_sql = " AND ".join(where)

        # Pull every pending item once; bucket in Python.
        pending_rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        trq.id::text,
                        trq.entity_id::text,
                        e.display_name AS entity_display_name,
                        e.slug AS entity_slug,
                        trq.tax_year,
                        trq.item_type,
                        trq.form_reference,
                        trq.content,
                        trq.confidence,
                        trq.status,
                        trq.submitter_kind,
                        trq.submitter_id,
                        trq.created_at
                    FROM tax_review_queue trq
                    LEFT JOIN entity e ON e.id = trq.entity_id
                    WHERE {where_sql} AND trq.status = 'pending'
                      AND (trq.form_reference IS NULL
                           OR (trq.form_reference NOT LIKE 'DOC_RECEIVED:%'
                               AND trq.form_reference NOT LIKE 'DECISION_RESOLVED:%'))
                    ORDER BY
                        CASE WHEN trq.form_reference LIKE 'CRITICAL%' THEN 0 ELSE 1 END,
                        CASE trq.confidence
                            WHEN 'high' THEN 0
                            WHEN 'medium' THEN 1
                            WHEN 'low' THEN 2
                            ELSE 3
                        END,
                        trq.created_at DESC
                    LIMIT 500
                    """
                ),
                params,
            )
        ).mappings().all()

        resolved_recent = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        trq.id::text,
                        trq.entity_id::text,
                        e.display_name AS entity_display_name,
                        e.slug AS entity_slug,
                        trq.tax_year,
                        trq.item_type,
                        trq.form_reference,
                        trq.content,
                        trq.status,
                        trq.submitter_id,
                        trq.reviewer_uc_uid,
                        trq.resolution_notes,
                        trq.resolved_at
                    FROM tax_review_queue trq
                    LEFT JOIN entity e ON e.id = trq.entity_id
                    WHERE {where_sql}
                      AND trq.status IN ('resolved', 'reviewed', 'rejected')
                      AND trq.resolved_at >= now() - interval '7 days'
                      AND (trq.form_reference IS NULL
                           OR (trq.form_reference NOT LIKE 'DOC_RECEIVED:%'
                               AND trq.form_reference NOT LIKE 'DECISION_RESOLVED:%'))
                    ORDER BY trq.resolved_at DESC
                    LIMIT 50
                    """
                ),
                params,
            )
        ).mappings().all()

        # DOC_RECEIVED markers — to mark Section 2 checklist items as
        # completed. Spans all years since the marker is global. We treat
        # the LATEST row per (doc_key) as authoritative — that lets users
        # toggle receipt on/off by inserting a new marker (rather than
        # DELETE which the least-privilege accounting_ops_app role lacks).
        # status='resolved'  -> received   ; status='rejected' -> un-received.
        doc_markers = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (doc_key)
                        doc_key,
                        created_at AS received_at,
                        reviewer_uc_uid AS marked_by,
                        status
                    FROM (
                        SELECT
                            substring(form_reference from 'DOC_RECEIVED: (.*)$') AS doc_key,
                            created_at,
                            reviewer_uc_uid,
                            status
                        FROM tax_review_queue
                        WHERE form_reference LIKE 'DOC_RECEIVED: %'
                    ) latest
                    ORDER BY doc_key, created_at DESC
                    """
                )
            )
        ).mappings().all()

        # DECISION_RESOLVED markers — same latest-wins pattern.
        decision_markers = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (dec_key)
                        dec_key,
                        full_ref,
                        notes,
                        resolved_at,
                        decided_by,
                        status
                    FROM (
                        SELECT
                            substring(form_reference from 'DECISION_RESOLVED: ([^|]+)') AS dec_key,
                            form_reference AS full_ref,
                            resolution_notes AS notes,
                            created_at AS resolved_at,
                            reviewer_uc_uid AS decided_by,
                            status
                        FROM tax_review_queue
                        WHERE form_reference LIKE 'DECISION_RESOLVED: %'
                    ) latest
                    ORDER BY dec_key, resolved_at DESC
                    """
                )
            )
        ).mappings().all()

        # Submissions in flight (open envelopes / uploads from
        # tax_filing_submission). Same entity / year scoping as the queue
        # query above. Gracefully degrades if the table doesn't yet exist
        # (migration 0011 not applied) — return empty list rather than 500.
        submissions_in_flight_rows: list[dict[str, Any]] = []
        try:
            tfs_where = ["tfs.status NOT IN ('closed')"]
            tfs_params: dict[str, Any] = {}
            if entity_id:
                tfs_where.append("tfs.entity_id = :tfs_eid")
                tfs_params["tfs_eid"] = str(entity_id)
            if scope_restriction is not None:
                tfs_where.append("tfs.entity_id = ANY(:tfs_scope)")
                tfs_params["tfs_scope"] = [str(e) for e in scope_restriction]
            if tax_year:
                tfs_where.append("tfs.tax_year = :tfs_ty")
                tfs_params["tfs_ty"] = tax_year
            tfs_where_sql = " AND ".join(tfs_where)
            submissions_in_flight_rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT
                                tfs.id::text,
                                tfs.entity_id::text,
                                e.display_name AS entity_display_name,
                                e.slug AS entity_slug,
                                tfs.jurisdiction, tfs.tax_year,
                                tfs.submission_title, tfs.forms_enclosed,
                                tfs.filing_method, tfs.tracking_number,
                                tfs.status,
                                tfs.submitted_at, tfs.expected_response_by,
                                tfs.last_tracking_status, tfs.delivered_at,
                                tfs.acknowledged_at
                            FROM tax_filing_submission tfs
                            LEFT JOIN entity e ON e.id = tfs.entity_id
                            WHERE {tfs_where_sql}
                            ORDER BY
                                CASE tfs.status
                                    WHEN 'awaiting_followup' THEN 0
                                    WHEN 'in_transit'        THEN 1
                                    WHEN 'submitted'         THEN 2
                                    WHEN 'delivered'         THEN 3
                                    WHEN 'acknowledged'      THEN 4
                                    WHEN 'draft'             THEN 5
                                    ELSE 6
                                END,
                                tfs.expected_response_by ASC NULLS LAST
                            LIMIT 50
                            """
                        ),
                        tfs_params,
                    )
                ).mappings().all()
            )
        except Exception:
            # Table not present yet, or some other RLS / migration gap —
            # surface as empty rather than failing the whole open-items load.
            submissions_in_flight_rows = []

    # ------- Bucketing -------
    sections: dict[str, list[dict[str, Any]]] = {
        "time_sensitive": [],
        "decision": [],
        "mosin": [],
        "attorney": [],
        "info": [],
    }
    for r in pending_rows:
        row = dict(r)
        bucket = _bucket_for_item(row)
        row["summary"] = _summarize_content(row.get("content"))
        # Drop the heavy content blob from the wire payload — viewers can
        # fetch the full text via the existing GET .../tax-review-queue/{id}
        # endpoint when they expand a card.
        row["content"] = None
        # Format dates as ISO strings (cleaner json)
        if isinstance(row.get("created_at"), dt.datetime):
            row["created_at"] = row["created_at"].isoformat()
        sections[bucket].append(row)

    # Documents catalogue with completion state merged in.
    # Marker convention: status='resolved' => received, status='rejected' => un-received.
    doc_state = {
        (m["doc_key"] or ""): {
            "received_at": (
                m["received_at"].isoformat()
                if isinstance(m["received_at"], dt.datetime)
                else m["received_at"]
            ),
            "marked_by": m["marked_by"],
            "is_received": m["status"] == "resolved",
        }
        for m in doc_markers
    }
    documents_out: list[dict[str, Any]] = []
    received_count = 0
    for doc in _AARON_DOCUMENT_CHECKLIST:
        # Tax-year filter: hide a doc only if it doesn't intersect the
        # requested year. Show all years if no filter.
        if tax_year is not None and tax_year not in doc.get("tax_years", []):
            continue
        d = dict(doc)
        state = doc_state.get(doc["key"])
        if state and state["is_received"]:
            d["status"] = "received"
            d["received_at"] = state["received_at"]
            d["marked_by"] = state["marked_by"]
            received_count += 1
        else:
            d["status"] = "pending"
            d["received_at"] = None
            d["marked_by"] = None
        documents_out.append(d)

    # Decisions catalogue with state merged in.
    # Marker convention: status='resolved' => decided, status='rejected' => cleared.
    dec_state = {
        (m["dec_key"] or "").strip(): {
            "full_ref": m["full_ref"],
            "notes": m["notes"],
            "resolved_at": (
                m["resolved_at"].isoformat()
                if isinstance(m["resolved_at"], dt.datetime)
                else m["resolved_at"]
            ),
            "decided_by": m["decided_by"],
            "is_active": m["status"] == "resolved",
        }
        for m in decision_markers
    }
    decisions_out: list[dict[str, Any]] = []
    decisions_decided = 0
    for dec in _AARON_DECISION_CHECKLIST:
        if tax_year is not None and tax_year not in dec.get("tax_years", []):
            continue
        d = dict(dec)
        state = dec_state.get(dec["key"])
        if state and state["is_active"]:
            full_ref = state["full_ref"] or ""
            # full_ref looks like "DECISION_RESOLVED: <key>|<verdict>"
            verdict = "accept"
            if "|" in full_ref:
                verdict = full_ref.split("|", 1)[1].strip()
            d["status"] = "resolved"
            d["verdict"] = verdict
            d["notes"] = state["notes"]
            d["resolved_at"] = state["resolved_at"]
            d["decided_by"] = state["decided_by"]
            decisions_decided += 1
        else:
            d["status"] = "pending"
            d["verdict"] = None
            d["notes"] = None
            d["resolved_at"] = None
            d["decided_by"] = None
        decisions_out.append(d)

    # Resolved-this-week list with summary
    resolved_out: list[dict[str, Any]] = []
    for r in resolved_recent:
        row = dict(r)
        row["summary"] = _summarize_content(row.get("content"))
        row["content"] = None
        if isinstance(row.get("resolved_at"), dt.datetime):
            row["resolved_at"] = row["resolved_at"].isoformat()
        resolved_out.append(row)

    # Submissions in flight — flatten datetimes for the wire
    submissions_out: list[dict[str, Any]] = []
    for r in submissions_in_flight_rows:
        row = dict(r)
        for k in ("submitted_at", "expected_response_by", "delivered_at",
                  "acknowledged_at"):
            if isinstance(row.get(k), dt.datetime):
                row[k] = row[k].isoformat()
        submissions_out.append(row)

    awaiting_followup_count = sum(
        1 for s in submissions_out if s.get("status") == "awaiting_followup"
    )

    counts = {
        "time_sensitive": len(sections["time_sensitive"]),
        "documents_pending": len(documents_out) - received_count,
        "documents_received": received_count,
        "decisions_pending": len(decisions_out) - decisions_decided,
        "decisions_decided": decisions_decided,
        "waiting_on_mosin": len(sections["mosin"]),
        "waiting_on_attorney": len(sections["attorney"]),
        "informational": len(sections["info"]),
        "resolved_this_week": len(resolved_out),
        "submissions_in_flight": len(submissions_out),
        "submissions_awaiting_followup": awaiting_followup_count,
        # The headline number that drives the dashboard widget — only items
        # that require AARON to act, not things waiting on someone else.
        # Submissions awaiting follow-up count as action-required (Aaron must
        # call the taxing authority).
        "action_required": (
            len(sections["time_sensitive"])
            + (len(documents_out) - received_count)
            + (len(decisions_out) - decisions_decided)
            + awaiting_followup_count
        ),
    }

    return OpenItemsResponse(
        workspace_id=str(workspace_id),
        entity_id=str(entity_id) if entity_id else None,
        as_of=dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        counts=counts,
        time_sensitive=sections["time_sensitive"],
        documents=documents_out,
        decisions=decisions_out,
        waiting_on_mosin=sections["mosin"],
        waiting_on_attorney=sections["attorney"],
        resolved_this_week=resolved_out,
        submissions_in_flight=submissions_out,
    )


# ---------------------------------------------------------------------------
# Mark a document as received / un-received
# ---------------------------------------------------------------------------


class DocReceivedBody(BaseModel):
    key: str
    received: bool = True
    note: str | None = None


@router.post("/api/workspaces/{workspace_id}/open-items/document-received")
async def mark_document_received(
    workspace_id: UUID,
    body: DocReceivedBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Toggle the 'received' state on a document checklist item.

    Implementation: writes a memo row to tax_review_queue with
    form_reference='DOC_RECEIVED: <key>' so the same audit infrastructure
    that backs the rest of the queue covers this state too. Toggling off
    deletes the marker rows.

    Role floor: accountant — Aaron is owner; CPA accountant is also OK
    so the CPA can confirm receipt if the document was forwarded directly.
    """
    require_role(workspace, "accountant")
    key = (body.key or "").strip()
    if not key:
        return {"ok": False, "error": "key is required"}

    # Use the first entity_id in scope as a placeholder — the marker row
    # isn't entity-specific, but the table requires entity_id NOT NULL.
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Pick any entity we can see (preferring MU Inc by slug).
        ent_row = (
            await session.execute(
                text(
                    """
                    SELECT id::text
                    FROM entity
                    ORDER BY CASE WHEN slug = 'magic-unicorn-inc' THEN 0 ELSE 1 END,
                             display_name
                    LIMIT 1
                    """
                )
            )
        ).first()
        if not ent_row:
            return {"ok": False, "error": "no entity available"}
        ent_id = ent_row[0]

        # status='resolved' marks the doc as received; status='rejected' un-receives it.
        # The aggregator picks the latest row per doc_key.
        marker_status = "resolved" if body.received else "rejected"
        await session.execute(
            text(
                """
                INSERT INTO tax_review_queue (
                    id, workspace_id, entity_id, tax_year,
                    item_type, form_reference, content, confidence,
                    status, submitter_kind, submitter_id,
                    reviewer_uc_uid, resolution_notes,
                    created_at, resolved_at
                )
                VALUES (
                    gen_random_uuid(), :wid, :eid, 0,
                    'memo', :form_ref,
                    jsonb_build_object('doc_key', CAST(:key AS text),
                                       'note', CAST(:note AS text),
                                       'received', CAST(:received AS boolean)),
                    'high', :marker_status, 'human', :uc_uid,
                    :uc_uid, CAST(:note AS text), now(), now()
                )
                """
            ),
            {
                "wid": str(workspace_id),
                "eid": ent_id,
                "form_ref": f"DOC_RECEIVED: {key}",
                "key": key,
                "note": body.note,
                "received": body.received,
                "marker_status": marker_status,
                "uc_uid": workspace.user_uc_uid,
            },
        )

    return {"ok": True, "key": key, "received": body.received}


# ---------------------------------------------------------------------------
# Resolve a decision (accept recommendation / different choice)
# ---------------------------------------------------------------------------


class DecisionBody(BaseModel):
    key: str
    verdict: str  # "accept" | "other"
    notes: str | None = None


@router.post("/api/workspaces/{workspace_id}/open-items/decision")
async def record_decision(
    workspace_id: UUID,
    body: DecisionBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Persist Aaron's verdict on a decision item.

    Writes a memo row with form_reference='DECISION_RESOLVED: <key>|<verdict>'
    so the same audit table covers it. The CPA can override later by
    posting a new memo (rare).

    Role floor: accountant (owner / admin / accountant all pass).
    """
    require_role(workspace, "accountant")
    key = (body.key or "").strip()
    verdict = (body.verdict or "accept").strip().lower()
    if verdict not in ("accept", "other"):
        return {"ok": False, "error": "verdict must be 'accept' or 'other'"}
    if not key:
        return {"ok": False, "error": "key is required"}

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        ent_row = (
            await session.execute(
                text(
                    """
                    SELECT id::text
                    FROM entity
                    ORDER BY CASE WHEN slug = 'magic-unicorn-inc' THEN 0 ELSE 1 END,
                             display_name
                    LIMIT 1
                    """
                )
            )
        ).first()
        if not ent_row:
            return {"ok": False, "error": "no entity available"}
        ent_id = ent_row[0]

        # Latest INSERT wins (no DELETE needed — runtime role can't DELETE).
        # The aggregator SELECT DISTINCT ON (dec_key) … ORDER BY resolved_at DESC
        # picks the freshest row, so re-deciding just inserts a new marker.
        await session.execute(
            text(
                """
                INSERT INTO tax_review_queue (
                    id, workspace_id, entity_id, tax_year,
                    item_type, form_reference, content, confidence,
                    status, submitter_kind, submitter_id,
                    reviewer_uc_uid, resolution_notes,
                    created_at, resolved_at
                )
                VALUES (
                    gen_random_uuid(), :wid, :eid, 0,
                    'memo', :form_ref,
                    jsonb_build_object('decision_key', CAST(:key AS text),
                                       'verdict', CAST(:verdict AS text),
                                       'notes', CAST(:notes AS text)),
                    'high', 'resolved', 'human', :uc_uid,
                    :uc_uid, CAST(:notes AS text), now(), now()
                )
                """
            ),
            {
                "wid": str(workspace_id),
                "eid": ent_id,
                "form_ref": f"DECISION_RESOLVED: {key}|{verdict}",
                "key": key,
                "verdict": verdict,
                "notes": body.notes,
                "uc_uid": workspace.user_uc_uid,
            },
        )

    return {"ok": True, "key": key, "verdict": verdict}
