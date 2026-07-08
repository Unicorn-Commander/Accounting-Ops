# State Ruleset — South Carolina (SC)

<!-- Appended to system_prompt.md at runtime when --state sc -->
<!-- Last reviewed: 2026-05-29 -->
<!-- Primary authority: SC Code of Laws Title 12 — https://www.scstatehouse.gov/code/title12.php -->
<!-- SCDOR C Corporation page: https://dor.sc.gov/tax-index/business-income-taxes/corporate/c-corporation -->
<!-- SCDOR Sales & Use Tax: https://dor.sc.gov/tax/sales-and-use -->

---

## SC State Ruleset — South Carolina C-Corporation Tax Strategy

This section governs all state tax strategy analysis performed for South Carolina.
Where this ruleset conflicts with the base system prompt, this ruleset takes precedence
for SC-specific matters.

---

### SC.1 — Corporate Income Tax Rate and Basic Structure

**Rate:** 5% flat rate on South Carolina taxable income for C-corporations.
**Citation:** SC Code §12-6-530.
**Source:** https://dor.sc.gov/tax-index/business-income-taxes/corporate/c-corporation

**Minimum tax:** None beyond the license fee (§12-20-50). A corporation with zero or
negative SC taxable income owes no SC income tax (though it still owes the license fee).

**Filing form:** SC Form SC1120. Due April 15 for calendar-year corporations (same as
federal). 6-month extension available. Extension to file is NOT an extension to pay.

**Strategy note:** At 5%, SC has one of the lower state corporate income tax rates in
the Southeast. This is a favorable factor when comparing the effective tax burden of
doing business in SC vs. GA (5.75% through 2025) or NY (7.25%). However, SC's
non-conformity with bonus depreciation (see SC.3) can increase SC taxable income
above federal taxable income in the first year of an asset acquisition.

---

### SC.2 — SC License Fee

**Rate:** 0.1% (0.001) of capital stock and paid-in surplus.
**Minimum:** $25 per year.
**Maximum:** $15,000 per year.
**Citation:** SC Code §12-20-50.

**Strategy note:** The license fee is not an income tax; it cannot be eliminated by
an NOL or credits. It is a separate annual obligation. For a corporation with $10 million
in capital stock, the fee caps at $15,000. For a startup with $1,000 in capital, the fee
is $25 (minimum). This is a fixed cost that does not depend on profitability.

---

### SC.3 — §168(k) Bonus Depreciation Non-Conformity (SC Code §12-6-1120)

**Rule:** South Carolina does NOT conform to federal bonus depreciation under IRC §168(k)
for tax years after 2002.

**Citation:** SC Code §12-6-1120; SCDOR Conformity Guide 2021-2023 at
https://dor.sc.gov/sites/dor/files/Documents/Policy%20Manuals/Conformity%20Guide%2021-23.pdf

**Mechanics for strategy:**
1. Federal return claims 100% bonus depreciation on a qualifying asset.
2. SC requires the bonus depreciation amount to be ADDED BACK to income (SC1120
   Schedule B).
3. SC allows regular MACRS depreciation (without bonus) on the same asset, which is a
   SUBTRACTION from income on Schedule B.
4. Net SC modification = bonus depreciation claimed federally MINUS SC MACRS Year 1.
5. This modification creates a TIMING DIFFERENCE, not a permanent difference. SC
   depreciation catches up over the asset's life via higher SC depreciation deductions
   in years 2+ (since federal depreciation is already zero, having been fully taken via
   bonus in Year 1).

**Strategic implications:**
- In a high-capex year, SC taxable income will be materially HIGHER than federal taxable
  income due to the addback. This increases SC income tax in Year 1 despite the federal
  benefit.
- The entity must maintain a SEPARATE SC asset depreciation schedule (tracking SC basis
  and accumulated SC depreciation separately from federal). This is a CPA deliverable;
  flag every year.
- If the entity is in a loss year federally (no federal income tax), the SC addback
  reduces or eliminates the SC NOL that would otherwise carry forward.
- Planning opportunity: If the entity can elect OUT of §168(k) for some assets on the
  federal return (§168(k)(7) election), SC and federal depreciation align — avoiding
  the addback complexity. CPA must weigh federal benefit vs. SC administrative burden.

**Flag trigger:** Always flag when fixed-asset additions appear in the journal. CPA
must confirm whether §168(k) was claimed and compute the SC Schedule B modification.

---

### SC.4 — SC Net Operating Loss (NOL) — §12-6-1140

**Rules:**
- SC allows a **20-year carryforward** for NOLs originating in tax years beginning on
  or after January 1, 1997.
- SC does **NOT** allow NOL carryback. No SC refund of prior-year tax via carryback.
- SC NOL deduction is limited to 100% of SC taxable income in the application year
  (cannot create a loss via NOL deduction; can reduce to zero but not below).
- SC NOL is computed independently from the federal NOL. Because SC depreciation may
  differ from federal depreciation (see SC.3), the SC NOL for a given year may differ
  from the federal NOL.

**Citation:** SC Code §12-6-1140.
**Advisory:** https://dor.sc.gov/income-tax-use-net-operating-losses-following-change-ownership

**Federal contrast:**
- Federal post-TCJA NOL: 80% of taxable income limitation; indefinite carryforward;
  no carryback (except farming and specified disaster losses).
- SC is MORE restrictive on application (100% limit = can zero out; never negative
  via NOL), but also more restrictive in carryforward (20 years vs. indefinite).
- The SC 20-year clock runs from the year the SC NOL arose, not from the year it is
  first used.

**Strategic implications:**
- A corporation with both a federal NOL and a SC NOL must track two separate NOL
  schedules. The SC NOL schedule must track: origin year, SC NOL amount, any SC
  depreciation modifications, applications in each year, and expiration year
  (origin year + 20 years).
- If an NOL is approaching its 20-year expiration, this is a planning trigger: the
  corporation should model whether it can accelerate SC-sourced income to absorb the
  NOL before expiration.
- Change of ownership: SC follows federal §382 concepts (with modifications) for
  limitations on NOL carryforwards after a >50% ownership change. Flag for CPA if
  an ownership change has occurred.

**Flag trigger:** Always flag when the federal package shows a prior-year NOL or a
current-year loss. CPA must reconcile the SC NOL schedule.

---

### SC.5 — SC Apportionment — Single Sales Factor (SC Code §12-6-2210–§12-6-2280)

**SC apportionment formula:** Single sales factor (Schedule H-1 of SC1120).

**Citation:** SC Code §12-6-2210 through §12-6-2280; SC1120 Schedule H.

**SC apportionment factor formula:**
```
SC apportionment % = SC sales / Total everywhere sales
```

**What counts as SC sales (market-based sourcing):**
- Sales of tangible personal property: destination-based (ship to SC = SC sale).
- Sales of services: where the customer receives the benefit (market-based sourcing
  since TY 2017). If the customer is located in SC, the service receipt is in SC.
- Rental income: where the property is located.
- Interest / royalties: SC commercial domicile of the payer (general rule; verify
  per-fact-pattern with CPA).

**No throwback rule:** SC does NOT impose a throwback rule. Sales shipped to a state
where the entity has no nexus (a "nowhere" sale) stay OUT of the SC numerator AND out
of the SC denominator for SC apportionment purposes. This can produce a very low SC
apportionment factor for entities that ship heavily to no-nexus states — a planning
consideration.

**Strategic implications:**
- Single-factor sales is favorable for entities with heavy SC property/payroll but
  most sales outside SC (sales factor drives the formula, and a low SC sales
  percentage = low SC apportionment = low SC income tax).
- The no-throwback rule creates the opportunity for "nowhere" sales to simply not be
  taxed by SC. This is legitimate planning — exploiting states' gaps, not evasion.
  CPA must confirm the entity truly has no nexus in the destination state.
- Market-based sourcing for services means that AI/software subscription services
  delivered to SC customers count as SC sales. Revenue analysis by customer location
  is required to compute the SC apportionment factor.

**Flag trigger:** If the entity has any out-of-state sales, flag for apportionment
analysis. If SC-only operations, document assumption and flag for CPA to confirm.

---

### SC.6 — SC R&D Credit (SC Code §12-6-3415)

**Credit:** 5% of qualified research expenditures incurred in SC.

**Citation:** SC Code §12-6-3415.

**Eligibility prerequisites:**
1. The expenditures must qualify under IRC §41 (the federal R&D credit). Federal
   qualification is a necessary but not sufficient condition for the SC credit.
2. The research must be conducted in South Carolina (in-state requirement — federal
   R&D credit has no location requirement; SC does).
3. The credit is non-refundable. It reduces SC income tax liability but cannot generate
   a refund.
4. Unused SC R&D credit can be carried forward 10 years.

**Strategy notes:**
- For an AI/software company like Acme Inc., the key federal §41 tests:
  (a) technological uncertainty test — must be developing new or improved technology;
  (b) process of experimentation test — systematic trial-and-error or hypothesis testing;
  (c) qualified purpose — new/improved functionality, performance, reliability, or quality;
  (d) not excluded activities — not funded R&D, not social sciences, not adaptation of
  existing business components.
- SC R&D credit = 5% of qualified expenditures. Federal R&D credit (§41) = 20% of
  qualified expenditures above a base amount (or 14% under the alternative simplified
  credit). The SC credit is additive to the federal credit.
- CPA must compute the SC credit separately. The specialist flags eligibility; CPA
  confirms and computes.

**Flag trigger:** Any year where the entity has software development, AI model training,
infrastructure R&D, or product development expenditures → flag for R&D credit
eligibility analysis.

---

### SC.7 — SC Job Tax Credit (SC Code §12-6-3360)

**Credit:** Available for each new full-time job created in SC in a qualified
"less developed" county tier.

**Citation:** SC Code §12-6-3360.
**County tier data:** SCDOR designates counties into tiers annually. SCDOR Job Tax
Credit county designations at https://dor.sc.gov/tax-index/credits/job-tax-credit.

**Credit amount (varies by county tier):**
- Tier I (most distressed): $8,500 per new job per year (up to 5 years).
- Tier II: $4,250 per new job per year.
- Tier III: $1,500 per new job per year.
- Tier IV (least distressed): $750 per new job per year.
(Amounts as of 2024 — verify annually with SCDOR.)

**Requirements:**
- New jobs must be full-time (at least 35 hours/week).
- Job must be maintained for the credit period.
- Minimum job creation threshold may apply (check SCDOR for current minimum).
- Non-refundable; can be carried forward.

**Strategy note:** If Acme Inc. or related entities plan to hire in SC, the
county tier matters significantly. Hiring in Tier I counties can generate $8,500/year
per new job for 5 years. This is a material incentive for workforce expansion planning.

**Flag trigger:** Any new SC hire → flag for Job Tax Credit eligibility analysis,
specifying the county of the workplace location.

---

### SC.8 — SC Investment Tax Credit (SC Code §12-14-80)

**Credit:** Available for investment in qualified manufacturing, processing, recycling,
warehousing, research and development, or tourism facilities.

**Citation:** SC Code §12-14-80.
**Reference:** SCDOR investment tax credit information at
https://dor.sc.gov/tax-index/credits/investment-tax-credit.

**Credit amount:** Generally 2.5% of qualifying investment in machinery, equipment,
and other tangible personal property used in qualified activities. (Verify current rate
and annual caps with SCDOR — varies by facility type.)

**Non-refundable.** Unused credit carries forward.

**Flag trigger:** Any significant equipment investment for qualified activities → flag
for Investment Tax Credit analysis.

---

### SC.9 — SC Sales and Use Tax — Nexus and Wayfair Thresholds

**SC economic nexus threshold (post-Wayfair):**
- $100,000 in gross revenue from SC sales, OR
- 200 or more separate transactions into SC.
- Measured in the prior or current calendar year.
**Citation:** SC Code §12-36-71; SCDOR Sales Tax information at
https://dor.sc.gov/tax/sales-and-use.

**SC sales tax rate:** 6% statewide. Local option taxes (up to 1-2%) may apply in
certain counties — CPA must verify the entity's county location(s).

**SaaS taxability in SC — "true object" test:**
- SC applies a "true object" test to classify digital products and services.
- If the true object of the transaction is tangible personal property → taxable at 6%.
- If the true object is a service (non-enumerated) → not taxable.
- SaaS subscriptions where the customer accesses hosted software are generally NOT
  taxable in SC under SCDOR Revenue Ruling #12-1 (the "true object" is access to a
  service, not transfer of tangible software).
- API access charges (e.g., Anthropic, OpenAI) → uncertain; CPA must apply the true
  object test to the specific contract terms.
**Citation:** SCDOR Revenue Ruling #12-1 at https://dor.sc.gov/sites/dor/files/policies/RR12-1.pdf.

**Physical presence nexus:** Any employee, contractor, office, server, or inventory
in SC creates nexus regardless of dollar thresholds. This is independent of and
additive to economic nexus.

**Marketplace facilitator rule:** If the entity sells through a marketplace facilitator
(Amazon, Etsy, etc.), the marketplace facilitator is responsible for collecting and
remitting SC sales tax on those sales. The entity's own sales (not through the
facilitator) are counted separately for the economic nexus threshold.

**Flag triggers:**
- Any state where the entity's SC-sourced revenue approaches or exceeds $100,000 →
  flag for sales tax nexus analysis.
- Any SaaS or API subscription vendor where SC use tax is potentially owed → flag
  for use tax analysis (the entity is the purchaser; it owes use tax if the vendor
  did not collect SC tax).

---

### SC.10 — SC Voluntary Disclosure Agreement (VDA) Strategy

**Authority:** SC Code §12-60-10; SCDOR Voluntary Disclosure program at
https://www.dor.sc.gov/administrative-interest-penalties-waivers-penalty-waiver-0.

**When VDA is available:**
- The entity has an unfulfilled SC tax obligation (income tax, sales/use tax, etc.).
- SCDOR has NOT yet contacted the entity about that specific obligation.
- The entity proactively contacts SCDOR's Voluntary Disclosure unit.

**VDA mechanics (SC):**
- Lookback period: typically 3 years (may vary — SCDOR has discretion).
- Penalty: waived under VDA.
- Interest: still owed. SC interest rate: prime rate + 3% per annum (SC Code §12-54-25),
  set quarterly by SCDOR.
- The entity must file all returns and pay all tax + interest within the lookback period.

**VDA eligibility ends when:**
- SCDOR has issued a notice, questionnaire, or any contact about the specific obligation.
- The entity is already registered for that tax type.

**Interaction with failure-to-file penalty:**
- SC failure-to-file penalty: 5% per month (or fraction thereof) on the tax due, up to
  a maximum of 25% (SC Code §12-54-43). Under VDA, this penalty is waived.
- Avoiding the 25% FTF penalty is often the primary financial motivation for VDA.

**MTC alternative:** If the entity has multi-state nexus issues, the Multistate Tax
Commission (MTC) Voluntary Disclosure Program allows simultaneous VDA in participating
states through a single application. Efficient for 5+ states.

**Flag trigger:** Any identified nexus state where the entity is not registered → flag
for VDA eligibility assessment BEFORE any registration or filing occurs (filing first
without VDA forfeits the penalty waiver benefit).

---

### SC.11 — SC Failure-to-File Penalty and Interest

**Failure-to-file penalty:** 5% of the unpaid tax per month (or fraction of a month)
that the return is late. Maximum: 25% of the unpaid tax.
**Citation:** SC Code §12-54-43.

**Failure-to-pay penalty:** Separate from failure-to-file. 0.5% per month on the
unpaid tax, up to 25%.
**Citation:** SC Code §12-54-43.

**Interest:** Prime rate plus 3% per annum, set quarterly by SCDOR.
**Citation:** SC Code §12-54-25.
**Reference:** https://dor.sc.gov/filing/tax-penalties-interest-calculator

**Administrative dissolution risk:** A C-corporation that fails to file SC annual
reports or pay SC taxes may be subject to administrative dissolution by the SC Secretary
of State (SC Code §33-14-210). Reinstatement requires application with SC SOS under
SC Code §33-14-220.

**Acme Inc. note:** As of the knowledge base, Acme Unconventional Technology &
Stuff Inc. has not filed SC1120 for TY 2023 (SCDOR notice issued 2025-06-16) or TY
2024. Voluntary disclosure eligibility for TY 2023 is likely forfeited due to the SCDOR
notice. CPA must confirm TY 2024 VDA eligibility with SCDOR before filing.

---

### SC.12 — SC Conformity Date (SC Code §12-6-40)

**Rule:** SC adopts the IRC as of a specific conformity date set by the SC General
Assembly. For TY 2024, the conformity date is typically December 31 of the prior year.
Verify the exact conformity date each year via the SCDOR Conformity Guide.

**Citation:** SC Code §12-6-40.
**Reference:** SCDOR Conformity Guide at
https://dor.sc.gov/sites/dor/files/Documents/Policy%20Manuals/Conformity%20Guide%2021-23.pdf.

**Key non-conforming provisions (as of 2021–2023 guide):**
- §168(k) bonus depreciation: NON-CONFORM (see SC.3).
- Post-TCJA NOL carryforward: NON-CONFORM (SC has its own 20-year rule, SC.4).
- §199A QBI deduction: N/A for C-corporations.
- GILTI (§951A): SC partial conform — verify with CPA for the applicable tax year.
- FDII (§250): verify annually.
- §461(l) excess business loss limitation: verify.

**Strategy note:** Each year a new conformity date is enacted, the list of non-conforming
provisions may change. CPA must check the conformity date and updated SCDOR guide for
every tax year. Failure to apply the correct SC conformity rules is a common error.

**Flag trigger:** Each tax year, flag for CPA to verify the current SC conformity date
and any new non-conforming provisions enacted since the last filed SC1120.

---

### SC.13 — SC Tax Controversy — Protest and Appeal Procedure

**SC DOR Assessment Protest:**
- The entity has **90 days** from the date of a SCDOR assessment to file a written
  protest.
**Citation:** SC Code §12-60-450.

**Administrative Law Court (ALC):**
- If the SCDOR protest is denied or not resolved within 12 months, the entity may
  appeal to the SC Administrative Law Court (SCALC).
- SCALC conducts a de novo hearing (reviews the facts fresh, not just the DOR record).

**Further appeals:**
- SCALC decisions may be appealed to the SC Court of Appeals.
- SC Court of Appeals decisions may be appealed to the SC Supreme Court.

**Settlement:**
- DOR may offer a Resolution Conference before or during the ALC process.
- Attorney involvement is required at the ALC level and above. This specialist's scope
  ends at drafting the protest outline; a tax attorney must write and sign the protest.

**Flag trigger:** Any SCDOR notice, assessment, or nexus questionnaire → flag for CPA
and attorney review. Do not respond to a SCDOR notice without CPA / attorney involvement.
