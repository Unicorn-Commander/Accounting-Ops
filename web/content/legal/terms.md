[COUNSEL: the open-source edition's licence changed from AGPL-3.0 to Apache-2.0 on 2026-10-24; review this document.]

# Terms of Service — Accounting-Ops (Hosted)

> # ⚠️ UNREVIEWED DRAFT — NOT IN FORCE
>
> **This document has not been reviewed by a lawyer. It is not published, not
> linked from the product, and does not govern anyone's use of anything.** It
> is a drafting input for counsel. Search this file for `[COUNSEL:` to find
> every open question.
>
> Draft date: 2026-07-20 · Status: awaiting legal review

---

**Provider:** Magic Unicorn LLC ("we", "us")
**Service:** the hosted Accounting-Ops service
**You:** the person or company that opens an account ("you", "Customer")

> `[COUNSEL: Confirm the contracting entity. The repo's LICENSE and DISCLAIMER.md
> both say "Magic Unicorn LLC". Confirm state of formation and that this is the
> entity that will actually invoice customers. If a different entity operates the
> hosted service, this needs to change everywhere in all four documents.]`

These terms cover the **hosted** service that we run for you. They do not cover
the self-hosted Community Edition, which you get under the Apache License 2.0 and which
comes with no warranty and no contract with us — see `LICENSE` and
`DISCLAIMER.md` in the source repository.

---

## 1. What you are buying

Accounting-Ops is **software**. It is an AI-assisted bookkeeping and
tax-form-preparation tool. It:

- connects to your bank, payment, and marketplace accounts and pulls in
  transactions;
- categorizes those transactions and posts them to a double-entry general
  ledger;
- produces financial statements from that ledger;
- fills in tax forms as **print-and-mail PDFs**;
- lets you ask questions about your books in plain language.

Section 3 of the **Scope of Service** document lists precisely what is and is
not included. Read it. It is short and it is the honest version.

## 2. What you are NOT buying — read this section

This is the most important section in this agreement, and we would rather lose
a sale than have you misunderstand it.

**2.1 — We are not your accountant, and this is not advice.** Accounting-Ops
is not a Certified Public Accountant, an Enrolled Agent, a tax preparer, an
attorney, or a fiduciary. Nothing it produces is tax, legal, accounting, or
financial advice. Using it does not create a professional relationship, an
engagement, or a privileged communication with us or with anyone.

**2.2 — We are not a tax return preparer, and you are.** You prepare your own
returns using this software as a tool. You review them. You sign them. You file
them. We do not sign returns, we hold no PTIN, and no one at our company reviews
your return before it goes out. The software helps you assemble a return; **you
are the preparer of record and the signer.**

> `[COUNSEL: This is the central legal question in this document and it needs a
> real answer, not a disclaimer. The concern is whether preparing substantially
> complete returns for a fee makes us a "tax return preparer" under IRC
> §7701(a)(36) / Treas. Reg. §301.7701-15 regardless of what our contract says,
> which would trigger PTIN requirements (IRC §6109(a)(4)), §6694 preparer
> penalties, and §7216 restrictions on disclosure/use of return information.
> Related but separate: unauthorized practice of law/accountancy exposure in the
> states we sell into. Please advise on (a) whether the current design is
> defensible, (b) whether specific product changes are needed to stay outside
> the preparer definition, and (c) whether §7216 consent language is required
> before we disclose return information to any subprocessor — see the Privacy
> Policy, this may be the binding constraint on our AI vendor list.]`

**2.3 — There is no electronic filing.** The service does not transmit anything
to the IRS, to any state revenue department, or to any other authority. It does
not connect to e-file (MeF), to EFTPS, or to any payment or e-signature rail for
filings. It produces PDFs. You print them and you mail them. This is not a
temporary limitation we are working around — the transmit path is deliberately
disabled in the code and raises an error if invoked.

**2.4 — The AI can be wrong, and you must check its work.** Categorizations,
figures, summaries, form entries, and answers are generated in part by
statistical models. They are assistive. They are sometimes wrong, and they can
be wrong in ways that look confident and plausible. Source data can also be
incomplete or stale, and tax rules change.

**You must review every figure before you rely on it, file it, sign it, or pay
against it.** If you file a return without checking it, the consequences —
including tax, interest, and penalties — are yours.

**2.5 — Get a professional for anything that matters.** For a filing with a
government agency, a financial statement a third party will rely on, or any
decision with real legal or financial consequence, consult a licensed CPA, EA,
or attorney in your jurisdiction. We recommend this genuinely, not as a
formality.

## 3. Your account and your responsibilities

You agree to:

- give accurate account and entity information, and keep it current;
- keep your credentials secure, and tell us promptly if you think your account
  has been compromised;
- have the authority to connect any financial account you connect, and to
  upload any record you upload;
- **review the output** as described in section 2.4;
- comply with the law in how you use the service.

You are responsible for everything done under your account, including by users
you invite and by any AI agent you authorize to act on your behalf.

If you use Accounting-Ops to do bookkeeping for *your* clients (the service
supports this — multiple workspaces, many clients), then as between you and us,
**you** are responsible to those clients for the work, and you are responsible
for having the right to put their data into our service. See the DPA.

## 4. Acceptable use

Do not:

- use the service to commit or conceal fraud, tax evasion, or money laundering,
  or to falsify records;
- upload data you do not have the right to upload;
- attempt to access another customer's data, or probe, scan, or test the
  security of the service without our prior written permission;
- attempt to bypass or disable the safety controls in the product — including
  the append-only ledger, the hash chain, the "do not file" watermarks on
  generated forms, or the disabled e-file path;
- resell or provide the hosted service to third parties as your own service
  without our agreement (using it to serve your own accounting clients is fine
  and expected — reselling *the platform itself* is not);
- overload the service, or use it in a way that degrades it for others;
- use it to build a competing product, or to extract our models, prompts, or
  form-mapping logic in bulk.

> `[COUNSEL: The self-hosted edition is Apache-2.0, so a competitive-use restriction
> on the hosted service sits alongside a licence that explicitly permits
> competitors to run their own copy. Please check this restriction is coherent
> with the Apache-2.0 grant and does not purport to restrict rights Apache-2.0 gives —
> we do not want to be accused of licence violation by our own terms. Consider
> narrowing to the hosted service's operational assets only.]`

We may suspend an account that we reasonably believe is doing any of the above,
or that is creating a security or legal risk. Where practical we will tell you
first and give you a chance to fix it.

## 5. Fees

> `[COUNSEL: Billing terms are unwritten because the pricing model is still being
> decided. Needed: billing period, renewal, price-change notice period, taxes,
> what happens on non-payment (grace period → suspension → termination), and
> whether there is a refund policy. Note the Scope of Service document exists
> specifically to reduce "this didn't do what I thought" refund requests — it
> would be reasonable to reference it here and require acknowledgement at
> checkout. That is a business decision.]`

`[PLACEHOLDER — fees, billing period, renewal, price changes, taxes, non-payment,
refunds]`

## 6. Availability

We aim to keep the service available and we monitor it. Things will still break
sometimes: we do maintenance, our infrastructure providers have incidents, and
the third-party services we connect to (banks, payment processors) go down on
their own schedule.

> `[COUNSEL / BUSINESS DECISION: Do NOT publish a number here until someone
> decides to commit to one. An uptime percentage is a contractual promise with
> teeth, and we do not currently have the monitoring, alerting, or credit
> mechanism to back one. The options are: (a) no SLA — "commercially reasonable
> efforts", the honest default for where we are; (b) a published target with no
> remedy; (c) a real SLA with service credits, which needs a defined measurement
> window, exclusions, and a credit schedule. Recommend (a) at launch. Whoever
> picks (c) must also build the measurement — an SLA we cannot measure is worse
> than none.]`

`[PLACEHOLDER — uptime commitment, or an explicit statement that none is given]`

**Regardless of the above:** because the service does not e-file, an outage does
not by itself cause you to miss a filing deadline — the deliverable is a PDF you
mail. Do not leave assembling a return until the day it is due, and do not treat
our availability as your deadline plan.

## 7. Your data

**7.1 — It is yours.** You keep all rights in the data you put into
Accounting-Ops and in the ledgers, statements, and forms it produces from that
data. We do not claim ownership.

**7.2 — What we do with it.** We process it to run the service for you, as
described in the Privacy Policy and, where it applies, the DPA. The Privacy
Policy lists every third party we send data to.

**7.3 — We do not train on your books.**

> `[COUNSEL: Confirm this statement is one we can actually make and defend. As
> built, the service sends ledger and tax content to third-party model APIs for
> inference (see Privacy Policy §4). Our commitment not to train is only as good
> as our vendors' contractual commitment not to train on API inputs. Before this
> sentence is published, someone must confirm the zero-retention / no-training
> terms are actually in force on the accounts we use in production, and we should
> hold the evidence. If they are not, this sentence must be deleted or rewritten.]`

**7.4 — Getting it out.** You can export everything at any time while your
account is active, in machine-readable form, including your stored documents.
This is a product feature, not a support request — see the Privacy Policy.

**7.5 — Aggregate and anonymized data.** We may use aggregate, de-identified
information about how the service is used (error rates, feature usage,
performance) to operate and improve it. This never includes your financial
records, your documents, or anything that identifies you or your customers.

## 8. Our intellectual property

We keep all rights in the service itself — the software, models, prompts, form
mappings, tax logic, and documentation — except for what is licensed to you
under the Apache License 2.0 in the open-source edition. These terms give you a right to use
the hosted service, nothing more.

## 9. Warranties, and the absence of them

We will provide the service with reasonable skill and care.

**Beyond that, and to the maximum extent the law allows, the service is
provided "as is" and we disclaim all other warranties** — express or implied —
including merchantability, fitness for a particular purpose, and
non-infringement.

**Specifically and without limiting the above, we do not warrant that:**

- any categorization, calculation, figure, form entry, or answer is correct;
- the ledger, statements, or tax forms are complete, accurate, or suitable for
  filing;
- the service will be uninterrupted or error-free;
- using it will produce a particular tax outcome, or avoid tax, interest, or
  penalties.

> `[COUNSEL: Some jurisdictions do not allow the exclusion of implied warranties,
> and several US states have consumer-protection statutes that may not be
> disclaimable. Add the standard carve-out, and advise whether we should be
> selling to consumers at all versus business-only. Business-only would simplify
> this materially — flag if you agree, because it is a product/go-to-market
> decision that should be made now rather than after launch.]`

## 10. Limitation of liability

> `[COUNSEL: The number and structure below are placeholders. This clause needs
> real drafting attention because of the specific risk here: an incorrect figure
> flows into a filed tax return, and the customer is assessed tax, interest, and
> penalties. That is a large, foreseeable, and directly-caused harm, and a court
> may be unsympathetic to a cap that leaves the customer holding it — especially
> against a consumer.
>
> Please advise on: (a) the cap amount and structure — fees paid in the trailing
> 12 months is conventional but may be very small relative to the harm; (b)
> whether tax penalties and interest should be an explicit exclusion, which is
> the usual approach for tax software, and whether that is enforceable; (c)
> whether a "penalty and interest reimbursement" offering — as TurboTax and
> H&R Block provide, and which is a commercial promise rather than a liability
> concession — is a better answer than a bare exclusion, both legally and
> commercially; (d) the mandatory carve-outs (fraud, wilful misconduct, death or
> personal injury, and anything else that cannot be capped in the governing
> jurisdiction).]`

**10.1** Neither party is liable for indirect, incidental, special,
consequential, or punitive damages, or for lost profits, revenue, goodwill, or
data, even if told such damages were possible.

**10.2** Our total liability for all claims relating to the service is capped at
`[PLACEHOLDER — amount / formula]`.

**10.3** For clarity, and given section 2: **you are responsible for your own tax
positions, filings, and payments.** We are not liable for tax, interest, or
penalties assessed against you. `[COUNSEL: enforceability — see above]`

**10.4** These limits do not apply to `[PLACEHOLDER — mandatory carve-outs]`.

## 11. Indemnity

> `[COUNSEL: Draft. At minimum a customer indemnity for their unlawful use, for
> data they had no right to upload, and — importantly for the accountant use
> case — for claims brought by *their* clients arising from work they did using
> our software. Consider whether we offer any IP indemnity in return; a
> one-directional indemnity may not survive negotiation with larger accounting
> firm customers.]`

`[PLACEHOLDER]`

## 12. Term, termination, and what happens to your data

**12.1 — You can leave at any time.** Cancel from the product or by contacting
us. `[COUNSEL: refund on mid-period cancellation — see §5]`

**12.2 — We can terminate** for material breach if you do not fix it within
`[PLACEHOLDER — cure period]` days of notice, immediately for the serious
misuse in section 4, or on `[PLACEHOLDER]` days' notice if we discontinue the
service.

**12.3 — Export your data first.** For **`[PLACEHOLDER — recommend at least 30]`
days** after termination, you can still request a full export. Do it. Export is
self-service while your account is active; after termination it becomes a
support request, and after the window closes we may no longer have the data.

**12.4 — Then we delete it.** After the export window, we delete your workspace
data on the schedule and by the method described in the Privacy Policy, subject
to anything we are legally required to keep.

**12.5 — How deletion actually works, honestly.** Your general ledger is
append-only and hash-chained — that is a deliberate feature, and it is what makes
your books tamper-evident and defensible in an audit. It also means we do not
surgically delete individual entries from a live ledger, because doing so would
silently corrupt the integrity guarantee your remaining books depend on.

So deletion is **at the level of the whole workspace**: we cut off access, purge
your bank credentials and stored documents promptly, and then drop the workspace's
records as a unit. The Privacy Policy describes this in detail, including what we
do immediately versus on a delay, and what we keep and why. We would rather
explain this plainly than promise a granularity we cannot deliver.

## 13. Changes to these terms

We may update these terms. For material changes we will give you notice
`[PLACEHOLDER — mechanism and period]` before they take effect. Continuing to
use the service after that means you accept the new terms; if you do not, you
may terminate.

## 14. Governing law and disputes

> `[COUNSEL: Governing law, venue, and dispute mechanism are all open. The
> business is `[state]`-based. Decisions needed: (a) governing law and exclusive
> venue; (b) arbitration versus courts, and if arbitration, the provider, rules,
> seat, and who pays; (c) class-action waiver — check enforceability in the
> states we sell into, this varies; (d) jury-trial waiver; (e) whether consumer
> customers get carve-outs. If we go business-only per §9, much of this
> simplifies.]`

`[PLACEHOLDER — governing law, venue, dispute resolution]`

## 15. General

`[PLACEHOLDER — assignment, entire agreement, severability, no waiver, force
majeure, notices, survival, relationship of the parties]`

> `[COUNSEL: Standard boilerplate, but please confirm the survival clause keeps
> sections 2 (no advice / customer is preparer), 7 (data), 9 (warranties), 10
> (liability), and 14 (governing law) alive after termination — section 2 in
> particular needs to survive, because a dispute over a filed return will surface
> long after the account is closed.]`

---

## Appendix — what we verified before drafting this

So counsel can see which statements are engineering facts rather than
aspirations. These were confirmed against the source at the time of drafting and
should be re-confirmed before publication.

| Claim in this document | How it was verified |
|---|---|
| No e-filing exists (§2.3) | The transmit path is hard-gated and raises unconditionally; a repo-wide search found no MeF integration, no Form 8879, no EFIN handling |
| We hold no PTIN and no one reviews returns (§2.2) | Repo-wide search: `PTIN` — 0 hits; `Circular 230` — 0 hits; `Form 8879` — 0 hits |
| Generated forms are watermarked "do not file" | `DISCLAIMER.md`; provenance guard refuses to emit figures it cannot trace to a source |
| The ledger is append-only and hash-chained (§12.5) | Database triggers reject `UPDATE` and `DELETE` on `journal_entry` and `event_log` for **every** role, including the table owner; a separate statement-level trigger blocks `TRUNCATE` |
| Surgical entry deletion would corrupt the chain (§12.5) | The hash covers the payee and description fields; editing or removing an entry causes the chain verifier to report tampering permanently |
| Export includes stored documents (§7.4) | Implemented in `app/data_rights/export.py` |
| Deletion is workspace-level (§12.5) | Implemented in `app/data_rights/erasure.py`; rationale documented in `docs/DATA-RIGHTS.md` |
