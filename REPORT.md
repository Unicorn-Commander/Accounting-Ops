# REPORT: PR #108 send-back round 5 (FINISHER-103, 11-24)

Branch agent/corporal-qwen/f5fb8b1c (PR #108). Base a024e00 (R4).
GATE ACCESS 5edf1ba1 (claude-fable) on a024e00 = REQUEST_CHANGES.
Fixed ALL four R5 items; one commit each; subjects AO108-R5-<id>.

| Item | Commit | Change | Tests (locked fastapi 0.136.3 / starlette 1.3.1) |
|------|--------|--------|--------------------------------------------------|
| P1-1 | c1c585c | HTML invoice routes (pay page GET /pay/{token} + preview) set INVOICE_HTML_CSP on their own response; middleware uses setdefault so the per-route header wins and the strict default stays for every other route; template has no forms, only external target is the hosted Stripe Checkout button, so form-action 'self' https://checkout.stripe.com; pay page frame-ancestors 'none' | test_security_headers.py 5 passed in 3.73s (pay page on real app: HTML CSP + inline style/fonts/data: logo all allowed; JSON route keeps strict) |
| P2-1 | ed53d57 | body_size_cap capped_receive: on the chunk that crosses the cap, send the 413 and raise _BodyCapExceeded at once; the crossing chunk is never returned to the app, so a handler never runs on an over-cap body; later receives return http.disconnect; docstring order fixed | test_body_size_cap.py 8 passed in 19.52s (11-byte chunk cap=10 and 5+6 chunks cap=10 = 413 AND handler side-effect absent) |
| P2-2 | 8dcdd35 | the per-page thread.join budget cannot fire while PyMuPDF holds the GIL, so it is best-effort only (said so in the comments); added the hard bound: _page_content_bytes sums a page's (uncompressed) content-stream xref lengths with no rasterization, and _render_pdf refuses a page over _PAGE_CONTENT_MAX_BYTES (128 KB) before it is handed to the C rasterizer (fast, no page, partial error, no slot); extract() and the documents.py call site now run the PDF work off the event loop via run_in_threadpool | test_tax_doc_pdf_cap.py 10 passed in 78.78s (a REAL 8000-rect page, ~334 KB content stream, built with pymupdf, no time.sleep monkeypatch, is refused fast <2s; a control page still rasterizes to a PNG) |
| P2-3 | f8ff9e3 | cpa-export 400/404/403/other log lines: log status + body length + a request ref (form + entity + year) only, never response.text; the raised detail is still the fixed per-class message | test_cpa_export_error_leak.py 4 passed in 0.71s + test_cpa_export_client.py 5 passed in 0.71s (a 400 body with a marker + fake assertion value reaches no log record; the 400 + ref are logged) |

Final full-file run (all touched test files): 32 passed.
ruff check on all 11 touched files (security_headers, invoices, main,
body_size_cap, tax_doc_vlm, documents, _cpa_export_client + 4 test files):
clean. No em dashes in any added line.

Left / for Aaron:
- P3s from the review: the prompt says "only if each is one line", but I could
  not read the claude-fable review text (offline; no gh / forge access from
  this run), so I could not enumerate them. I fixed only the four R5 items,
  as instructed. If you want the P3s, paste the review text and I will do the
  one-liners.
- /docs and /redoc are kept enabled (decision made in P1-1): the edge does not
  block them and the API is the only surface that documents it; they keep the
  strict default CSP. Say the word if you want them disabled instead.
- No RUF100 or other new ruff findings; the pre-existing RUF100 in
  body_size_cap.py was already addressed in the round before this one.
