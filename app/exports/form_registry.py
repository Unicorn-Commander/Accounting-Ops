"""Form registry — the single source of truth for every work-product form.

Adding a new form to the platform is now DATA, not bespoke dispatch code: drop a
``render_<kind>(tax_year, watermark_mode, inputs) -> (bytes, content_type)``
function in a module under ``app/exports/`` and register one ``FormSpec`` here.
``work_products._render_work_product`` dispatches through ``render_form``.

Renderer contract (every entry honors it):
  * Signature: ``render(tax_year: int | None, watermark_mode: WatermarkMode,
    inputs: dict) -> tuple[bytes, str]``.
  * ``inputs`` is the WorkProduct.inputs map: ``{field_key: {"value": str,
    "source_ref": str, "origin": str}}``. Provenance is validated by the caller
    BEFORE render; renderers fill only present, non-blank values (conservative
    blank-fill) and NEVER fabricate.
  * The watermark reflects the work-product status (DRAFT / self-certified /
    clean) — renderers reuse ``form_prefill._render_acroform_pdf`` (AcroForm,
    flattened + visible) or ``_apply_watermark`` (generated letters).

``mode`` documents how a form's numbers are produced:
  * ``computed``  — line amounts come from a deterministic engine (e.g. 1040 via
    the vendored tax engines). The work product can be prepared end-to-end.
  * ``fill``      — the platform fills figures the client/their documents supply
    (transcribe-not-compute); the human provides the numbers.
  * ``letter``    — a generated document (e.g. a CP504 response), not a form.

Lazy import by (module, func) keeps app boot light and avoids circular imports;
a registered form whose module is absent simply raises a clear error at render.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass

from app.exports.form_prefill import WatermarkMode


@dataclass(frozen=True)
class FormSpec:
    kind: str               # stable id, e.g. "1040", "sch-c", "st-3", "cp504"
    title: str              # human title
    jurisdiction: str       # "federal" | state code ("SC", "CA", …)
    mode: str               # "computed" | "fill" | "letter"
    module: str             # "app.exports.f1040_fill"
    func: str               # "render_1040"
    goes_to: str = ""       # plain-language "given to …" (the IRS, your payer, …)
    category: str = "individual"  # individual | business | payroll | state | notice


# The registry. Breadth additions land here as one entry + a renderer module.
FORM_REGISTRY: dict[str, FormSpec] = {
    "w-4p": FormSpec(
        kind="w-4p", title="Form W-4P — Pension/Annuity Withholding",
        jurisdiction="federal", mode="computed",
        module="app.exports.form_prefill", func="render_w4p",
        goes_to="your pension or annuity payer (NOT the IRS)",
        category="individual",
    ),
    "1040": FormSpec(
        kind="1040", title="Form 1040 — U.S. Individual Income Tax Return",
        jurisdiction="federal", mode="computed",  # tax_compute_1040 produces computed lines
        module="app.exports.f1040_fill", func="render_1040",
        goes_to="the IRS", category="individual",
    ),
    "st-3": FormSpec(
        kind="st-3", title="SC ST-3 — State Sales & Use Tax Return",
        jurisdiction="SC", mode="fill",
        module="app.exports.st3_fill", func="render_st3",
        goes_to="the SC Department of Revenue", category="state",
    ),
    "cp504": FormSpec(
        kind="cp504", title="CP504 Notice Response Letter",
        jurisdiction="federal", mode="letter",
        module="app.exports.cp504_letter", func="render_cp504",
        goes_to="the IRS (mail to the address on your notice)",
        category="notice",
    ),
    # --- Breadth: top-frequency CPA forms (added 2026-06-25) -----------------
    # 1040 schedules (attach to the individual return).
    "sch-a": FormSpec(kind="sch-a", title="Schedule A — Itemized Deductions",
        jurisdiction="federal", mode="fill", module="app.exports.sch_a_fill",
        func="render_sch_a", goes_to="attached to your Form 1040 (the IRS)", category="individual"),
    "sch-b": FormSpec(kind="sch-b", title="Schedule B — Interest and Ordinary Dividends",
        jurisdiction="federal", mode="fill", module="app.exports.sch_b_fill",
        func="render_sch_b", goes_to="attached to your Form 1040 (the IRS)", category="individual"),
    "sch-c": FormSpec(kind="sch-c", title="Schedule C — Profit or Loss From Business",
        jurisdiction="federal", mode="fill", module="app.exports.sch_c_fill",
        func="render_sch_c", goes_to="attached to your Form 1040 (the IRS)", category="individual"),
    "sch-d": FormSpec(kind="sch-d", title="Schedule D — Capital Gains and Losses",
        jurisdiction="federal", mode="fill", module="app.exports.sch_d_fill",
        func="render_sch_d", goes_to="attached to your Form 1040 (the IRS)", category="individual"),
    "sch-e": FormSpec(kind="sch-e", title="Schedule E — Supplemental Income and Loss",
        jurisdiction="federal", mode="fill", module="app.exports.sch_e_fill",
        func="render_sch_e", goes_to="attached to your Form 1040 (the IRS)", category="individual"),
    "sch-se": FormSpec(kind="sch-se", title="Schedule SE — Self-Employment Tax",
        jurisdiction="federal", mode="fill", module="app.exports.sch_se_fill",
        func="render_sch_se", goes_to="attached to your Form 1040 (the IRS)", category="individual"),
    # Entity returns.
    "1065": FormSpec(kind="1065", title="Form 1065 — U.S. Return of Partnership Income",
        jurisdiction="federal", mode="fill", module="app.exports.f1065_fill",
        func="render_1065", goes_to="the IRS", category="business"),
    "1120-s": FormSpec(kind="1120-s", title="Form 1120-S — S Corporation Income Tax Return",
        jurisdiction="federal", mode="fill", module="app.exports.f1120s_fill",
        func="render_1120_s", goes_to="the IRS", category="business"),
    "k1-1065": FormSpec(kind="k1-1065", title="Schedule K-1 (Form 1065) — Partner's Share",
        jurisdiction="federal", mode="fill", module="app.exports.k1_1065_fill",
        func="render_k1_1065", goes_to="the partner (and attached to the 1065)", category="business"),
    # Payroll + extension.
    "941": FormSpec(kind="941", title="Form 941 — Employer's Quarterly Federal Tax Return",
        jurisdiction="federal", mode="fill", module="app.exports.f941_fill",
        func="render_941", goes_to="the IRS", category="payroll"),
    "4868": FormSpec(kind="4868", title="Form 4868 — Automatic Extension to File (individual)",
        jurisdiction="federal", mode="fill", module="app.exports.f4868_fill",
        func="render_4868", goes_to="the IRS", category="individual"),
    "1125-e": FormSpec(kind="1125-e", title="Form 1125-E — Compensation of Officers",
        jurisdiction="federal", mode="fill", module="app.exports.f1125e_fill",
        func="render_1125e", goes_to="the IRS (attachment to Form 1120/1120-S)", category="business"),
    # State.
    "ca-540": FormSpec(kind="ca-540", title="California Form 540 — Resident Income Tax Return",
        jurisdiction="CA", mode="fill", module="app.exports.ca_540_fill",
        func="render_ca_540", goes_to="the California Franchise Tax Board", category="state"),
    # --- Business entity + state corporate + notices (added 2026-06-26) -------
    "1120": FormSpec(kind="1120", title="Form 1120 — U.S. Corporation Income Tax Return",
        jurisdiction="federal", mode="fill", module="app.exports.form_1120_fill",
        func="render_1120", goes_to="the IRS", category="business"),
    "sc1120": FormSpec(kind="sc1120", title="SC1120 — South Carolina C Corporation Income Tax Return",
        jurisdiction="SC", mode="fill", module="app.exports.sc_form_prefill",
        func="render_sc1120", goes_to="the SC Department of Revenue", category="state"),
    "scdor-notice": FormSpec(kind="scdor-notice", title="SC DOR Notice Response Letter",
        jurisdiction="SC", mode="letter", module="app.exports.scdor_letter",
        func="render_scdor_response", goes_to="the SC Department of Revenue", category="notice"),
}


def register(spec: FormSpec) -> None:
    """Register (or override) a form spec. Intended for module-import-time use."""
    FORM_REGISTRY[spec.kind] = spec


def get_spec(kind: str) -> FormSpec | None:
    return FORM_REGISTRY.get((kind or "").strip().lower())


def list_forms() -> list[FormSpec]:
    return sorted(FORM_REGISTRY.values(), key=lambda s: (s.category, s.kind))


def render_form(
    kind: str,
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Dispatch to a registered form's renderer. Raises ValueError on unknown kind."""
    spec = get_spec(kind)
    if spec is None:
        raise ValueError(
            f"No form registered for kind {kind!r}. "
            f"Known: {sorted(FORM_REGISTRY)}."
        )
    module = importlib.import_module(spec.module)
    renderer = getattr(module, spec.func)
    return renderer(tax_year, watermark_mode, inputs)
