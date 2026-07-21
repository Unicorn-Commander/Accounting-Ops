#!/usr/bin/env bash
# download_irs_forms.sh — fetch official IRS PDFs for pre-fill.
#
# Downloads to data/irs_forms/. The forms are gitignored (too large for git;
# IRS redistribution is unrestricted for informational use per IRS Publication 1).
#
# Usage:
#   bash scripts/download_irs_forms.sh          # TY 2024 only (default)
#   bash scripts/download_irs_forms.sh 2023     # also fetch TY 2023
#   bash scripts/download_irs_forms.sh 2023 2024
#
# The script is idempotent — if a file already exists it prints a checksum
# and skips the download.
#
# IRS form URLs (stable; the IRS keeps old-year forms in the same path):
#   https://www.irs.gov/pub/irs-pdf/f1120.pdf       — current year Form 1120
#   https://www.irs.gov/pub/irs-prior/f1120--{year}.pdf  — prior years
#   https://www.irs.gov/pub/irs-pdf/f1125e.pdf      — current year 1125-E
#   https://www.irs.gov/pub/irs-prior/f1125e--{year}.pdf — prior years
#
# Requirements: curl (or wget), sha256sum (or shasum on macOS).
set -euo pipefail

FORMS_DIR="$(cd "$(dirname "$0")/.." && pwd)/data/irs_forms"
mkdir -p "$FORMS_DIR"

YEARS=("${@:-2024}")

# Detect checksum tool
if command -v sha256sum &>/dev/null; then
    CKSUM="sha256sum"
elif command -v shasum &>/dev/null; then
    CKSUM="shasum -a 256"
else
    CKSUM="echo (no checksum tool found)"
fi

# Detect download tool
if command -v curl &>/dev/null; then
    DOWNLOAD_CMD="curl -fsSL --retry 3 --retry-delay 2 -o"
elif command -v wget &>/dev/null; then
    DOWNLOAD_CMD="wget -q -O"
else
    echo "ERROR: Neither curl nor wget found. Install one and retry." >&2
    exit 1
fi

download_form() {
    local dest="$1"
    local url="$2"
    local label="$3"

    if [[ -f "$dest" ]]; then
        echo "  SKIP  $label — already present ($dest)"
        $CKSUM "$dest"
        return 0
    fi

    echo "  FETCH $label"
    echo "        $url"
    echo "        -> $dest"
    $DOWNLOAD_CMD "$dest" "$url"
    $CKSUM "$dest"
    echo "  OK    $label downloaded."
}

# IRS HEAD check helper — verify URL exists before downloading
head_check() {
    local url="$1"
    if command -v curl &>/dev/null; then
        curl -fsI --retry 2 --retry-delay 1 "$url" -o /dev/null 2>&1 && return 0
    elif command -v wget &>/dev/null; then
        wget -q --spider "$url" 2>&1 && return 0
    fi
    return 1
}

for YEAR in "${YEARS[@]}"; do
    echo ""
    echo "=== Tax Year $YEAR ==="

    # Form 1120
    # NOTE(2026-07-10): irs-pdf/f1120.pdf is now the 2025 revision - the old
    # YEAR==2024 shortcut silently saved the WRONG revision as f1120_2024.pdf.
    if [[ "$YEAR" == "2025" ]]; then
        F1120_URL="https://www.irs.gov/pub/irs-pdf/f1120.pdf"
    else
        F1120_URL="https://www.irs.gov/pub/irs-prior/f1120--${YEAR}.pdf"
    fi
    F1120_DEST="$FORMS_DIR/f1120_${YEAR}.pdf"

    if head_check "$F1120_URL"; then
        download_form "$F1120_DEST" "$F1120_URL" "Form 1120 (TY $YEAR)"
    else
        echo "  WARN  Form 1120 TY $YEAR not available at $F1120_URL — skipping."
        echo "        Check https://www.irs.gov/forms-instructions for the correct URL."
    fi

    # Form 1125-E
    if [[ "$YEAR" == "2024" ]]; then
        F1125E_URL="https://www.irs.gov/pub/irs-pdf/f1125e.pdf"
    else
        F1125E_URL="https://www.irs.gov/pub/irs-prior/f1125e--${YEAR}.pdf"
    fi
    F1125E_DEST="$FORMS_DIR/f1125e_${YEAR}.pdf"

    if head_check "$F1125E_URL"; then
        download_form "$F1125E_DEST" "$F1125E_URL" "Form 1125-E (TY $YEAR)"
    else
        echo "  WARN  Form 1125-E TY $YEAR not available at $F1125E_URL — skipping."
    fi

    # Form 4562 (Depreciation and Amortization)
    # SAME GOTCHA AS 1120: irs-pdf/f4562.pdf is the 2025 revision, so a
    # YEAR!=2025 request MUST come from irs-prior or it silently saves the
    # wrong revision. This matters more for 4562 than for most forms: the 2025
    # revision INSERTED a 50-year class row into Part III Section B, shifting
    # residential rental from 19h to 19i and nonresidential real from 19i to
    # 19j. Saving the 2025 PDF as f4562_2024.pdf would render every real-property
    # figure one row off. See app/exports/f4562_field_map.py.
    if [[ "$YEAR" == "2025" ]]; then
        F4562_URL="https://www.irs.gov/pub/irs-pdf/f4562.pdf"
    else
        F4562_URL="https://www.irs.gov/pub/irs-prior/f4562--${YEAR}.pdf"
    fi
    F4562_DEST="$FORMS_DIR/f4562_${YEAR}.pdf"

    if head_check "$F4562_URL"; then
        download_form "$F4562_DEST" "$F4562_URL" "Form 4562 (TY $YEAR)"
    else
        echo "  WARN  Form 4562 TY $YEAR not available at $F4562_URL — skipping."
    fi
done

# --- Individual forms (current-year fillable) -----------------------------
# Form W-4P (pension/annuity withholding certificate — professional-substitute
# path) and Form 1040. W-4P is filed on the current-year form regardless of the
# 1099-R's year (it elects future withholding).
echo ""
echo "=== Individual forms (current) ==="

FW4P_URL="https://www.irs.gov/pub/irs-pdf/fw4p.pdf"
FW4P_DEST="$FORMS_DIR/f-w4p.pdf"
if head_check "$FW4P_URL"; then
    download_form "$FW4P_DEST" "$FW4P_URL" "Form W-4P (current)"
else
    echo "  WARN  Form W-4P not available at $FW4P_URL — skipping."
fi

F1040_URL="https://www.irs.gov/pub/irs-pdf/f1040.pdf"
F1040_DEST="$FORMS_DIR/f1040.pdf"
if head_check "$F1040_URL"; then
    download_form "$F1040_DEST" "$F1040_URL" "Form 1040 (current)"
else
    echo "  WARN  Form 1040 not available at $F1040_URL — skipping."
fi

# Note: SC ST-3 is a SC Department of Revenue form, fetched separately into
# data/sc_forms/ST-3.pdf from https://dor.sc.gov/sites/dor/files/forms/ST3.pdf

# Form W-9 (Request for TIN)
FW9_URL="https://www.irs.gov/pub/irs-pdf/fw9.pdf"
FW9_DEST="$FORMS_DIR/fw9.pdf"
if head_check "$FW9_URL"; then
    download_form "$FW9_DEST" "$FW9_URL" "Form W-9 (current)"
else
    echo "  WARN  Form W-9 not available at $FW9_URL — skipping."
fi
# Form 7004 (Business Extension)
F7004_URL="https://www.irs.gov/pub/irs-pdf/f7004.pdf"
F7004_DEST="$FORMS_DIR/f7004.pdf"
if head_check "$F7004_URL"; then
    download_form "$F7004_DEST" "$F7004_URL" "Form 7004 (current)"
else
    echo "  WARN  Form 7004 not available at $F7004_URL — skipping."
fi


echo ""
echo "Done. Forms cached in: $FORMS_DIR"
echo ""
echo "Next step: verify field names match the downloaded PDFs:"
echo "  pdftk data/irs_forms/f1120_2024.pdf dump_data_fields_utf8 | grep FieldName | head -60"
echo ""
echo "Then start the app:"
echo "  docker compose build app && docker compose up -d --force-recreate app"
