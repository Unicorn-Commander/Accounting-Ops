# SCDOR Crawl Notes

## General guidance

SCDOR (South Carolina Department of Revenue) hosts at `https://dor.sc.gov`.
All pages are publicly accessible without authentication.

User-Agent:
```
User-Agent: Accounting-Ops-TaxCurator/1.0 (you@example.com; non-commercial research)
```

## Key pages to monitor

| Topic | URL |
|---|---|
| C-Corp tax center | https://dor.sc.gov/tax-index/business-income-taxes/corporate |
| SC1120 instructions PDF | https://dor.sc.gov/forms-site/forms/sc1120i.pdf |
| SC1120 form PDF | https://dor.sc.gov/sites/dor/files/forms/SC1120.pdf |
| IRC Conformity Guide | https://dor.sc.gov/sites/dor/files/Documents/Policy%20Manuals/Conformity%20Guide%2021-23.pdf |
| Voluntary Disclosure | https://dor.sc.gov/businesses/nexus/voluntary-disclosure |
| Nexus info | https://dor.sc.gov/businesses/nexus |
| Penalty Waiver (95-2) | https://dor.sc.gov/administrative-interest-penalties-waivers-penalty-waiver-0 |
| Penalty Waiver (02-5) | https://dor.sc.gov/administrative-interest-penalties-waivers-penalty-waiver-3 |

## Key SC facts (as of 2026-05-29 — verify on each curator run)

- **Corporate income tax rate:** 5.0% (flat) per SC Code §12-6-530.
- **IRC conformity date:** SC conforms to the IRC as of a fixed date (rolling
  conformity NOT adopted). As of 2023 instructions, conformity date was
  December 31, 2021. Verify the 2024 conformity date from the latest SC1120 instructions.
- **Bonus depreciation:** SC decoupled. SC requires an IRC §168(k) addition-back
  (add back the federal bonus depreciation, then deduct 20%/year over 5 years).
- **§163(j):** SC may NOT conform to the TCJA §163(j) limitation. Verify each year.
- **§199A pass-through deduction:** Not applicable (C-corps only at the entity level).
- **GILTI:** SC may require addition of GILTI if not conforming.
- **NOL:** SC has its own NOL rules; does not necessarily mirror federal 80% limitation.

## Administrative dissolution risk

Per SC Code §33-14-210, a corporation that fails to file its annual report and
pay state fees can be administratively dissolved by the SC Secretary of State.
This is a separate risk track from SCDOR penalty exposure and must be flagged
separately by the Audit Defense Specialist.

## Filing due dates

- SC1120 due: same as federal Form 1120 (April 15 for calendar-year corps, or
  15th day of the 4th month after fiscal year-end; October 15 with extension).
- SC accepts the federal extension automatically if filed; no separate SC extension
  required if federal extension is in place. Verify each year.
