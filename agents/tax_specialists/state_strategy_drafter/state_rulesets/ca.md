# State Ruleset — California (CA)

<!-- Appended to system_prompt.md at runtime when --state ca -->
<!-- Status: PLACEHOLDER — TODO — Content not yet populated from CA FTB/CDTFA research -->
<!-- Last updated: 2026-05-29 -->
<!-- Primary authority: CA Rev. & Tax. Code — https://leginfo.legislature.ca.gov/faces/codesTOCSelected.xhtml?tocCode=RTC -->
<!-- CA FTB Corporation Tax: https://www.ftb.ca.gov/businesses/index.html -->
<!-- CA CDTFA Sales Tax: https://www.cdtfa.ca.gov/ -->

---

## CA State Ruleset — California C-Corporation Tax Strategy

**STATUS: TODO — This ruleset is a placeholder. All sections below require population
from California Franchise Tax Board (FTB) and California Department of Tax and Fee
Administration (CDTFA) research before use in production strategy analysis.**

When this ruleset is active, the specialist MUST flag every topic with:
```
flag_for_cpa_review(
    form_reference="State Strategy — CA — Ruleset TODO",
    content="CA state ruleset has not been populated. All CA analysis is based on
    the general framework in the base system prompt only. CPA must verify all CA
    positions against current CA FTB and CDTFA guidance before relying on this
    analysis. California is a high-complexity state; engage CA-licensed tax counsel.",
    confidence="low",
    citations=["CA Rev. & Tax. Code §23151"]
)
```

**PRIORITY NOTE:** California is the highest-complexity state in this framework.
CA has unique provisions (minimum franchise tax, unitary group rules, economic nexus
rules broader than most states, aggressive audit posture). Engage CA-licensed tax
counsel before taking any CA tax position.

---

### CA.1 — Corporate Franchise Tax Rate

**TODO:** Populate from CA FTB.

Known framework (verify):
- CA corporate franchise tax: 8.84% on California net income (or minimum franchise
  tax of $800, whichever is greater).
- S-corporations pay 1.5% on net income plus the $800 minimum.
- LLC franchise tax: $800/year (plus graduated fees on gross receipts). Not applicable
  here (C-corp focus).
- Alternative Minimum Tax (CA): applies at 6.65% on California AMT income — verify
  if applicable.
- Form: CA Form 100 (California Corporation Franchise or Income Tax Return).
**Citation:** CA Rev. & Tax. Code §23151 — TODO verify current rate.
**Source:** https://www.ftb.ca.gov/businesses/index.html — TODO verify.

---

### CA.2 — §168(k) Bonus Depreciation Non-Conformity

**TODO:** Populate from CA FTB.

Known framework (verify):
- California does NOT conform to §168(k) bonus depreciation.
- CA also has its own §179 limitation ($25,000 — lower than federal limit) — TODO verify.
**Citation:** CA Rev. & Tax. Code §24356 — TODO verify.

---

### CA.3 — CA NOL Rules

**TODO:** Populate from CA FTB.

Known framework (verify):
- CA NOL: Generally allows 20-year carryforward; no carryback for corporations
  (California suspended NOL deductions in some prior years during budget crises —
  verify whether any suspension is in effect for the applicable tax year).
**Citation:** CA Rev. & Tax. Code §24416 — TODO verify.

---

### CA.4 — CA Apportionment — Three-Factor with Market-Based Sourcing

**TODO:** Populate from CA FTB.

Known framework (verify):
- CA uses a three-factor formula weighted toward sales (or elective single-sales-factor
  for some industries — verify current election options).
- Market-based sourcing for services: customer-location-based receipts.
- CA has throwback rules for sales of tangible personal property where the seller lacks
  nexus in the destination state.
- Unitary business group rules may require combined reporting for related entities.
**Citation:** CA Rev. & Tax. Code §25128 — TODO verify.

---

### CA.5 — CA Economic Nexus Threshold (Sales Tax / Wayfair)

**TODO:** Populate from CA FTB / CDTFA.

Known framework (verify):
- CA Wayfair economic nexus: $500,000 in gross sales in prior or current calendar year
  (no transaction count test) — TODO verify.
**Citation:** CA Rev. & Tax. Code §6203 — TODO verify.
**Source:** https://www.cdtfa.ca.gov/industry/outofstate.html — TODO verify.

---

### CA.6 — CA SaaS Taxability

**TODO:** Populate from CA CDTFA.

Known framework (verify):
- California generally does NOT impose sales tax on SaaS (sale of services, not TPP).
- But there are nuances around prewritten software delivered electronically vs. SaaS
  hosted remotely — TODO verify current CDTFA guidance for AI API services.
**Citation:** CDTFA publication 109 (Technology Transfer Agreements) and related — TODO verify.

---

### CA.7 — CA R&D Tax Credit

**TODO:** Populate from CA FTB.

Known framework (verify):
- California offers an R&D credit at 15% of qualifying expenditures over a base
  amount, and 24% for payments to qualified research consortia.
- Carryforward: unlimited.
- Non-refundable.
**Citation:** CA Rev. & Tax. Code §23609 — TODO verify.

---

### CA.8 — CA VDA Strategy

**TODO:** Populate from CA FTB / CDTFA.

Known framework (verify):
- CA FTB has a voluntary disclosure program.
- CA CDTFA has a separate voluntary disclosure program for sales/use tax.
- CA lookback period and penalty waiver mechanics — TODO verify.
**Citation:** CA Rev. & Tax. Code §19411 (FTB VDA) — TODO verify.
**Source:** https://www.ftb.ca.gov/businesses/voluntary-disclosure.html — TODO verify.

---

### CA.9 — CA Minimum Franchise Tax ($800)

**TODO:** Verify current amount and any exceptions for new corporations.

Known framework (verify):
- Every CA corporation (and LLC) owes a minimum $800 franchise tax, even if it has
  zero income or a loss.
- New corporations may be exempt from the $800 minimum for their first tax year —
  verify current exemption rules.
**Citation:** CA Rev. & Tax. Code §23153 — TODO verify.

---

## TODO Checklist (CA Ruleset)

- [ ] CA.1 — Confirm current corporate franchise tax rate (8.84%) and AMT applicability
- [ ] CA.2 — Confirm CA §168(k) non-conformity and CA §179 limit for applicable year
- [ ] CA.3 — Confirm CA NOL rules and whether any suspension is in effect
- [ ] CA.4 — Confirm CA three-factor apportionment, throwback rules, and unitary group rules
- [ ] CA.5 — Confirm CA Wayfair economic nexus threshold ($500K)
- [ ] CA.6 — Confirm CA SaaS taxability for AI API services (CDTFA guidance)
- [ ] CA.7 — Confirm CA R&D credit rate (15%/24%) and carryforward rules
- [ ] CA.8 — Confirm CA FTB and CDTFA VDA mechanics and lookback periods
- [ ] CA.9 — Confirm CA minimum franchise tax and new-corp exemption
- [ ] Add CA.10 — CA combined reporting / unitary business group rules
- [ ] Add CA.11 — CA controversy procedure (Board of Equalization / Office of Tax Appeals)
- [ ] Add CA.12 — CA LLC fee structure (not C-corp but relevant for affiliate entities)
- [ ] Add CA.13 — Bay Area local taxes (SF gross receipts tax, etc.) if entity has SF presence

**Assigned to:** CA-licensed CPA or CA tax counsel (CPA the CPA / TaxSquare may not be
CA-licensed — verify credentials before relying on CA tax advice).
**Priority:** P1 if entity has CA customers over $500K (sales tax nexus) or CA employees.
