# REPORT: PR #108 send-back round 4 (FINISHER-102, 11-24)

Branch agent/corporal-qwen/f5fb8b1c (PR #108). Base 45b9b28e (R3-6).
R4 fixes only: read aba471d3 (claude-sonnet) REQUEST_CHANGES on R3-4.
R3-1, R3-3, R3-5, R3-6 verified hold; no change. One commit per item.

| Item | Commit | Change | Tests (locked pins fastapi 0.136.3 / starlette 1.3.1) |
|------|--------|--------|--------------------------------------------------------|
| R4-0 (R3-2) | n/a | over-cap Content-Length already 413'd at once, body not read (body_size_cap.py:114-119); no change | n/a (already in place) |
| R4-1 | b11de79 | _render_pdf loop = min(max_pages, page_count); caller opens doc for page_count; 1-page W-2 no longer IndexError | test_tax_doc_pdf_cap.py 6 passed in 60.83s |
| R4-2 | 4a1a16d | worker appends to its own list (worker_out); caller out.extend after join; loop vars bound (B023) | test_tax_doc_pdf_cap.py 7 passed in 61.00s |
| R4-3 | 8c47b71 | thread.start() wrapped: on raise, release slot + re-raise (no leaked slot) | test_tax_doc_pdf_cap.py 8 passed in 60.96s |
| R4-4 | 1b2c214 | doc=None guard (if doc is not None: close); body_size_cap docstring: app entered but aborted, never sees truncated body | test_tax_doc_pdf_cap.py 8 passed in 60.97s |

Final full-file run (both touched test files): 14 passed in 75.78s
(test_tax_doc_pdf_cap.py 8 + test_body_size_cap.py 6).
ruff check app/categorization/tax_doc_vlm.py tests/smoke/test_tax_doc_pdf_cap.py: clean.
ruff check app/body_size_cap.py: 1 pre-existing RUF100 (line 162, app/ non-blocking in CI;
present before this round; my change is docstring-only).

R4-4 note: the R3-4 test's fake doc shares one real fitz doc between the caller's
page_count open (R4-1) and the worker's open, so the worker's close (now a bare
doc.close() that no longer swallows ValueError) would raise on the already-closed
shared real doc; the fake's close() now tolerates that double-close. This is a test
fixture detail, not a production behavior change.

Left / for Aaron: none blocking. The pre-existing RUF100 in body_size_cap.py:162
(app/, non-blocking) is out of scope for R4; I left it untouched per "touch nothing
else." No em dashes used.
