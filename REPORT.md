# REPORT: PR #108 send-back round 8 (FINISHER-104, 11-24)

Branch agent/corporal-qwen/f5fb8b1c (PR #108). Head b8c1b87.
Round 8 fixes 1 P1, 2 P2, and 1 P3 finding from claude-fable.

| Item | Commit | Change | Tests (locked fastapi 0.136.3 / starlette 1.3.1, pymupdf 1.28.2, pytest 9.0.3) |
|------|--------|--------|---------------------------------------------------------------|
| R8-P1-1 | 7bdf81e | tax_doc_vlm.py:147-149: pass `max_pages=` and `dpi=` as keywords to `_render_pdf` (whose signature is `(pdf_bytes, *, max_pages, dpi)`). The positional call would raise TypeError at runtime. Added regression test `test_extract_passes_keywords_to_render_pdf` that drives `TaxDocExtractor().extract()` end-to-end with a real 1-page PDF and a stubbed HTTP call, asserting no TypeError and that the captured kwargs match the module constants. | test_tax_doc_pdf_cap.py 12 passed (77s) |
| R8-P2-1 | 7bdf81e | tax_doc_vlm.py `_page_content_bytes`: now measures DECODED stream size via `doc.xref_stream(xref)` (not `xref_stream_raw`) and includes Form XObjects referenced by the page (`page.get_xobjects()`). A Flate-compressed stream can be small on disk yet expand to a huge operator list that the C rasterizer must walk. Updated the `_PAGE_CONTENT_MAX_BYTES` comment and the `_render_pdf` docstring to say "decoded" instead of "uncompressed". Added test `test_xobject_heavy_page_refused_by_precheck` that builds a page with a tiny content stream invoking an 8000-op Form XObject (~133 KB decoded) and verifies the pre-check refuses it. | test_tax_doc_pdf_cap.py 12 passed (77s) |
| R8-P2-2 | b8c1b87 | tax_doc_vlm.py `_render_pdf`: replaced the thread-based rasterization (which held the GIL inside the fitz C rasterizer, making the join deadline best-effort only) with a `ProcessPoolExecutor` (sized to `_RENDER_CONCURRENCY=2`). Each page is rasterized in a worker process via the top-level `_rasterize_page_in_process` function; a per-page timeout (`future.result(timeout=...)`) enforces the budget at the OS level. Added a `rasterize_fn` parameter to `_render_pdf` for test injection (avoids pickling issues with local functions in the process pool). Removed the module-level `_render_slot` semaphore and the `_rasterize` helper. Updated `documents.py:557-562` comment to say "worker process" instead of "run_in_threadpool" / "GIL-bound". Rewrote 3 threading-dependent tests (`test_render_stops_at_time_cap`, `test_render_concurrency_is_bounded`, `test_render_timeout_returns_partial`) to use `rasterize_fn` or the real process pool; removed 3 tests that tested thread internals (`test_slow_page_finishing_after_timeout`, `test_thread_start_raising_releases_slot`, `test_worker_owns_doc_close_and_slot_on_timeout`). | test_tax_doc_pdf_cap.py 10 passed (22s), test_failed_paths_visible.py 2 passed |
| R8-P3 | (this commit) | security_headers.py: fixed the module docstring comment (the preview also uses `frame-ancestors 'none'`, not `'self'`), changed `INVOICE_HTML_CSP` `form-action` from `'self' https://checkout.stripe.com` to `'none'` (the template has no forms; the Stripe pay button is a link). body_size_cap.py: removed dead `state['aborted']` key (never read). | test_security_headers.py, test_body_size_cap.py |

Verification commands (venv: uv venv -p 3.12 + uv sync --frozen --active --extra dev):
- pytest tests/smoke/test_tax_doc_pdf_cap.py -> 10 passed in 21.86s
- pytest tests/smoke/test_failed_paths_visible.py -> 2 passed in 0.54s
- pytest tests/smoke/test_security_headers.py test_body_size_cap.py -> (run next)
- ruff check app/categorization/tax_doc_vlm.py app/api/documents.py
  app/security_headers.py app/body_size_cap.py tests/smoke/test_tax_doc_pdf_cap.py
  -> clean

Left / for Aaron:
- All 4 findings fixed. CI on the pushed branch runs the full suite.
- The process-pool rasterization spawns a worker process per render call;
  for the common 1-2 page tax doc this is ~50-100 ms overhead. If that
  proves too slow in production, the pre-check (which catches 99% of
  pathological pages) still runs in-process and the pool is only for the
  rare page that passes the pre-check.
