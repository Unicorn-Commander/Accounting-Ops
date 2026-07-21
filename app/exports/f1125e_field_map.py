"""IRS Form 1125-E (Compensation of Officers) — AcroForm field map.

Field names were verified 2026-07-21 by dumping the official fillable PDF
(``data/irs_forms/f1125e_2024.pdf``, the current irs.gov ``f1125e.pdf``
revision — the 1125-E is revision-dated Rev. October 2016, not annual) with
``pypdf`` (``PdfReader().get_fields()``) and pinning every terminal widget's
/Rect against the printed column captions (positional audit at 150 dpi).

CRITICAL nesting note
---------------------
The officer-table widgets are NOT direct children of ``Page1[0]``. Their
fully-qualified names run through TWO intermediate non-terminal nodes::

    topmostSubform[0].Page1[0].Line1Table[0].BodyRow{N}[0].f1_{X}[0]

(An earlier draft of this map omitted ``Line1Table[0].BodyRow{N}[0]`` — with
the exact-match /Fields-tree walker in ``form_prefill._render_acroform_pdf``
every officer cell then silently failed to fill.) Only the header (``f1_1``,
``f1_2``) and the three total lines (``f1_123``–``f1_125``) sit directly under
``Page1[0]``.

Layout (single page, all /Tx — the 1125-E has no checkboxes)
-----------------------------------------------------------
Header: corporation name (``f1_1``), EIN (``f1_2``). Line 1 table: 20 body
rows x 6 columns, numbered in strict row-major document order —
row N uses ``f1_{3+6(N-1)}`` .. ``f1_{8+6(N-1)}``:

    (a) name       x≈36..230
    (b) SSN        x≈230..331
    (c) % time     x≈331..378
    (d) % common   x≈389..428
    (e) % preferred x≈439..478
    (f) compensation x≈490..576

Totals: line 2 ``f1_123`` (y≈132), line 3 ``f1_124`` (y≈108), line 4
``f1_125`` (y≈72), all in the right-hand amount column.

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical key
-> the fully-qualified AcroForm field name. Dollar amounts are written as plain
strings ("12500" — no $ or commas). Conservative blank-fill: a line with no
input value is simply not written.

CPA/EA verification note
------------------------
These names match the f1125e_2024.pdf as distributed on irs.gov (fetched by
``scripts/download_irs_forms.sh``, TY2024 -> current ``irs-pdf/f1125e.pdf``).
If a later (or prior-year ``f1125e_{year}.pdf``) PDF is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"

# Officer-table column order — matches the printed captions (a)..(f) and the
# widget x-bands pinned above. Position i in a row maps to f1_{base+i}.
_OFFICER_COLS: tuple[str, ...] = (
    "name",       # (a) Name of officer
    "ssn",        # (b) Social security number
    "pct_time",   # (c) Percent of time devoted to business
    "common",     # (d) Percent of stock owned — common
    "preferred",  # (e) Percent of stock owned — preferred
    "comp",       # (f) Amount of compensation
)


def _officer_row_fields(row: int) -> dict[str, str]:
    """Fully-qualified names for officer row ``row`` (1-based, 1..20).

    Row N lives under ``Line1Table[0].BodyRow{N}[0]`` and uses the six
    consecutive terminals ``f1_{3+6(N-1)}`` .. ``f1_{8+6(N-1)}`` — verified
    against the pypdf dump (BodyRow1 = f1_3..f1_8, ... BodyRow20 =
    f1_117..f1_122).
    """
    base = 3 + 6 * (row - 1)
    prefix = f"{_P1}.Line1Table[0].BodyRow{row}[0]"
    return {
        f"officer_{row}_{col}": f"{prefix}.f1_{base + i}[0]"
        for i, col in enumerate(_OFFICER_COLS)
    }


# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F1125E_FIELDS: dict[str, str] = {
    # --- Header (direct children of Page1) ---
    "corporation_name": f"{_P1}.f1_1[0]",  # Corporation name
    "ein":              f"{_P1}.f1_2[0]",  # Employer identification number
}

# --- Line 1 officer table: 20 rows x 6 columns, nested per BodyRow ---
for _row in range(1, 21):
    F1125E_FIELDS.update(_officer_row_fields(_row))
del _row

F1125E_FIELDS.update({
    # --- Total lines (direct children of Page1) ---
    "line_2_total":   f"{_P1}.f1_123[0]",  # Line 2 — Total compensation of officers
    "line_3_claimed": f"{_P1}.f1_124[0]",  # Line 3 — Compensation claimed on 1125-A / elsewhere
    "line_4_net":     f"{_P1}.f1_125[0]",  # Line 4 — Line 2 minus line 3 (caller-supplied, never derived here)
})

# Identity items the renderer never auto-fills, even if supplied — the taxpayer
# enters these themselves.
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset(
    f"officer_{row}_ssn" for row in range(1, 21)
)

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 1125-E fields.
VALID_F1125E_FIELDS: frozenset[str] = frozenset(F1125E_FIELDS.values())
