# State Ruleset — Florida (FL)

<!-- Appended to system_prompt.md at runtime when --state fl -->
<!-- Status: PLACEHOLDER — TODO — Content not yet populated from FL DOR research -->
<!-- Last updated: 2026-05-29 -->
<!-- Primary authority: FL Statutes Chapter 220 — https://www.flsenate.gov/Laws/Statutes/2024/Chapter220 -->
<!-- FL DOR Corporate Tax: https://floridarevenue.com/taxes/taxesfees/Pages/corporate.aspx -->

---

## FL State Ruleset — Florida C-Corporation Tax Strategy

**STATUS: TODO — This ruleset is a placeholder. All sections below require population
from Florida Department of Revenue (FL DOR) research before use in production strategy
analysis.**

When this ruleset is active, the specialist MUST flag every topic with:
```
flag_for_cpa_review(
    form_reference="State Strategy — FL — Ruleset TODO",
    content="FL state ruleset has not been populated. All FL analysis is based on
    the general framework in the base system prompt only. CPA must verify all FL
    positions against current FL DOR guidance before relying on this analysis.",
    confidence="low",
    citations=["FL Stat. §220.11"]
)
```

---

### FL.1 — Corporate Income Tax Rate

**TODO:** Populate from FL DOR.

Known framework (verify):
- Florida corporate income tax rate: 5.5% (standard rate). Rate has been reduced
  in some years — verify current rate for the applicable tax year.
- Florida has NO individual income tax. This is a significant distinction; only
  C-corporations and certain entities pay FL corporate income tax.
- Form: F-1120 (Florida Corporate Income/Franchise and Emergency Excise Tax Return).
- Filing due date: April 1 (note: April 1, NOT April 15, for calendar-year corps)
  — TODO verify.
**Citation:** FL Stat. §220.11 — TODO verify current rate.
**Source:** https://floridarevenue.com/taxes/taxesfees/Pages/corporate.aspx — TODO verify.

---

### FL.2 — §168(k) Bonus Depreciation Conformity

**TODO:** Populate from FL DOR.

Known framework (verify):
- Florida has historically conformed to §168(k) bonus depreciation — BUT verify
  the current conformity date and any decoupling enacted by the FL legislature.
**Citation:** FL Stat. §220.12 (conformity provision) — TODO verify.

---

### FL.3 — FL NOL Rules

**TODO:** Populate from FL DOR.

Known framework (verify):
- FL NOL rules — verify carryforward period and carryback rules.
- FL has generally followed federal NOL rules closely — TODO verify post-TCJA.
**Citation:** FL Stat. §220.13 — TODO verify.

---

### FL.4 — FL Apportionment Formula

**TODO:** Populate from FL DOR.

Known framework (verify):
- Florida uses three-factor apportionment: (Property + Payroll + 2×Sales) / 4
  (double-weighted sales factor) — TODO verify current formula.
- Market-based sourcing for services — TODO verify.
**Citation:** FL Stat. §220.15 — TODO verify.

---

### FL.5 — FL Economic Nexus Threshold (Sales Tax / Wayfair)

**TODO:** Populate from FL DOR.

Known framework (verify):
- FL Wayfair economic nexus: $100,000 in taxable remote sales in prior calendar year
  (effective July 1, 2021). No transaction count test (as of current knowledge) —
  TODO verify.
**Citation:** FL Stat. §212.0596 — TODO verify.
**Source:** https://floridarevenue.com/taxes/taxesfees/Pages/sales_tax.aspx — TODO verify.

---

### FL.6 — FL SaaS Taxability

**TODO:** Populate from FL DOR.

Known framework (verify):
- Florida's SaaS taxability is not settled — has been subject to varying DOR guidance.
- Some communications services may be subject to FL communications services tax (CST)
  rather than sales tax.
- CPA must verify current FL DOR guidance for AI API services specifically.
**Citation:** FL Stat. §212.05 — TODO verify.

---

### FL.7 — FL R&D Tax Credits

**TODO:** Populate from FL DOR.

Known framework (verify):
- Florida does NOT have a standalone state R&D credit comparable to SC §12-6-3415 or
  the federal §41 credit (as of prior-year knowledge) — TODO verify for current year.
- FL does have a sales tax exemption for R&D equipment — TODO verify.
**Citation:** FL Stat. §220.196 (R&D credit — if applicable) — TODO verify existence.

---

### FL.8 — FL VDA Strategy

**TODO:** Populate from FL DOR.

Known framework (verify):
- Florida has a voluntary disclosure program.
- FL participates in the MTC VDA program.
- FL lookback period and penalty waiver mechanics — TODO verify.
**Citation:** FL Stat. §213.21 — TODO verify.
**Source:** https://floridarevenue.com — TODO locate VDA page.

---

## TODO Checklist (FL Ruleset)

- [ ] FL.1 — Confirm current FL corporate income tax rate and due date (April 1?)
- [ ] FL.2 — Confirm FL §168(k) conformity status for applicable tax year
- [ ] FL.3 — Confirm FL NOL carryforward rules and post-TCJA conformity
- [ ] FL.4 — Confirm FL three-factor apportionment formula and market-based sourcing
- [ ] FL.5 — Confirm FL Wayfair economic nexus threshold ($100K, no transaction count)
- [ ] FL.6 — Confirm FL SaaS taxability and communications services tax applicability
- [ ] FL.7 — Confirm whether FL has any R&D or technology credit; confirm R&D equipment exemption
- [ ] FL.8 — Confirm FL VDA mechanics and lookback period
- [ ] Add FL.9 — FL intangible tax (if applicable — historically Florida had an intangibles tax; verify current status)
- [ ] Add FL.10 — FL controversy procedure (DOR administrative hearings, circuit court)
- [ ] Add FL.11 — FL local surtaxes (some Florida counties impose discretionary sales surtax)

**Assigned to:** CPA the CPA / TaxSquare or equivalent FL-licensed tax counsel.
**Priority:** P2 unless entity has significant FL operations or FL customer base approaching nexus threshold.
