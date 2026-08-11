#!/usr/bin/env python3
"""Generate DRAFT pre-filled tax submission PDFs for the configured entity, all open years.

Produces 8 PDFs (4 years × 2 forms) plus cover sheets + mailing labels in
drafts/forms/. Every PDF is DRAFT-watermarked and bears a "do not file" banner.

Idempotent: re-running just regenerates from current platform state. The
output filenames are stable, so re-runs overwrite the existing artifacts.

Usage:
    python scripts/generate_submission_drafts.py
    python scripts/generate_submission_drafts.py --years 2023        # one year
    python scripts/generate_submission_drafts.py --years 2022,2023   # multi
    python scripts/generate_submission_drafts.py --entity example    # a specific entity

Data sources:
- For the MVP run on local dev, we use synthetic data (federal taxable income
  derived from the entity's tax_status row). When run against the live app,
  the operator can swap in a HTTP call to /api/workspaces/{wid}/cpa-export.

Note: This script does NOT submit anything. The output is for human review.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make `app` importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.exports.cover_sheet import (  # noqa: E402
    CoverSheetData,
    generate_cover_sheet_pdf,
)
from app.exports.form_prefill import generate_form_pdf  # noqa: E402
from app.exports.mailing_label import (  # noqa: E402
    MailingLabelData,
    generate_mailing_label_pdf,
)
from app.exports.sc_form_prefill import (  # noqa: E402
    generate_sc1120_pdf,
    generate_sc1120v_pdf,
)

# ---------------------------------------------------------------------------
# Example entity fixtures. These are generic placeholders for the Community
# Edition — override the EIN via the ENTITY_EIN env var or add real
# entities explicitly to ENTITY_FIXTURES. No real entity data ships here.
# ---------------------------------------------------------------------------

ENTITY_FIXTURES: dict[str, dict] = {
    "example": {
        "display_name": "Example Corp Inc.",
        "ein": os.getenv("ENTITY_EIN", "12-3456789"),
        "mailing_address": "123 Example Street",
        "city_state_zip": "Anytown, ST 00000",
    },
}

# Per-year synthetic cpa_package (used only when the live cpa-export sidecar is
# unavailable). The numbers come from the books-reconstruction work captured in
# drafts/books-reconciliation-2026-05-30.md.
SYNTHETIC_PACKAGES: dict[tuple[str, int], dict] = {
    ("example", 2022): {
        "income": {"lines": []},
        "deductions": {"lines": []},
        "taxable_income_before_nol_cents": 0,  # No activity TY 2022
    },
    ("example", 2023): {
        "income": {"lines": [
            {"line": "1a", "amount_cents": 0},
            {"line": "11", "amount_cents": 0},
        ]},
        "deductions": {"lines": [
            {"line": "26", "amount_cents": 367079},  # Operating costs
            {"line": "27", "amount_cents": 367079},  # Total deductions
        ]},
        "taxable_income_before_nol_cents": -367079,
    },
    ("example", 2024): {
        "income": {"lines": [
            {"line": "1a", "amount_cents": 0},
            {"line": "11", "amount_cents": 0},
        ]},
        "deductions": {"lines": []},
        "taxable_income_before_nol_cents": 0,  # Placeholder; CPA fills
    },
    ("example", 2025): {
        "income": {"lines": []},
        "deductions": {"lines": []},
        "taxable_income_before_nol_cents": 0,  # Placeholder; CPA fills
    },
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_for_entity_year(
    entity_key: str,
    tax_year: int,
    output_dir: Path,
) -> dict:
    """Generate the form bundle for one (entity, tax_year). Returns a manifest."""
    fixture = ENTITY_FIXTURES[entity_key]
    cpa_pkg = SYNTHETIC_PACKAGES.get((entity_key, tax_year))
    entity_slug = entity_key.replace("_", "-")
    manifest: dict[str, list[dict]] = {"artifacts": []}

    # 1. SC1120 (state) — overlay PDF
    try:
        sc_pdf, sc_ctx = generate_sc1120_pdf(
            tax_year=tax_year,
            entity_display_name=fixture["display_name"],
            ein=fixture["ein"],
            mailing_address=fixture["mailing_address"],
            city_state_zip=fixture["city_state_zip"],
            queue_items=[],
            cpa_package=cpa_pkg,
        )
        sc_path = output_dir / f"{entity_slug}-{tax_year}-SC1120-prefilled.pdf"
        sc_path.write_bytes(sc_pdf)
        manifest["artifacts"].append({
            "form": "SC1120",
            "path": str(sc_path),
            "bytes": len(sc_pdf),
            "filled_lines": sc_ctx.filled_lines,
        })
    except FileNotFoundError as exc:
        manifest["artifacts"].append({
            "form": "SC1120",
            "error": str(exc),
        })

    # 2. SC1120-V (payment voucher)
    try:
        # License fee is the statutory minimum $25 for a corp with no capital
        # stock balance; a year with positive SC tax would add it on top.
        # For this MVP, we always tender $25 = 2500 cents.
        v_pdf = generate_sc1120v_pdf(
            tax_year=tax_year,
            entity_display_name=fixture["display_name"],
            ein=fixture["ein"],
            mailing_address=fixture["mailing_address"],
            city_state_zip=fixture["city_state_zip"],
            payment_amount_cents=2500,
        )
        v_path = output_dir / f"{entity_slug}-{tax_year}-SC1120V-prefilled.pdf"
        v_path.write_bytes(v_pdf)
        manifest["artifacts"].append({
            "form": "SC1120V",
            "path": str(v_path),
            "bytes": len(v_pdf),
        })
    except FileNotFoundError as exc:
        manifest["artifacts"].append({
            "form": "SC1120V",
            "error": str(exc),
        })

    # 3. Form 1120 (federal income tax return)
    try:
        f_pdf, f_ctype = generate_form_pdf(
            form_name="1120",
            tax_year=tax_year,
            entity_display_name=fixture["display_name"],
            ein=fixture["ein"],
            queue_items=[],
            cpa_package=cpa_pkg,
        )
        f_path = output_dir / f"{entity_slug}-{tax_year}-Form1120-prefilled.pdf"
        f_path.write_bytes(f_pdf)
        manifest["artifacts"].append({
            "form": "Form 1120",
            "path": str(f_path),
            "bytes": len(f_pdf),
            "content_type": f_ctype,
        })
    except FileNotFoundError as exc:
        manifest["artifacts"].append({
            "form": "Form 1120",
            "error": str(exc),
        })

    # 4. SCDOR cover sheet for the SC packet
    sc_cs = generate_cover_sheet_pdf(CoverSheetData(
        submission_title=f"TY {tax_year} SC1120 — Late Filing Cure Packet",
        tax_year=tax_year,
        entity_name=fixture["display_name"],
        entity_ein=fixture["ein"],
        to_office_key="scdor_corporate",
        enclosures=[
            f"SC1120 (TY {tax_year}, CPA-signed)",
            "SC1120-V payment voucher ($25.00 license fee)",
            "Check for $25.00, payable to South Carolina Department of Revenue",
            "Form SC2848 Power of Attorney designating the entity's CPA"
            if tax_year == 2023 else "Form SC2848 POA (if not already on file)",
            "Reasonable-cause cure letter (signed by the entity's CPA)"
            if tax_year == 2023 else None,
        ],
        notes=(
            "Tracking number to be added by the operator after USPS Certified Mail "
            "purchase. Record the tracking number via the "
            "/open-items Submissions surface when assigned."
        ),
    ))
    cs_path = output_dir / f"{entity_slug}-{tax_year}-SCDOR-cover-sheet.pdf"
    cs_path.write_bytes(sc_cs)
    manifest["artifacts"].append({
        "form": "Cover sheet (SCDOR)",
        "path": str(cs_path),
        "bytes": len(sc_cs),
    })

    # 5. SCDOR mailing label
    sc_label = generate_mailing_label_pdf(MailingLabelData(
        to_office_key="scdor_corporate",
        sender_reference=(
            f"{fixture['display_name']} EIN {fixture['ein']} — TY {tax_year} SC1120 cure"
        ),
        tracking_number=None,  # the operator writes the tracking number in after USPS
    ))
    label_path = output_dir / f"{entity_slug}-{tax_year}-SCDOR-mailing-label.pdf"
    label_path.write_bytes(sc_label)
    manifest["artifacts"].append({
        "form": "Mailing label (SCDOR)",
        "path": str(label_path),
        "bytes": len(sc_label),
    })

    # 6. IRS cover sheet for the federal packet
    irs_cs = generate_cover_sheet_pdf(CoverSheetData(
        submission_title=f"TY {tax_year} Form 1120 — Late Filing",
        tax_year=tax_year,
        entity_name=fixture["display_name"],
        entity_ein=fixture["ein"],
        to_office_key="irs_1120_sc_nopayment",  # Loss year → no payment
        enclosures=[
            f"Form 1120 (TY {tax_year}, CPA-signed)",
            "Schedule L (balance sheet)",
            "Form 1125-E (officer compensation)" if tax_year >= 2024 else None,
            "Statement of Officer Information",
            "Reasonable-cause explanation (if pre-emptive FTF mitigation)",
        ],
        notes="Tracking number to be added by the operator after USPS Certified Mail purchase.",
    ))
    irs_cs_path = output_dir / f"{entity_slug}-{tax_year}-IRS-cover-sheet.pdf"
    irs_cs_path.write_bytes(irs_cs)
    manifest["artifacts"].append({
        "form": "Cover sheet (IRS)",
        "path": str(irs_cs_path),
        "bytes": len(irs_cs),
    })

    # 7. IRS mailing label
    irs_label = generate_mailing_label_pdf(MailingLabelData(
        to_office_key="irs_1120_sc_nopayment",
        sender_reference=(
            f"{fixture['display_name']} EIN {fixture['ein']} — TY {tax_year} Form 1120"
        ),
        tracking_number=None,
    ))
    irs_label_path = output_dir / f"{entity_slug}-{tax_year}-IRS-mailing-label.pdf"
    irs_label_path.write_bytes(irs_label)
    manifest["artifacts"].append({
        "form": "Mailing label (IRS)",
        "path": str(irs_label_path),
        "bytes": len(irs_label),
    })

    return manifest


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--years", default="2022,2023,2024,2025",
        help="Comma-separated tax years to generate (default: 2022,2023,2024,2025)",
    )
    p.add_argument(
        "--entity", default="example",
        help="Entity key (must be in ENTITY_FIXTURES). Default: example",
    )
    p.add_argument(
        "--output-dir", default=None,
        help="Output directory. Default: drafts/forms/ in the repo root.",
    )
    args = p.parse_args()

    years = [int(y.strip()) for y in args.years.split(",") if y.strip()]
    entity_key = args.entity
    if entity_key not in ENTITY_FIXTURES:
        print(f"ERROR: unknown entity '{entity_key}'", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir) if args.output_dir else (ROOT / "drafts" / "forms")
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating drafts for {entity_key} years {years}")
    print(f"Output: {output_dir}")
    print()

    total_artifacts = 0
    total_bytes = 0
    for year in years:
        print(f"=== TY {year} ===")
        manifest = generate_for_entity_year(entity_key, year, output_dir)
        for art in manifest["artifacts"]:
            if "error" in art:
                print(f"  ERROR  {art['form']}: {art['error']}")
            else:
                total_artifacts += 1
                total_bytes += art["bytes"]
                fp = art["path"]
                fl = art.get("filled_lines")
                fl_note = f" (lines={','.join(fl)})" if fl else ""
                print(f"  OK     {art['form']:<22} {art['bytes']:>8,}B  {fp}{fl_note}")
        print()

    print(f"Generated {total_artifacts} artifacts, {total_bytes:,} bytes total.")
    print()
    print("NOTE: All artifacts carry a DRAFT watermark. None are file-ready.")
    print("      Pass to a licensed CPA for review, completion, and signature.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
