# State Ruleset — Texas (TX)

<!-- Appended to system_prompt.md at runtime when --state tx -->
<!-- Status: PLACEHOLDER — TODO — Content not yet populated from TX Comptroller research -->
<!-- Last updated: 2026-05-29 -->
<!-- Primary authority: TX Tax Code Title 2 — https://statutes.capitol.texas.gov/Docs/TX/htm/TX.171.htm -->
<!-- TX Comptroller Franchise Tax: https://comptroller.texas.gov/taxes/franchise/ -->

---

## TX State Ruleset — Texas C-Corporation Tax Strategy

**STATUS: TODO — This ruleset is a placeholder. All sections below require population
from Texas Comptroller of Public Accounts research before use in production strategy
analysis.**

When this ruleset is active, the specialist MUST flag every topic with:
```
flag_for_cpa_review(
    form_reference="State Strategy — TX — Ruleset TODO",
    content="TX state ruleset has not been populated. All TX analysis is based on
    the general framework in the base system prompt only. CPA must verify all TX
    positions against current TX Comptroller guidance before relying on this analysis.
    NOTE: Texas has NO corporate income tax; it uses a franchise tax (margin tax) instead.",
    confidence="low",
    citations=["TX Tax Code §171.001"]
)
```

**CRITICAL NOTE:** Texas has NO corporate income tax. The base system prompt's
references to 'corporate income tax' and 'conformity with the IRC' do NOT directly
apply to Texas. Texas imposes a FRANCHISE TAX (also called the "margin tax") that is
based on revenue, not taxable income. This is a fundamentally different tax structure
than any other state in this framework. All IC-section references in the base system
prompt are largely inapplicable to the TX franchise tax.

---

### TX.1 — Texas Franchise Tax ("Margin Tax") — Not an Income Tax

**TODO:** Populate from TX Comptroller.

Known framework (verify):
- Texas franchise tax is NOT a corporate income tax. It is a gross-margin-based tax.
- Tax base: the lesser of (a) 70% of total revenue, (b) total revenue minus COGS,
  or (c) total revenue minus compensation (each subject to a $1 million deduction).
- Tax rate: 0.75% for most businesses; 0.375% for businesses primarily engaged in
  retail or wholesale. Verify current rates.
- No franchise tax owed if total revenue ≤ $2.47 million (2024 threshold — verify;
  threshold increases annually).
- Form: Texas Franchise Tax Report (Form 05-158 or 05-169).
- Due date: May 15 (not April 15) — TODO verify.
**Citation:** TX Tax Code §171.101 — TODO verify.
**Source:** https://comptroller.texas.gov/taxes/franchise/ — TODO verify.

---

### TX.2 — No §168(k) or IRC Conformity Issues (TX)

**NOTE:** Because Texas taxes gross margin (not net income), §168(k) bonus depreciation,
IRC conformity, and NOL carryforwards are largely irrelevant for Texas franchise tax
purposes. The only IRC conformity issue is whether federal COGS or compensation
definitions affect the Texas margin computation.

**TODO:** Verify whether the Texas COGS deduction tracks federal COGS definitions.
**Citation:** TX Tax Code §171.1012 (COGS deduction) — TODO verify.

---

### TX.3 — TX Apportionment (Receipts Factor)

**TODO:** Populate from TX Comptroller.

Known framework (verify):
- Texas uses a single-receipts-factor apportionment based on where the customer
  receives the benefit (market-based sourcing for services).
- For software/SaaS services: receipts sourced to the location of the customer.
- Throwback rule: Texas does NOT have a throwback rule — confirm.
**Citation:** TX Tax Code §171.103 — TODO verify.

---

### TX.4 — TX Economic Nexus Threshold (Sales Tax / Wayfair)

**TODO:** Populate from TX Comptroller.

Known framework (verify):
- TX Wayfair economic nexus: $500,000 in gross revenue from Texas sales in a 12-month
  period (no transaction count test) — TODO verify.
**Citation:** TX Tax Code §151.107 — TODO verify.
**Source:** https://comptroller.texas.gov/taxes/sales/ — TODO verify.

---

### TX.5 — TX SaaS Taxability

**TODO:** Populate from TX Comptroller.

Known framework (verify):
- Texas imposes sales tax on SaaS as a "data processing service."
- 80% of the sale price of a data processing service is taxable; 20% is not taxable
  (as of prior-year knowledge) — TODO verify current rule.
- AI API services (Anthropic, OpenAI) — likely subject to TX sales tax as data
  processing services — CPA must verify.
**Citation:** TX Tax Code §151.0101 (taxable services); TX Admin. Code §3.330 (data
processing) — TODO verify.

---

### TX.6 — TX R&D Tax Credits / Incentives

**TODO:** Populate from TX Comptroller.

Known framework (verify):
- Texas has offered franchise tax R&D credits in the past (expired; verify if
  reinstated or replaced for the applicable tax year).
- Texas Enterprise Zone and other incentive programs — not directly R&D credits but
  relevant for expansion planning.
**Citation:** TX Tax Code §171.651 (R&D credit if applicable) — TODO verify.

---

### TX.7 — TX Franchise Tax VDA Strategy

**TODO:** Populate from TX Comptroller.

Known framework (verify):
- TX Comptroller has a voluntary disclosure program for franchise tax.
- TX lookback period and penalty waiver mechanics — TODO verify.
**Citation:** TX Tax Code §111.103 — TODO verify.
**Source:** https://comptroller.texas.gov/taxes/audit/voluntary-disclosure/ — TODO verify.

---

## TODO Checklist (TX Ruleset)

- [ ] TX.1 — Confirm current TX franchise tax rates (0.75% / 0.375%) and no-tax threshold
- [ ] TX.2 — Confirm TX COGS deduction tracks federal COGS (or deviates)
- [ ] TX.3 — Confirm TX receipts-factor apportionment and no-throwback rule
- [ ] TX.4 — Confirm TX Wayfair economic nexus threshold ($500K)
- [ ] TX.5 — Confirm TX SaaS taxability as "data processing services" (80% rule)
- [ ] TX.6 — Confirm whether any TX R&D franchise tax credit is currently available
- [ ] TX.7 — Confirm TX Comptroller VDA mechanics and lookback period
- [ ] Add TX.8 — TX gross-receipts-based nexus for franchise tax (separate from sales tax nexus)
- [ ] Add TX.9 — TX controversy procedure (Comptroller hearings, SOAH, district court)
- [ ] Add TX.10 — TX local taxes (some Texas cities/counties have local sales tax — verify)
- [ ] Add TX.11 — TX no-individual-income-tax note (relevant for officer compensation planning)

**Assigned to:** CPA the CPA / NorthbridgeTax or equivalent TX-licensed tax counsel.
**Priority:** P2 unless entity has significant TX revenue approaching $500K (sales tax nexus) or TX employees.
