# Treasury / CFR / Congress Crawl Notes

## Treasury Regulations (26 CFR)

The official source for Treasury Regulations (Treas. Reg.) is the Electronic Code
of Federal Regulations at https://www.ecfr.gov/current/title-26.

Key regulations relevant to the current specialists:
| Regulation | Topic | URL pattern |
|---|---|---|
| Treas. Reg. §1.174-1 | R&E expenditures | https://www.ecfr.gov/current/title-26/chapter-I/subchapter-A/part-1/section-1.174-1 |
| Treas. Reg. §1.163(j)-1 through -11 | Business interest | https://www.ecfr.gov/current/title-26/chapter-I/subchapter-A/part-1/section-1.163(j)-1 |
| Treas. Reg. §1.172-1 | NOL deduction | https://www.ecfr.gov/current/title-26/chapter-I/subchapter-A/part-1/section-1.172-1 |
| Treas. Reg. §301.6651-1 | Failure-to-file penalty | https://www.ecfr.gov/current/title-26/chapter-I/subchapter-F/part-301/section-301.6651-1 |

eCFR is freely accessible without authentication. Extract just the regulation text.
Skip the eCFR navigation chrome (left-panel tree, breadcrumbs, etc.).

## Congress.gov (OBBBA / pending legislation)

URL: https://www.congress.gov/bill/119th-congress/house-bill/1

The OBBBA tracker source has `refresh_days: 14` because bill status can change
rapidly during conference committee and floor votes. The curator must fetch this
on every run within the 14-day window.

**CRITICAL note for ingest:**  Store pending legislation with
`source_authority = 'Congress'` and include `[PENDING LEGISLATION]` in the title.
Do NOT store it as `source_authority = 'IRS'` or `source_authority = 'CFR'`.
The specialists must distinguish between enacted law and pending bills.

Once enacted, SCDOR will eventually publish conformity guidance. At that point,
create a new SC-jurisdiction document with `source_authority = 'SCDOR'` and
supersede the old one.

## IRS Notices and Revenue Procedures

IRS Notices are at: https://www.irs.gov/pub/irs-drop/n-YY-NNN.pdf
Revenue Procedures: https://www.irs.gov/pub/irs-drop/rp-YY-NNN.pdf

These are stable once published (IRS does not edit issued notices/rev-procs).
Set `refresh_days: 365` for them once confirmed final, or `refresh_days: 180`
for initial ingestion.

## Rate limits

Congress.gov has Cloudflare protection. Use the same User-Agent as IRS/SCDOR.
Add a 3-second delay between Congress.gov requests.
eCFR.gov is generally more permissive but still rate-limit to 1 req/sec.
