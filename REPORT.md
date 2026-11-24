# REPORT: PR #108 send-back round 6, MERGE ONLY (FINISHER-103, 11-24)

Branch agent/corporal-qwen/f5fb8b1c (PR #108). Head 042da83.
#110 (BATCH H money + access) merged as main 218dbe7c; the PR branch was
not mergeable, so round 6 is a single merge commit, no product change.

| Item | Commit | Change | Tests (locked fastapi 0.136.3 / starlette 1.3.1, pytest 9.0.3) |
|------|--------|--------|---------------------------------------------------------------|
| R6-1 | 042da83 | Merge origin/main (218dbe7c) into the PR branch. One conflict, app/api/invoices.py imports: took both the #110 imports (PdfRenderUnavailableError, pay-link/checkout symbols) and this PR's INVOICE_HTML_CSP. Verified both sides sit on each other's routes: GET /pay/{token} + preview keep INVOICE_HTML_CSP (R5-P1-1) on top of #110's hashed-token lookup (hash_pay_token/token_hash), identical 404 for inactive tokens, /pay/complete/{invoice_id} return page, and the owner/admin regenerate route with audit row; app/main.py keeps both middleware sets (body cap + security headers from this PR, the rest from main); app/auth.py keeps this PR's AO-SW-5 ERROR log; CHANGELOG.md keeps both note sets. Auto-merged, no hand edits | this PR: test_security_headers.py 5 passed, test_body_size_cap.py 8 passed, test_tax_doc_pdf_cap.py 10 passed (95.67s, no stack); #110: test_checkout_pay_link_failure.py 7, test_pat_unit.py 48, test_ar_invoicing_unit.py 25 passed (0.80s). ruff check clean on app/api/invoices.py + all 6 test files |

Verification commands (venv: uv venv -p 3.12 + uv sync --frozen, locked
pins; dev extras installed per uv.lock: pytest 9.0.3, ruff 0.15.13):
- pytest tests/smoke/test_security_headers.py test_body_size_cap.py
  test_tax_doc_pdf_cap.py -> 23 passed, 1 warning in 95.67s
- pytest tests/smoke/test_checkout_pay_link_failure.py test_pat_unit.py
  test_ar_invoicing_unit.py -> 80 passed in 0.80s
- ruff check app/api/invoices.py + the 6 test files -> clean
- full CI suite NOT run here (instruction: run only the 6 named files)

Left / for Aaron:
- Nothing else in this round; it was merge-only. CI on the pushed branch
  runs the full suite; if it is red, it is pre-existing main debt, not a
  merge regression (the 6 named files are all green).
- No decisions needed; the conflict resolution kept both sides as #110's
  review required.
