# State Ruleset — New York (NY)

<!-- Appended to system_prompt.md at runtime when --state ny -->
<!-- Status: PLACEHOLDER — TODO — Content not yet populated from NY DTF research -->
<!-- Last updated: 2026-05-29 -->
<!-- Primary authority: NY Tax Law Article 9-A — https://www.nysenate.gov/legislation/laws/TAX -->
<!-- NY DTF Corporate Tax: https://www.tax.ny.gov/bus/ct/ctidx.htm -->

---

## NY State Ruleset — New York C-Corporation Tax Strategy

**STATUS: TODO — This ruleset is a placeholder. All sections below require population
from New York Department of Taxation and Finance (NY DTF) research before use in
production strategy analysis.**

When this ruleset is active, the specialist MUST flag every topic with:
```
flag_for_cpa_review(
    form_reference="State Strategy — NY — Ruleset TODO",
    content="NY state ruleset has not been populated. All NY analysis is based on
    the general framework in the base system prompt only. CPA must verify all NY
    positions against current NY DTF guidance before relying on this analysis.",
    confidence="low",
    citations=["NY Tax Law Article 9-A"]
)
```

---

### NY.1 — Corporate Franchise Tax (Article 9-A)

**TODO:** Populate from NY DTF.

Known framework (verify):
- NY corporate franchise tax rate: highest of (a) 6.5% on business income, (b) 0.1875%
  on allocated business capital, (c) $19/thousand of NY receipts (alternative tax), or
  (d) minimum tax ($25–$200K based on NY receipts). Verify current rates for tax year.
- NYC has a SEPARATE NYC corporate tax — this ruleset covers NY State only.
- Form: CT-3 (NY General Business Corporation Franchise Tax Return).
**Citation:** NY Tax Law §210 — TODO verify current rate.
**Source:** https://www.tax.ny.gov/bus/ct/ctidx.htm — TODO verify.

---

### NY.2 — §168(k) Bonus Depreciation Non-Conformity

**TODO:** Populate from NY DTF.

Known framework (verify):
- New York does NOT conform to §168(k) bonus depreciation. Must add back federal bonus
  depreciation to NY income and use NY-allowable depreciation instead.
- Very similar to SC treatment — high priority for any entity with significant capex.
**Citation:** NY Tax Law §208.9(b)(16) — TODO verify current section.

---

### NY.3 — NY Net Operating Loss Rules

**TODO:** Populate from NY DTF.

Known framework (verify):
- NY NOL rules have historically differed from federal (pre- and post-TCJA).
- NY NOL: 20-year carryforward (Article 9-A); no carryback for NY purposes — TODO verify.
**Citation:** NY Tax Law §208.9(f) — TODO verify.

---

### NY.4 — NY Apportionment Formula

**TODO:** Populate from NY DTF.

Known framework (verify):
- NY moved to a single-receipts-factor apportionment formula effective for tax years
  beginning on or after January 1, 2015 (single sales factor; market-based sourcing).
- Market-based sourcing for services: customer-location-based.
- Throwback rule: NY may have throwback provisions — TODO verify.
**Citation:** NY Tax Law §210-A — TODO verify.

---

### NY.5 — NY Economic Nexus Threshold (Sales Tax / Wayfair)

**TODO:** Populate from NY DTF.

Known framework (verify):
- NY Wayfair economic nexus: $500,000 AND 100 transactions in a calendar year — TODO verify.
- NY also had a click-through nexus rule (referral nexus) prior to Wayfair — TODO verify
  current status.
**Citation:** NY Tax Law §1101(b)(8)(iv) — TODO verify.
**Source:** https://www.tax.ny.gov/bus/st/stidx.htm — TODO verify.

---

### NY.6 — NY SaaS Taxability

**TODO:** Populate from NY DTF.

Known framework (verify):
- New York treats SaaS as taxable "computer software" delivered electronically.
- SaaS subscriptions to NY customers are generally subject to NY sales tax.
- CPA must confirm current NY DT guidance, especially for AI API services.
**Citation:** NY Tax Law §1101(b)(6) and TSB-M-10(7)S — TODO verify.

---

### NY.7 — NY R&D Tax Credit

**TODO:** Populate from NY DTF.

Known framework (verify):
- NY offers an R&D credit (Investment Tax Credit / Excelsior Jobs Program).
- Verify applicable credit program for a small AI company.
**Citation:** NY Tax Law §210-B — TODO verify.

---

### NY.8 — NY VDA Strategy

**TODO:** Populate from NY DTF.

Known framework (verify):
- NY DTF has a voluntary disclosure program.
- NY lookback period and penalty waiver mechanics — TODO verify.
- NY also participates in the MTC VDA program.
**Citation:** NY Tax Law §1145 — TODO verify.
**Source:** https://www.tax.ny.gov/bus/voldisc/ — TODO verify.

---

### NY.9 — NY "Convenience of Employer" Rule

**TODO:** Populate from NY DTF.

Known framework (verify):
- NY has a unique "convenience of the employer" rule: if an employee works outside
  NY for the employee's own convenience (not because the employer requires it), those
  work days are allocated to NY for payroll/apportionment purposes.
- Critical for any entity with employees who work remotely from outside NY by choice.
- Significant apportionment and payroll tax implications.
**Citation:** NY Tax Law §631(b)(1)(B) — TODO verify; NY DTF Technical Services Bulletin.

---

## TODO Checklist (NY Ruleset)

- [ ] NY.1 — Confirm current corporate franchise tax rate and alternative minimum calculations
- [ ] NY.2 — Confirm current §168(k) non-conformity and NY depreciation rules
- [ ] NY.3 — Confirm NY NOL carryforward rules and post-TCJA conformity
- [ ] NY.4 — Confirm NY single-receipts-factor apportionment and market-based sourcing
- [ ] NY.5 — Confirm NY Wayfair economic nexus thresholds
- [ ] NY.6 — Confirm NY SaaS taxability with current DT guidance (especially AI APIs)
- [ ] NY.7 — Identify applicable NY R&D or startup credits
- [ ] NY.8 — Confirm NY VDA mechanics and lookback period
- [ ] NY.9 — Confirm "convenience of employer" rule applicability to remote employees
- [ ] Add NY.10 — NYC corporate tax (separate analysis — not covered in this state ruleset)
- [ ] Add NY.11 — NY controversy and appeal procedure (Division of Tax Appeals, Tribunal, Appellate Division)

**Assigned to:** CPA the CPA / TaxSquare or equivalent NY-licensed tax counsel.
**Priority:** P1 if entity has NY customers (possible sales tax nexus) or NY employees.
