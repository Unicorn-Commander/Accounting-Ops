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
    if [[ "$YEAR" == "2024" ]]; then
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

# Form 8821 (Tax Information Authorization)
F8821_URL="https://www.irs.gov/pub/irs-pdf/f8821.pdf"
F8821_DEST="$FORMS_DIR/f8821.pdf"
if head_check "$F8821_URL"; then
    download_form "$F8821_DEST" "$F8821_URL" "Form 8821 (current)"
else
    echo "  WARN  Form 8821 not available at $F8821_URL — skipping."
fi

echo ""
echo "Done. Forms cached in: $FORMS_DIR"
echo ""
echo "Next step: verify field names match the downloaded PDFs:"
echo "  pdftk data/irs_forms/f1120_2024.pdf dump_data_fields_utf8 | grep FieldName | head -60"
echo ""
echo "Then start the app:"
echo "  docker compose build app && docker compose up -d --force-recreate app"
