# IRS Crawl Notes

## General guidance

- All IRS public pages and PDFs are freely accessible without authentication.
- PDF URLs follow the pattern `https://www.irs.gov/pub/irs-pdf/<filename>.pdf`.
- HTML instruction pages follow `https://www.irs.gov/forms-pubs/about-form-<formnum>`.
- Use `trafilatura` (or `httpx` + `html2text`) to extract clean text from HTML.
  For PDFs, use `pypdf` or `pdfminer.six`.

## Rate limiting

IRS.gov is a federal site with Cloudflare protection.  Add a `User-Agent` header:
```
User-Agent: Accounting-Ops-TaxCurator/1.0 (aaron@magicunicorn.tech; non-commercial research)
```
Respect `Retry-After` headers.  Default inter-request delay: 2 seconds.

## Known stable PDF paths (as of 2026-05-29)

| Form/Pub | URL |
|---|---|
| Form 1120 instructions | https://www.irs.gov/pub/irs-pdf/i1120.pdf |
| Form 1125-E instructions | https://www.irs.gov/pub/irs-pdf/i1125e.pdf |
| Publication 536 (NOL) | https://www.irs.gov/pub/irs-pdf/p536.pdf |
| Publication 946 (Depreciation) | https://www.irs.gov/pub/irs-pdf/p946.pdf |
| 1099-NEC + 1099-MISC instructions | https://www.irs.gov/pub/irs-pdf/i1099mec.pdf |
| Rev. Proc. 2024-9 | https://www.irs.gov/pub/irs-drop/rp-24-09.pdf |
| Notice 2024-85 | https://www.irs.gov/pub/irs-drop/n-24-85.pdf |

## Things to watch for

- IRS updates PDFs with a new URL when they publish a new tax year revision.
  The `about-form-XXXX` HTML page always links to the current revision.
  When a PDF 404s, check the about-form page for the redirect.

- IRS sometimes publishes "draft" instructions before the final version.
  Look for the word "DRAFT" in the document title — do NOT ingest draft versions.
  Set `is_current=FALSE` if a draft is accidentally ingested and a final is later found.

- The OBBBA (One Big Beautiful Bill Act) is pending as of 2026-05-29.
  Its provisions for §174 restoration, §163(j) ATI fix, and bonus depreciation
  revival have NOT yet been enacted into law.  Store them as `tax_year=null` with
  `source_authority='Congress'` and a note in the title: "[PENDING LEGISLATION]".
  Update to `source_authority='IRS'` only after IRS publishes guidance reflecting
  enacted changes.
