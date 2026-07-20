# The professional-substitute stance

Accounting-Ops is built to be an **autonomous professional substitute**, not a
form-filler you babysit. The agents are designed to do the work a bookkeeper or
staff accountant would do — end to end — and to keep going without asking for
permission at every step. Where a traditional tool stops at "here's a suggestion,
you decide," Accounting-Ops is willing to *do the job*: categorize, post,
reconcile, draft the statements, assemble the package.

> **This is a design stance, not a legal claim.** Accounting-Ops is software. It
> is **not** a CPA, an Enrolled Agent, a tax preparer, an attorney, or a
> fiduciary, and using it creates no professional relationship. You are
> responsible for everything you file, sign, or pay. See
> [DISCLAIMER.md](../DISCLAIMER.md).

## Optional autonomy, not mandated human-in-the-loop

The default is that the agent gets real work done. Human review is a **control
you can turn up or down**, not a tax on every action:

- **Deterministic auto-post** for low-risk, high-confidence work (rule-matched,
  entity-mapped, sign-consistent, below the materiality threshold).
- **Queue for confirm** for mid-materiality or model-suggested items.
- **Hard block** for high-materiality items and for substantiation-required
  categories (e.g. travel/meals without a receipt) — these require an explicit
  human confirmation and a memo.

You set where those lines fall. Nothing forces a human into the loop for routine
bookkeeping; the guards exist for the cases that actually warrant a second look.

## The guardrails that make autonomy safe

Autonomy is paired with hard, auditable guards:

- **Append-only ledger with a hash chain.** Postings are immutable; corrections
  are *new* reversing entries, and `verify_chain` proves the history wasn't
  altered.
- **Provenance-or-nothing.** The software refuses to fabricate a figure it can't
  trace to a source — it returns an error rather than inventing a number.
- **Draft / "do not file" watermarks** on every generated tax document.
- **No transmission, no money movement.** In the Community Edition there is no
  e-file (MeF), no state-portal submission, no EFTPS, no e-sign, and no
  trades/transfers/payments on your behalf (see [DISCLAIMER.md](../DISCLAIMER.md)).

## CPA review is optional — and encouraged where it counts

For anything with legal or financial consequence — a tax return, a filing with a
government agency, financial statements a third party will rely on — have a
qualified, licensed professional review it. Accounting-Ops makes that review
*cheaper and faster* by producing clean, sourced, verifiable work — but it does
not replace the professional's judgment or their license. The choice to involve
one is yours.

**Your books. Your machine. Your rules.**

---

# Charter and implementation record

_Added 2026-06-25. Governs the autonomous tax/finance work-product features._
_This section is the engineering charter behind the stance above._
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
| Depreciation engine + Form 4562 | `app/tax/engines/depreciation.py`, `app/tax/engines/configs_depreciation.py`, `app/tax/compute_4562.py`, `app/exports/f4562_field_map.py`, `app/exports/f4562_fill.py`, `FixedAsset` model + migration `0027` | The **second computed form**. Pure MACRS (GDS/ADS, half-year / mid-quarter / mid-month, the §168(d)(3) 40% trigger), §179 with the investment phase-out + taxable-income limit + carryforward, §168(k) bonus, §280F listed property. Year constants are cited to IRS Pub 946 and an unsupported year RAISES. Assets persist in `fixed_asset` because depreciation is multi-year state. |

### Form 4562: two things that will bite a future maintainer

1. **The Part III class rows MOVED in the 2025 revision.** A new "50-year
   property" row at 19h pushed residential rental from 19h→19i and
   nonresidential real from 19i→19j (Section C gained a 50-year row at 20e, and
   line 23 split into 23a/23b). Reusing the 2024 map on the 2025 form files
   residential rental depreciation in the 50-year row — a real number on the
   wrong line, silently. The engine therefore keys Part III by **recovery
   period**, `f4562_field_map` owns the per-revision row mapping, and an unmapped
   year raises. `tests/unit/test_form_4562_render.py` asserts the shift on the
   RENDERED PDF, not just the map.
2. **§168(k) depends on the ACQUISITION date from TY2025**, not only the
   placed-in-service date: OBBBA (P.L. 119-21) reinstated 100% for property
   acquired after 2025-01-19, while earlier-acquired property placed in service
   in 2025 stays at 40%. An asset with no `acquired_on` cannot be resolved and
   the engine raises rather than picking one.
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

1. A deterministic worksheet/engine in `app/tax/` (year-aware, raises on unsupported year,
   surfaces assumptions) if the form needs computed lines. Year constants belong in a separate
   `configs_*.py` with a `SUPPORTED_TAX_YEARS` tuple and a citation for every figure.
2. A field map + a `render_<kind>(tax_year, watermark_mode, inputs)` renderer in `app/exports/`,
   reusing `_render_acroform_pdf` (AcroForm) or `_apply_watermark` (generated letters).
   **Verify every AcroForm name against the actual PDF** — a name that does not exist fills
   nothing and fails silently. See `tests/unit/test_form_4562_render.py` for the pattern.
3. One `FormSpec` entry in `app/exports/form_registry.py`. (No dispatch branch is needed any
   more — `app/api/work_products.py::_render_work_product` dispatches through the registry.)
4. A creator tool in `app/tools/` (reads sources for provenance, writes a DRAFT `WorkProduct`),
   wired into `app/tools/__init__.py`, `mcp_server.py`, the persona allow-lists, and
   `app/api/discovery.py::_TOOL_METADATA`.

Every line you render must carry a `source_ref`, or the work-products serve route will refuse it.
