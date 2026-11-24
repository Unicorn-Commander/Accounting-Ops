# REPORT: PR #108 send-back round 9 (FINISHER-104, 11-24)

Branch agent/corporal-qwen/f5fb8b1c (PR #108). Head 7b3b076.
Round 9 adds the single missing commit AO108-R8-P2-1. R8-P1-1 (7bdf81ea),
R8-P2-2 (b8c1b87b) and R8-P3 (8caeb4c3) already landed in round 8; this round
does only the P2-1 pre-check fix and its tests.

| Item | Commit | Change | Tests (locked fastapi 0.136.3 / starlette 1.3.1, pymupdf 1.28.2, pytest 9.0.3) |
|------|--------|--------|---------------------------------------------------------------|
| R8-P1-1 | 7bdf81e | tax_doc_vlm.py:147-149: pass `max_pages=` and `dpi=` as keywords to `_render_pdf` (keyword-only signature). Added `test_extract_passes_keywords_to_render_pdf` driving `TaxDocExtractor().extract()` end-to-end with a real 1-page PDF + stubbed HTTP, asserting no TypeError and the captured kwargs match the module constants. | (round 8) test_tax_doc_pdf_cap.py |
| R8-P2-1 | 7b3b076 (this round) | `_page_content_bytes` (tax_doc_vlm.py ~:430) summed `doc.xref_stream_raw` (COMPRESSED bytes): a Flate-compressed page of many rects is small on disk yet huge decoded, so it passed the 128 KB bound. Form XObjects invoked with '/X Do' were not counted. FIX: measure DECODED bytes via `doc.xref_stream`, stop summing once the total passes the bound, and add every Form XObject the page references (`page.get_xobjects()`) plus the one nesting level an XObject itself references (its /Resources /XObject dict, new helper `_xobject_xrefs`). Corrected the bound comment and the `_render_pdf` docstring to say decoded. New tests: `test_compressed_heavy_page_refused_by_precheck` (8000-rect page, deflate=True, ~333 KB decoded vs ~28 KB raw, refused fast), `test_xobject_heavy_page_refused_by_precheck` (tiny page content stream invoking a ~341 KB decoded XObject, ~27 KB compressed, refused), `test_nested_xobject_bytes_counted_by_precheck` (page -> tiny wrapper -> ~200 KB decoded XObject; only the nested level pushes it over the bound), and the existing `test_light_page_passes_content_precheck` (a normal one-page PDF still passes). | test_tax_doc_pdf_cap.py 12 passed (52s); test_failed_paths_visible.py 2 passed (1s) |
| R8-P2-2 | b8c1b87 | tax_doc_vlm.py `_render_pdf`: replaced thread-based rasterization (GIL held inside the fitz C rasterizer, so the join deadline was best-effort) with a `ProcessPoolExecutor` sized to `_RENDER_CONCURRENCY=2`; each page rasterizes in a worker process via top-level `_rasterize_page_in_process`, per-page timeout enforced at the OS level. Added `rasterize_fn` test-injection param. | (round 8) test_tax_doc_pdf_cap.py |
| R8-P3 | 8caeb4c3 | security_headers.py: fixed the preview frame-ancestors comment (sends 'none') and `INVOICE_HTML_CSP` form-action -> 'none' (the template has no form; the Stripe pay button is a link). body_size_cap.py: removed dead `state['aborted']`. | (round 8) test_security_headers.py, test_body_size_cap.py |
| R9-P2-1 | (none) | No item: round 9 was "ONE ITEM" = AO108-R8-P2-1 only. This is that item (committed as 7b3b076). | see R8-P2-1 |

Verification (venv: `uv venv -p 3.12` + `uv sync --frozen --active --extra dev`):
- `pytest tests/smoke/test_tax_doc_pdf_cap.py` -> 12 passed in 52s
- `pytest tests/smoke/test_failed_paths_visible.py` -> 2 passed in 1s
- `ruff check app/categorization/tax_doc_vlm.py tests/smoke/test_tax_doc_pdf_cap.py` -> clean

The three new pre-check tests were confirmed to FAIL on the pre-fix code
(`xref_stream_raw`, no XObjects) and PASS after the fix: the compressed-size
measure let the XObject-heavy and nested pages through, the decoded measure
refuses them.

Left / for Aaron: nothing in this round. The full suite runs on CI. The
pre-check is in-process and cheap (a few decoded stream reads); the process
pool is only reached by a page that passes the pre-check, so the common
1-2 page tax doc pays no pool overhead.
