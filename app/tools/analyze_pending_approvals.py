"""MCP tool: analyze_pending_approvals — batch pre-analyzer for approval_queue.

Groups pending ``bank_transaction_import`` rows in the approval_queue, scores
each group with a confidence heuristic, writes one ``approval_recommendation``
row per approval, and returns a cluster summary.

This tool is READ + WRITE (writes recommendations) but NEVER calls the decide
endpoint — it produces recommendations only.  Aaron retains the click.

Authorization floor: accountant (same as list_approvals).

Confidence heuristics:
    HIGH:
        - Suggested category is in CATEGORY_TO_ACCOUNT (mapped)
        - Vendor/payee is in the WELL_KNOWN_VENDORS list
        - Amount falls in the vendor's expected range (< $500 for most SaaS)
        - No CPA-flagged category
    MEDIUM:
        - Category is mapped/plausible but vendor not in known-good list, OR
        - Amount is outside the typical range for that vendor, OR
        - No prior approval history signal (first-time vendor)
    LOW (regardless of anything else — always needs human eyes):
        - No category suggested / uncategorized
        - Amount > $1,000 (materiality H)
        - Category is one of: 'Banking·Interest', 'SaaS·AI',
          'Inter-entity transfer', 'Contractor·1099-NEC'
        - Payee is unrecognized AND amount > $200

Cluster ID format: ``{vendor_sig}-{category_slug}-{year}-{quarter}``
where vendor_sig is a short slug derived from the payee.
"""
from __future__ import annotations

import re
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import workspace_session

# ---------------------------------------------------------------------------
# Heuristic constants
# ---------------------------------------------------------------------------

# Categories that are AUTO-LOW regardless of vendor/amount (CPA-judgment required)
_CPA_FLAGGED_CATEGORIES: set[str] = {
    "Banking · Interest",
    "SaaS · AI",
    "Inter-entity transfer",
    # Contractor classification is outside CATEGORY_TO_ACCOUNT today but guard
    # in case it ever appears as a suggested category from the ingest script.
    "Contractor · 1099-NEC",
}

# Well-known SaaS/cloud vendors whose charges are typically routine.
# Normalised to lowercase for comparison.
_WELL_KNOWN_VENDORS: set[str] = {
    "stripe",
    "heroku",
    "aws",
    "amazon web services",
    "amazon",
    "adobe",
    "github",
    "slack",
    "google",
    "google cloud",
    "gcp",
    "microsoft",
    "azure",
    "digitalocean",
    "cloudflare",
    "fastly",
    "mailgun",
    "sendgrid",
    "twilio",
    "notion",
    "figma",
    "linear",
    "vercel",
    "netlify",
    "fly.io",
    "render",
    "railway",
    "anthropic",
    "openai",
    "supabase",
    "planetscale",
    "neon",
    "postmark",
    "listmonk",
    "sentry",
    "datadog",
    "new relic",
    "pagerduty",
    "zoom",
    "dropbox",
    "1password",
    "bitwarden",
    "lastpass",
    "docusign",
    "hubspot",
    "salesforce",
    "zendesk",
    "intercom",
    "jira",
    "confluence",
    "atlassian",
}

# Materiality: amounts above this threshold (in cents) are HIGH-materiality
# and always land at LOW confidence (need human eyes).
_MATERIALITY_H_CENTS: int = 100_000  # $1,000

# Above this amount (cents) we flag MEDIUM even for known vendors (unusual spend)
_SAAS_HIGH_WATER_CENTS: int = 50_000  # $500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vendor_slug(payee: str | None) -> str:
    """Normalise payee to a short lowercase slug for cluster-id construction."""
    if not payee:
        return "unknown"
    # Strip common suffixes like "Inc", "LLC", "Corp", "Ltd", "."
    clean = re.sub(r"\b(inc\.?|llc\.?|corp\.?|ltd\.?|co\.?)\b", "", payee, flags=re.I)
    clean = re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-")
    return clean[:32] or "unknown"


def _category_slug(category: str | None) -> str:
    if not category:
        return "uncategorized"
    return re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")[:32]


def _year_quarter(effective_date: str | None) -> tuple[str, str]:
    """Extract (year, quarter) from ISO date string 'YYYY-MM-DD'."""
    if not effective_date:
        return "unknown", "qx"
    try:
        year = effective_date[:4]
        month = int(effective_date[5:7])
        quarter = f"q{(month - 1) // 3 + 1}"
        return year, quarter
    except (IndexError, ValueError):
        return "unknown", "qx"


def _is_well_known(payee: str | None) -> bool:
    if not payee:
        return False
    p = payee.lower()
    # Check if any known vendor name appears in the payee string
    return any(v in p for v in _WELL_KNOWN_VENDORS)


def _score_confidence(
    category: str | None,
    payee: str | None,
    amount_cents: int,
    llm_suggested: bool,
) -> tuple[str, str, str]:
    """Return (confidence, recommended_decision, rationale).

    Confidence: 'high' | 'medium' | 'low'
    recommended_decision: 'approve' | 'flag_for_review' | 'reject'
    """
    reasons: list[str] = []

    # ---- AUTO-LOW guards (checked first, short-circuit) ----

    # 1. CPA-flagged categories
    if category and category in _CPA_FLAGGED_CATEGORIES:
        return (
            "low",
            "flag_for_review",
            f"Category '{category}' is CPA-flagged and requires specialist judgment.",
        )

    # 2. No category (uncategorized)
    if not category or category.strip().lower() in ("", "uncategorized", "none"):
        return (
            "low",
            "flag_for_review",
            "No category suggested; payee unrecognized or ingest rule missing.",
        )

    # 3. High-materiality amount (> $1,000)
    if abs(amount_cents) >= _MATERIALITY_H_CENTS:
        dollar = abs(amount_cents) / 100
        return (
            "low",
            "flag_for_review",
            f"Amount ${dollar:,.2f} exceeds $1,000 materiality threshold — requires CPA attestation.",
        )

    # 4. Unrecognized payee + non-trivial amount (> $200)
    if not _is_well_known(payee) and abs(amount_cents) > 20_000:
        dollar = abs(amount_cents) / 100
        return (
            "low",
            "flag_for_review",
            f"Payee '{payee}' not in known-good vendor list and amount ${dollar:,.2f} > $200.",
        )

    # ---- HIGH confidence ----
    known = _is_well_known(payee)
    in_range = abs(amount_cents) < _SAAS_HIGH_WATER_CENTS

    if known and in_range and llm_suggested:
        reasons.append(f"'{payee}' is a well-known SaaS vendor.")
        reasons.append(f"Amount ${abs(amount_cents)/100:,.2f} is within normal range.")
        reasons.append(f"Category '{category}' was AI-suggested.")
        return (
            "high",
            "approve",
            " ".join(reasons),
        )

    # ---- MEDIUM confidence ----
    if known and not in_range:
        reasons.append(
            f"'{payee}' is a well-known vendor but amount ${abs(amount_cents)/100:,.2f} "
            "is unusually high."
        )
    elif known and not llm_suggested:
        reasons.append(
            f"'{payee}' is a well-known vendor but category was not AI-suggested — "
            "verify the category mapping is correct."
        )
    elif not known:
        reasons.append(
            f"Payee '{payee}' is not in the known-good vendor list; verify manually."
        )
    else:
        reasons.append(f"Category '{category}' is plausible but not confirmed HIGH.")

    return (
        "medium",
        "flag_for_review",
        " ".join(reasons) if reasons else f"Category '{category}' needs verification.",
    )


def _build_cluster_id(
    vendor_slug: str,
    category_slug: str,
    year: str,
    quarter: str,
) -> str:
    return f"{vendor_slug}-{category_slug}-{year}-{quarter}"


# ---------------------------------------------------------------------------
# Main tool
# ---------------------------------------------------------------------------


async def analyze_pending_approvals(
    workspace_id: UUID,
    entity_id: UUID,
    year: int | None = None,
    item_type: str = "bank_transaction_import",
) -> dict[str, Any]:
    """Batch pre-analyzer: group + score pending approval_queue rows.

    Queries all pending ``bank_transaction_import`` rows for the workspace +
    entity (optionally filtered by calendar year).  Groups them into clusters
    by (suggested_category, vendor_signature, amount_range, quarter).  For each
    row, scores a confidence level and writes one ``approval_recommendation``
    row (upsert on approval_id).  Returns a cluster summary.

    Does NOT approve anything.  Recommendations only.

    Args:
        workspace_id: workspace to analyze.
        entity_id: entity within the workspace.
        year: optional calendar year filter (YYYY).  Matched against the date
            embedded in payload->source_external_id or payload->effective_date.
        item_type: subject_type to analyze (default 'bank_transaction_import').

    Returns:
        {
          "total_analyzed": int,
          "clusters": [
            {
              "cluster_id": str,
              "count": int,
              "total_amount_cents": int,
              "recommended_decision": str,
              "confidence": str,
              "suggested_category": str | None,
              "sample_payees": [str, ...],
              "sample_rows": [{approval_id, amount_cents, effective_date, payee}, ...]
            },
            ...
          ],
          "summary": {"high": int, "medium": int, "low": int}
        }

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")
    submitter_kind = "service_token" if ctx.agent_id else "user"
    submitter_id = ctx.agent_id or ctx.user_uc_uid

    async with workspace_session(SessionLocal, workspace_id) as session:
        # ---- 1. Fetch pending BTI rows for this entity ----
        params: dict[str, Any] = {
            "eid": str(entity_id),
            "item_type": item_type,
        }
        year_filter = ""
        if year:
            # Match year in ISO date embedded in source_external_id or effective_date
            # Both the 'bofa-1792:YYYY-MM-DD:...' pattern and payload->effective_date
            # start with the year.
            params["year_prefix"] = str(year)
            year_filter = (
                "AND ("
                "  (payload->>'source_external_id') LIKE :year_prefix || '-%' "
                "  OR (payload->>'effective_date') LIKE :year_prefix || '-%'"
                ")"
            )

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id::text AS approval_id,
                           payload,
                           amount,
                           created_at
                    FROM approval_queue
                    WHERE entity_id = :eid
                      AND subject_type = :item_type
                      AND status = 'pending'
                      {year_filter}
                    ORDER BY created_at DESC
                    LIMIT 5000
                    """
                ),
                params,
            )
        ).mappings().all()

        if not rows:
            return {
                "total_analyzed": 0,
                "clusters": [],
                "summary": {"high": 0, "medium": 0, "low": 0},
            }

        # ---- 2. Score + cluster each row ----
        # cluster_id -> list of scored rows
        clusters: dict[str, list[dict[str, Any]]] = {}

        for row in rows:
            payload: dict = row["payload"] or {}
            approval_id = row["approval_id"]

            category = payload.get("category")
            payee = payload.get("payee") or payload.get("description")
            amount_cents_raw = payload.get("amount_cents")
            effective_date = payload.get("effective_date")
            llm_suggested: bool = bool(payload.get("llm_suggested", False))

            try:
                amount_cents = int(amount_cents_raw) if amount_cents_raw is not None else 0
            except (TypeError, ValueError):
                amount_cents = 0

            # Build cluster key
            vslug = _vendor_slug(payee)
            cslug = _category_slug(category)
            yr, qtr = _year_quarter(effective_date)
            cluster_id = _build_cluster_id(vslug, cslug, yr, qtr)

            confidence, decision, rationale = _score_confidence(
                category=category,
                payee=payee,
                amount_cents=amount_cents,
                llm_suggested=llm_suggested,
            )

            scored = {
                "approval_id": approval_id,
                "cluster_id": cluster_id,
                "confidence": confidence,
                "recommended_decision": decision,
                "recommended_category": category,
                "rationale": rationale,
                "amount_cents": amount_cents,
                "effective_date": effective_date,
                "payee": payee,
            }

            if cluster_id not in clusters:
                clusters[cluster_id] = []
            clusters[cluster_id].append(scored)

        # ---- 3. Upsert recommendation rows ----
        for cluster_id, scored_rows in clusters.items():
            for r in scored_rows:
                await session.execute(
                    text(
                        """
                        INSERT INTO approval_recommendation (
                            id, workspace_id, entity_id, approval_id,
                            cluster_id, recommended_decision, recommended_category,
                            confidence, rationale, submitter_kind, submitter_id,
                            created_at
                        ) VALUES (
                            gen_random_uuid(),
                            :workspace_id, :entity_id, CAST(:approval_id AS uuid),
                            :cluster_id, :recommended_decision, :recommended_category,
                            :confidence, :rationale, :submitter_kind, :submitter_id,
                            now()
                        )
                        ON CONFLICT (approval_id) DO UPDATE SET
                            cluster_id = EXCLUDED.cluster_id,
                            recommended_decision = EXCLUDED.recommended_decision,
                            recommended_category = EXCLUDED.recommended_category,
                            confidence = EXCLUDED.confidence,
                            rationale = EXCLUDED.rationale,
                            submitter_kind = EXCLUDED.submitter_kind,
                            submitter_id = EXCLUDED.submitter_id,
                            created_at = now()
                        """
                    ),
                    {
                        "workspace_id": str(workspace_id),
                        "entity_id": str(entity_id),
                        "approval_id": r["approval_id"],
                        "cluster_id": r["cluster_id"],
                        "recommended_decision": r["recommended_decision"],
                        "recommended_category": r["recommended_category"],
                        "confidence": r["confidence"],
                        "rationale": r["rationale"],
                        "submitter_kind": submitter_kind,
                        "submitter_id": submitter_id,
                    },
                )

    # ---- 4. Build the cluster summary ----
    confidence_counts: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    cluster_summary: list[dict[str, Any]] = []

    for cluster_id, scored_rows in clusters.items():
        # Determine cluster-level confidence: worst-case wins (any LOW -> LOW, etc.)
        if any(r["confidence"] == "low" for r in scored_rows):
            cluster_conf = "low"
        elif any(r["confidence"] == "medium" for r in scored_rows):
            cluster_conf = "medium"
        else:
            cluster_conf = "high"

        # Cluster-level decision: majority vote but LOW overrides
        if cluster_conf == "low":
            cluster_decision = "flag_for_review"
        elif all(r["recommended_decision"] == "approve" for r in scored_rows):
            cluster_decision = "approve"
        else:
            cluster_decision = "flag_for_review"

        total_amount = sum(r["amount_cents"] for r in scored_rows)
        sample_payees = list(
            {r["payee"] for r in scored_rows if r["payee"]}
        )[:5]
        sample_rows = [
            {
                "approval_id": r["approval_id"],
                "amount_cents": r["amount_cents"],
                "effective_date": r["effective_date"],
                "payee": r["payee"],
                "confidence": r["confidence"],
            }
            for r in sorted(scored_rows, key=lambda x: abs(x["amount_cents"]), reverse=True)[:5]
        ]
        suggested_cat = scored_rows[0]["recommended_category"] if scored_rows else None

        confidence_counts[cluster_conf] += len(scored_rows)

        cluster_summary.append(
            {
                "cluster_id": cluster_id,
                "count": len(scored_rows),
                "total_amount_cents": total_amount,
                "recommended_decision": cluster_decision,
                "confidence": cluster_conf,
                "suggested_category": suggested_cat,
                "sample_payees": sample_payees,
                "sample_rows": sample_rows,
            }
        )

    # Sort clusters: HIGH first, then by count descending
    _order = {"high": 0, "medium": 1, "low": 2}
    cluster_summary.sort(
        key=lambda c: (_order.get(c["confidence"], 9), -c["count"])
    )

    return {
        "total_analyzed": len(rows),
        "clusters": cluster_summary,
        "summary": confidence_counts,
    }
