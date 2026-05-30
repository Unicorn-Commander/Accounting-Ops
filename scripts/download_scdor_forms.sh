#!/usr/bin/env bash
# download_scdor_forms.sh — fetch official SCDOR PDFs for pre-fill.
#
# Downloads to data/scdor_forms/. The forms are gitignored (too large for git;
# SCDOR distribution is unrestricted for taxpayer use).
#
# Usage:
#   bash scripts/download_scdor_forms.sh
#
# The script is idempotent — if a file already exists it prints a checksum
# and re-downloads to refresh (SCDOR republishes forms mid-cycle when they fix
# rev numbers; better to always refresh than miss an update).
#
# SCDOR uses a single canonical URL per form (no /irs-prior/ style year-stamped
# variants); the form revision date is printed inside the PDF (e.g. "Rev. 7/2/25").
# Always re-run this script before generating a packet for filing.
#
# Stable URLs (verified 2026-05-30):
#   SC1120  → https://dor.sc.gov/sites/dor/files/forms/SC1120.pdf
#   SC1120V → https://dor.sc.gov/sites/dor/files/forms/SC1120V.pdf
#
# Requirements: curl (or wget), sha256sum (or shasum on macOS).
set -euo pipefail

FORMS_DIR="$(cd "$(dirname "$0")/.." && pwd)/data/scdor_forms"
mkdir -p "$FORMS_DIR"

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

    echo "  FETCH $label"
    echo "        $url"
    echo "        -> $dest"
    $DOWNLOAD_CMD "$dest" "$url"
    $CKSUM "$dest"
    echo "  OK    $label downloaded."
}

# SC1120
download_form \
    "$FORMS_DIR/SC1120.pdf" \
    "https://dor.sc.gov/sites/dor/files/forms/SC1120.pdf" \
    "SC1120 (current year)"

# SC1120V
download_form \
    "$FORMS_DIR/SC1120V.pdf" \
    "https://dor.sc.gov/sites/dor/files/forms/SC1120V.pdf" \
    "SC1120-V (current year)"

echo ""
echo "Done. SCDOR forms cached in: $FORMS_DIR"
echo ""
echo "NOTE: SCDOR PDFs are STATIC (not AcroForms). The pre-fill engine"
echo "      (app/exports/sc_form_prefill.py) uses coordinate-based overlay."
echo "      If SCDOR releases a new revision with shifted layout, re-run"
echo "      scripts/probe_sc1120_layout.py to refresh coordinates."
