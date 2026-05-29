# State Ruleset — Georgia (GA)

<!-- Appended to system_prompt.md at runtime when --state ga -->
<!-- Status: PLACEHOLDER — TODO — Content not yet populated from GA DOR research -->
<!-- Last updated: 2026-05-29 -->
<!-- Primary authority: O.C.G.A. Title 48 — https://advance.lexis.com/container?config=00JAA3ZTU0NTM4OC0xMjY0LTRhMDAtYTg4Mi00Njk5M2RiNjRiNjQKAFBvZENhdGFsb2f9B0K5U2MdCi0yM2VhNGVkMg&crid=e8c5e1e0-7e6d-4aa4-a9fe-c9ce82c3d28a -->
<!-- GA DOR Corporate Income Tax: https://dor.georgia.gov/taxes/business-taxes/corporate-income-tax -->

---

## GA State Ruleset — Georgia C-Corporation Tax Strategy

**STATUS: TODO — This ruleset is a placeholder. All sections below require population
from Georgia DOR research before use in production strategy analysis.**

When this ruleset is active, the specialist MUST flag every topic with:
```
flag_for_cpa_review(
    form_reference="State Strategy — GA — Ruleset TODO",
    content="GA state ruleset has not been populated. All GA analysis is based on
    the general framework in the base system prompt only. CPA must verify all GA
    positions against current Georgia DOR guidance before relying on this analysis.",
    confidence="low",
    citations=["O.C.G.A. Title 48"]
)
```

---

### GA.1 — Corporate Income Tax Rate

**TODO:** Populate from Georgia DOR.

Known framework (verify):
- Georgia corporate income tax rate: 5.75% (as of 2024; subject to rate reduction
  schedule enacted by Georgia legislature — verify current rate for tax year).
- Form: Georgia Form 600 (C Corporation Income Tax Return).
- Filing due date: April 15 for calendar-year corporations.
**Citation:** O.C.G.A. §48-7-21 (verify).
**Source:** https://dor.georgia.gov/taxes/business-taxes/corporate-income-tax — TODO verify.

---

### GA.2 — §168(k) Bonus Depreciation Conformity

**TODO:** Populate from GA DOR.

Known framework (verify):
- Georgia historically partially conformed to §168(k) (allowed 50% bonus depreciation
  as of prior-year guidance). Verify current conformity status for the applicable year.
**Citation:** O.C.G.A. §48-7-21 (Georgia IRC conformity provision) — TODO verify section.

---

### GA.3 — GA Net Operating Loss Rules

**TODO:** Populate from GA DOR.

Known framework (verify):
- GA NOL rules follow federal rules with some modifications.
- Carryforward period and carryback rules — TODO verify.
**Citation:** O.C.G.A. §48-7-21 — TODO verify.

---

### GA.4 — GA Apportionment Formula

**TODO:** Populate from GA DOR.

Known framework (verify):
- Georgia uses single sales factor apportionment for most corporations (O.C.G.A. §48-7-31).
- Market-based sourcing for services — TODO verify effective date.
**Citation:** O.C.G.A. §48-7-31 — TODO verify.

---

### GA.5 — GA Economic Nexus Threshold (Sales Tax / Wayfair)

**TODO:** Populate from GA DOR.

Known framework (verify):
- GA economic nexus: $100,000 in sales OR 200 transactions — TODO verify current threshold.
- Form: ST-3 (Sales and Use Tax Return).
**Citation:** O.C.G.A. §48-8-2 — TODO verify.
**Source:** https://dor.georgia.gov/taxes/sales-use-tax — TODO verify.

---

### GA.6 — GA R&D Tax Credit

**TODO:** Populate from GA DOR.

Known framework (verify):
- Georgia offers an R&D credit under O.C.G.A. §48-7-40.25.
- Credit rate and qualifying expenditures — TODO verify.
**Citation:** O.C.G.A. §48-7-40.25 — TODO verify.

---

### GA.7 — GA SaaS Taxability

**TODO:** Populate from GA DOR.

Known framework (verify):
- Georgia historically treated SaaS as taxable as a telecommunications service.
- Current treatment — TODO verify with GA DOR guidance.
**Citation:** GA DOR Policy Bulletin SUT-2013-2 (or successor) — TODO verify.

---

### GA.8 — GA VDA Strategy

**TODO:** Populate from GA DOR.

Known framework (verify):
- Georgia participates in the Multistate Tax Commission (MTC) Voluntary Disclosure
  Program.
- GA DOR also has its own voluntary disclosure program — TODO populate mechanics,
  lookback period, penalty waiver details.
**Citation:** O.C.G.A. §48-2-43 — TODO verify.
**Source:** https://dor.georgia.gov — TODO locate VDA page.

---

### GA.9 — GA Failure-to-File Penalty and Interest

**TODO:** Populate from GA DOR.

Known framework (verify):
- GA FTF penalty rate and interest rate — TODO verify.
**Citation:** O.C.G.A. §48-2-44 — TODO verify.

---

## TODO Checklist (GA Ruleset)

Before this ruleset is used in production, the following must be completed:

- [ ] GA.1 — Confirm current corporate income tax rate and any enacted rate reductions
- [ ] GA.2 — Confirm current §168(k) conformity status for applicable tax year
- [ ] GA.3 — Confirm GA NOL carryforward rules and any post-TCJA conformity differences
- [ ] GA.4 — Confirm GA apportionment formula and market-based sourcing rules
- [ ] GA.5 — Confirm GA Wayfair nexus thresholds and any transaction count test
- [ ] GA.6 — Confirm GA R&D credit rate, qualifying expenditures, and carry-forward rules
- [ ] GA.7 — Confirm GA SaaS taxability with current DOR guidance
- [ ] GA.8 — Confirm GA VDA mechanics, lookback period, and penalty waiver details
- [ ] GA.9 — Confirm GA FTF penalty rate and interest rate
- [ ] Add GA.10 — GA nexus analysis (economic nexus for income tax vs. sales tax)
- [ ] Add GA.11 — GA throwback/throwout rules
- [ ] Add GA.12 — GA Job Tax Credit or comparable credits
- [ ] Add GA.13 — GA controversy and appeal procedure (DOR protest, Tax Tribunal, GA Court of Appeals)

**Assigned to:** CPA Mosin / NorthbridgeTax or equivalent GA tax counsel.
**Priority:** P1 if entity has GA nexus or is expanding into GA.
