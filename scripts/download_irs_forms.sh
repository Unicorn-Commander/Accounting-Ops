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
#   bash scripts/download_irs_forms.sh --refresh f9465
#
# The script is idempotent — if a file already exists it prints a checksum
# and skips the download. ``--refresh <form>`` (the filename stem, e.g.
# ``f9465``) re-downloads that one file even when it is already present and
# prints the old and new sha256. Every other file keeps the skip behaviour.
# A year-specific file is named in full (``--refresh f1120_2024``).
#
# IRS form URLs (stable; the IRS keeps old-year forms in the same path):
#   https://www.irs.gov/pub/irs-pdf/f1120.pdf       — current year Form 1120
#   https://www.irs.gov/pub/irs-prior/f1120--{year}.pdf  — prior years
#   https://www.irs.gov/pub/irs-pdf/f1125e.pdf      — current year 1125-E
#   https://www.irs.gov/pub/irs-prior/f1125e--{year}.pdf — prior years
#
# Requirements: curl (or wget), sha256sum (or shasum on macOS).
set -euo pipefail

# Tests point this at a temp directory. Unset, the path is the repo's
# data/irs_forms, same as before the flag existed.
if [[ -n "${IRS_FORMS_DOWNLOAD_DIR:-}" ]]; then
    FORMS_DIR="${IRS_FORMS_DOWNLOAD_DIR}"
else
    FORMS_DIR="$(cd "$(dirname "$0")/.." && pwd)/data/irs_forms"
fi
mkdir -p "$FORMS_DIR"

REFRESH_FORM=""
REFRESH_HITS=0
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --refresh)
            if [[ $# -lt 2 || -z "${2:-}" || "${2}" == --* ]]; then
                echo "ERROR: --refresh requires a form name (for example f9465)." >&2
                exit 2
            fi
            REFRESH_FORM="$2"
            shift 2
            ;;
        --*)
            echo "ERROR: unknown option $1 (expected --refresh <form>)." >&2
            exit 2
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done
if [[ ${#POSITIONAL[@]} -eq 0 ]]; then
    YEARS=(2024)
else
    YEARS=("${POSITIONAL[@]}")
fi

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

# True when ``dest`` is the form named by ``--refresh``.
_refresh_this() {
    [[ -n "${REFRESH_FORM}" ]] || return 1
    local base stem want
    base="$(basename "$1")"
    stem="${base%.pdf}"
    want="${REFRESH_FORM%.pdf}"
    [[ "$want" == "$stem" || "$want" == "$base" ]]
}

_sha256_of() {
    $CKSUM "$1" | awk 'NR==1 { print $1 }'
}

download_form() {
    local dest="$1"
    local url="$2"
    local label="$3"

    if _refresh_this "$dest"; then
        REFRESH_HITS=$((REFRESH_HITS + 1))
    fi

    if [[ -f "$dest" ]] && ! _refresh_this "$dest"; then
        echo "  SKIP  $label — already present ($dest)"
        $CKSUM "$dest"
        return 0
    fi

    # Re-download into a temp file so a failed fetch leaves the old PDF in place.
    if [[ -f "$dest" ]]; then
        local old_sha tmp new_sha
        old_sha="$(_sha256_of "$dest")"
        echo "  REFRESH $label — re-downloading even though it is present"
        echo "        $url"
        echo "        -> $dest"
        echo "  OLD sha256 ${old_sha}"
        tmp="$(mktemp "${dest}.XXXXXX")"
        if ! $DOWNLOAD_CMD "$tmp" "$url"; then
            rm -f "$tmp"
            exit 1
        fi
        mv "$tmp" "$dest"
        new_sha="$(_sha256_of "$dest")"
        echo "  NEW sha256 ${new_sha}"
        echo "  OK    $label downloaded."
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

    # Form 1096 (Annual Summary and Transmittal of U.S. Information Returns).
    # YEAR-SPECIFIC like the 1125-E: the current irs-pdf copy is the CURRENT
    # year's transmittal; prior years live in irs-prior. (The official 1096 is
    # a red-ink scannable form -- our fill is a SELF_CERTIFIED-watermarked
    # review copy, never represented as the scannable original.)
    if [[ "$YEAR" == "2025" ]]; then
        F1096_URL="https://www.irs.gov/pub/irs-pdf/f1096.pdf"
    else
        F1096_URL="https://www.irs.gov/pub/irs-prior/f1096--${YEAR}.pdf"
    fi
    F1096_DEST="$FORMS_DIR/f1096_${YEAR}.pdf"
    if head_check "$F1096_URL"; then
        download_form "$F1096_DEST" "$F1096_URL" "Form 1096 (TY $YEAR)"
    else
        echo "  WARN  Form 1096 TY $YEAR not available at $F1096_URL — skipping."
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
# Form 8821 (Tax Information Authorization)
F8821_URL="https://www.irs.gov/pub/irs-pdf/f8821.pdf"
F8821_DEST="$FORMS_DIR/f8821.pdf"
if head_check "$F8821_URL"; then
    download_form "$F8821_DEST" "$F8821_URL" "Form 8821 (current)"
else
    echo "  WARN  Form 8821 not available at $F8821_URL — skipping."
fi
# Form 2848 (Power of Attorney)
F2848_URL="https://www.irs.gov/pub/irs-pdf/f2848.pdf"
F2848_DEST="$FORMS_DIR/f2848.pdf"
if head_check "$F2848_URL"; then
    download_form "$F2848_DEST" "$F2848_URL" "Form 2848 (current)"
else
    echo "  WARN  Form 2848 not available at $F2848_URL — skipping."
fi

# Form 843 (Claim for Refund and Request for Abatement) — current revision.
F843_URL="https://www.irs.gov/pub/irs-pdf/f843.pdf"
F843_DEST="$FORMS_DIR/f843.pdf"
if head_check "$F843_URL"; then
    download_form "$F843_DEST" "$F843_URL" "Form 843 (current)"
else
    echo "  WARN  Form 843 not available at $F843_URL — skipping."
fi

# Form 9465 (Payment Plan Request) — current revision.
# Pinned 2026-11-05 in app/exports/f9465_field_map.py against the PDF this URL
# returned that morning (170,094 bytes):
#   printed footer "Form 9465 (Rev. 12-2026)" (header "Rev. December 2026"),
#   1 page, 40 AcroForm entries (PdfReader.get_fields, parents included),
#   sha256 2c1253f4902d2ae20a3041d04b64e206f618cbd9d4500ae0aa3d859fc063191a,
#   OMB No. 1545-0074, Cat. No. 14842Y.
# The Rev. September 2020 file (2 pages, unpadded f1_1.. names) is no longer
# what this URL serves. A revision change must fail
# tests/smoke/test_f9465_fill_unit.py::TestSourcePdfGroundTruth
# before any field assertion:
#   "Form 9465 revision changed: map verified against <rev>, PDF is <rev>"
F9465_URL="https://www.irs.gov/pub/irs-pdf/f9465.pdf"
F9465_DEST="$FORMS_DIR/f9465.pdf"
if head_check "$F9465_URL"; then
    download_form "$F9465_DEST" "$F9465_URL" "Form 9465 (current)"
else
    echo "  WARN  Form 9465 not available at $F9465_URL — skipping."
fi




if [[ -n "${REFRESH_FORM}" && "${REFRESH_HITS}" -eq 0 ]]; then
    echo "ERROR: --refresh ${REFRESH_FORM} did not match a form this script downloads." >&2
    exit 1
fi

echo ""
echo "Done. Forms cached in: $FORMS_DIR"
echo ""
echo "Next step: verify field names match the downloaded PDFs:"
echo "  pdftk data/irs_forms/f1120_2024.pdf dump_data_fields_utf8 | grep FieldName | head -60"
echo ""
echo "Then start the app:"
echo "  docker compose build app && docker compose up -d --force-recreate app"
