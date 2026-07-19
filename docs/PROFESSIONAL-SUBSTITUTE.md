# Professional-Substitute Layer

_Added 2026-06-25. Governs the autonomous tax/finance work-product features._

## Posture (charter)

Accounting-Ops (with Tax-Planning-Ops) acts as an **autonomous professional-substitute**:
it does the work of a CPA / EA / tax attorney **when one isn't available or affordable**,
and takes the user **all the way** to a finished, ready-to-sign work product — not advice,
not a to-do list. Three principles govern it:

1. **CPA/professional review is an OPTIONAL gate, not a hard wall.** A workspace with no
   available professional can self-certify a work product to completion. The review path is
   preserved for workspaces that *do* have a pro. This is controlled per-workspace by
   `EngagementPolicy.review_mode` (`pro_required` is the safe default; `self_attest_allowed`
   opens self-certification; `autonomous_off` disables agent preparation).
2. **Never fabricate a figure.** Every non-blank line on a work product must trace to a
   `source_ref` (a source document extraction, a deterministic worksheet, or an explicit user
   entry). The render path *refuses* (HTTP 422) to emit a figure without provenance. Missing
   data is left blank (conservative blank-fill), never guessed. No LLM computes a number that
   lands on a form.
3. **Transmission is dormant and default-OFF.** The platform CAN be wired to e-file / submit to
   a state portal / EFTPS / e-sign, but every transmission channel ships disabled; only paper
   printing is enabled. Enabling a channel requires three independent gates plus a per-org legal
   decision (see the submission rail below). The default behavior is unchanged: produce the
   document, the human prints/signs/mails it.

## What was built

| Piece | Where | Notes |
|---|---|---|
| `EngagementPolicy` (per-workspace) | `app/models/ledger.py`, migration `0023`, `app/engagement/policy.py`, `app/api/engagement.py` | The "is a professional available?" switch + local-first + rail flags. Lazily created, safe default `pro_required`. |
| `WorkProduct` (per-entity) | `app/models/ledger.py`, migration `0023` | A finished artifact (filled form / letter). Re-rendered **on demand** from `inputs` ({value, source_ref, origin}); `status` drives the watermark. No stale snapshot. |
| Conditional watermark | `app/exports/form_prefill.py` (`WatermarkMode`) | `draft` → DRAFT/"do not file"; `self_certified` → softer provenance banner; `none` → clean. The 1120 path is unchanged (defaults to `draft`). |
| Robust AcroForm fill | `app/exports/form_prefill.py` (`_render_acroform_pdf`, `_set_acroform_values`) | XFA-hybrid-safe (clone + drop XFA + direct field-tree set). Required for modern IRS forms (W-4P, 1040). |
| Form W-4P (reference path) | `app/tax/w4p_worksheet.py`, `app/exports/w4p_field_map.py`, `app/exports/form_prefill.py` (`generate_w4p_pdf`) | The proof. Deterministic Step 3 + Step 4(b) worksheet (year-aware, raises on unsupported year). Goes to the **payer**, not the IRS — zero e-file/UPL exposure. |
| `tax_fill_w4p` agent tool | `app/tools/tax_w4p.py` (+ persona allow-lists, `mcp_server.py`) | Reads the 1099-R (identity provenance), runs the worksheet, writes a DRAFT WorkProduct, returns a download reference. |
| Work-products API | `app/api/work_products.py` | List/detail, `GET …/document.pdf` (re-render + watermark-by-status + provenance guard + audit), `POST …/attest` (the bypassable self-attest gate). |
| Last-mile UI | `web/app/taxes/w-4p/` | Preview + self-certify card (gated on policy) + download + plain-language "give to your payer, not the IRS" guidance. |
| Dormant submission rail | `app/submission/rail.py`, `app/api/submission.py` | `SubmissionRail` protocol + adapters. Only `PAPER_PRINT` enabled; e-file/state/EFTPS/e-sign present but raise `FeatureDisabled` (3 gates + kill-switch; even gated, `execute` is a deliberate no-op-raise today). |
| Local-first chat routing | `agents/chat/inference_config.py`, `app/config.py` | New tier after BYOK: sovereign local qwen (`AO_CHAT_LOCAL_*`) when `EngagementPolicy.prefer_local_inference` is on; cloud is the fallback. Default-safe (off ⇒ unchanged). |

## The bypassable gate, concretely

A work product starts as `draft` (DRAFT watermark). To leave draft:
- **If a pro is available** (`review_mode = pro_required`, the default): it follows the existing
  professional-review path → `pro_reviewed`. Self-attest is refused (HTTP 403).
- **If no pro is available** (`review_mode = self_attest_allowed`): the user self-certifies via
  `POST …/work-products/{id}/attest` → `self_approved`, `attestation_kind = self`, re-rendered
  with the softer "prepared via self-certification" banner instead of "do not file".

This makes the CPA wall optional **without** weakening it for those who want it, and **without**
touching the ledger's immutability guarantees (the journal hard-wall in migration `0005` is
unrelated and untouched).

## Adding another form

1. A deterministic worksheet in `app/tax/` (year-aware, raises on unsupported year, surfaces
   assumptions) if the form needs computed lines.
2. A field map + a `render_<kind>(tax_year, watermark_mode, inputs)` renderer in `app/exports/`,
   reusing `_render_acroform_pdf` (AcroForm) or `_apply_watermark` (generated letters).
3. A dispatch branch in `app/api/work_products.py::_render_work_product`.
4. A creator tool in `app/tools/` (reads sources for provenance, writes a DRAFT `WorkProduct`),
   wired into `app/tools/__init__.py`, `mcp_server.py`, the persona allow-lists, and
   `app/api/discovery.py::_TOOL_METADATA`.

Every line you render must carry a `source_ref`, or the work-products serve route will refuse it.
