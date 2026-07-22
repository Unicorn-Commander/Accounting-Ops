# Accounting-Ops — What It Does, and What It Does Not

> # ⚠️ UNREVIEWED DRAFT — NOT PUBLISHED
>
> **This document has not been reviewed by a lawyer and is not published or shown
> to any customer.** It is a drafting input for counsel and for whoever owns
> pre-purchase messaging. Search for `[COUNSEL:` for open questions.
>
> Draft date: 2026-07-20 · Status: awaiting review

---

**This is the honest version. Read it before you pay.**

We wrote this because the fastest way to make someone angry is to let them
believe the software does something it does not. Every limitation below is real
and current. We would rather lose the sale than have you find these out in
April.

---

## What it does

**Bookkeeping**
- Connects to your bank (via SimpleFIN), Stripe, and eBay, and pulls in
  transactions automatically.
- Categorizes them using AI, and learns from your corrections.
- Maintains a real double-entry general ledger — not a spreadsheet with
  categories.
- The ledger is **append-only and hash-chained**: nothing is ever silently
  edited or deleted. Corrections are posted as reversing entries, so the history
  and the correction are both visible. This is what makes your books defensible
  if anyone ever asks.
- Produces profit-and-loss statements, balance sheets, trial balances, cash-flow
  and cash-position reports, and runway.
- Handles multiple entities and multiple workspaces, so an accountant can run
  many clients.
- Lets you ask questions in plain language and get answers grounded in your
  actual ledger.

**Tax form preparation**
- Fills in **19 tax forms** as print-ready PDFs, including:
  - **Individual:** Form 1040, Schedules A, B, C, D, E, SE, Form 4868
    (extension), Form W-4P
  - **Business:** Form 1120, Form 1120-S, Form 1065, Schedule K-1 (1065)
  - **Payroll:** Form 941
  - **State:** SC ST-3 (sales & use), SC1120 (SC C-corporation), California
    Form 540
  - **Notices:** IRS CP504 response letter, SC DOR notice response letter
- For **C-corporations**, it derives Form 1120 figures directly from your
  general ledger, and shows you which ledger accounts produced each line.
- Refuses to invent numbers. If it cannot trace a figure to a source, it returns
  an error rather than guessing. Every generated form is watermarked as a draft.

---

## What it does NOT do

**It does not file anything, electronically or otherwise.**
There is no e-filing. None. The software does not connect to IRS e-file, to any
state filing portal, to EFTPS, or to any e-signature rail for returns. It gives
you a PDF. **You print it, sign it, and mail it.** This is not a feature we are
about to ship — the transmit path is deliberately disabled in the code.

**It does not sign your return, and neither do we.**
You are the preparer and the signer of your own return. We hold no PTIN. Nobody
at our company reviews your return before it goes out. If you want a
professional to review it, hire one — and we think you should for anything
consequential.

**It is not a CPA, and it does not give advice.**
It is software. It does not give tax, legal, accounting, or financial advice, and
using it does not create a professional relationship with anyone.

**It does not calculate depreciation.**
There is no Form 4562, and no MACRS or §179 depreciation engine. Form 1120 has a
line for depreciation and the software will carry a figure there — but **you have
to supply that figure**, from your accountant or your own schedule. If you have
significant fixed assets, this is a real gap and you should know about it now.

**Ledger-to-return derivation is C-corporation only.**
The automatic "derive the return from your books" path works for **Form 1120
(C-corp)**. Forms 1120-S, 1065, and the 1040 schedules can be filled, but they
are not yet derived from your ledger the same way — you supply more of the
figures yourself.

**State coverage is South Carolina and California only.**
SC: ST-3, SC1120, DOR notice responses. CA: Form 540. **That is the complete
list.** If you need a return for any other state, this software will not produce
it. There is no partial support and no workaround.

**It does not do payroll, and it does not move money.**
No payroll processing, no tax payments, no transfers, no trades. Form 941 is
prepared as a PDF; the actual payroll is yours to run elsewhere.

**It is not a substitute for reviewing your own books.**
The AI is wrong sometimes. Categorizations, figures, and answers must be checked
before you rely on them.

---

## Who this is a good fit for

- A US small business or solo operator who wants their books kept
  automatically, with real double-entry rigor.
- A **C-corporation**, especially in **South Carolina**, that wants its Form 1120
  built from its actual ledger.
- A bookkeeper or accountant running several clients who wants the mechanical
  work automated and an audit trail they can defend.
- Someone who is going to have a professional review and file the return, and
  wants to hand that professional a clean, complete package.

## Who this is NOT a good fit for — yet

- Anyone who needs **e-filing**. Go elsewhere. This is the single biggest reason
  to not buy this.
- Anyone with **significant depreciable assets** who needs the depreciation
  calculated.
- Anyone needing a state return **outside SC or CA**.
- Anyone who wants a licensed professional to prepare, review, and sign their
  return. That is a different kind of service and we are not it.
- Anyone who wants software they never have to check.

---

## The short version

**Accounting-Ops keeps your books and prepares your forms. You review them, you
sign them, and you mail them.**

If that division of labor is not what you want, this is not the right product,
and we would rather tell you now.

---

> `[COUNSEL: Four questions on this document.
>
> (1) Should acknowledging this be a required step at checkout? It was written
> as the anti-refund document — the honest scope statement a customer sees before
> paying. Its value in a "this didn't do what I expected" dispute depends a lot
> on whether we can show the customer saw it. Recommend a checkout checkbox with
> a timestamped record, but that is a legal/UX call. Note it also interacts with
> the Terms §5 refund policy, which is unwritten.
>
> (2) Is the plain, unhedged tone a liability or an asset? The instinct in legal
> review is usually to soften language like "Go elsewhere." Our view is that the
> bluntness is the entire point — a hedged version of this document does not
> prevent the dispute it exists to prevent. Please push back only where the
> language creates actual risk, not where it is merely uncomfortable.
>
> (3) These limitations will change as the product ships features. Depreciation
> and additional entity derivations are actively being built. This document must
> be versioned and updated in lockstep with releases, and a stale version is
> worse than none — it becomes a misrepresentation. Recommend an owner and a
> release-checklist item.
>
> (4) Does anything here need to appear in the Terms themselves rather than only
> in a separate document, to be contractually effective? If so, tell us which
> statements and we will move them.]`

## Verification note

Every claim above was checked against the source, not assumed:

- **19 forms** — counted from the form registry (`app/exports/form_registry.py`).
- **SC and CA only** — the only non-federal jurisdictions in that registry.
- **No e-filing** — the transmit path is hard-gated and raises unconditionally;
  no MeF integration, no Form 8879, no EFIN handling anywhere in the repository.
- **No PTIN, no Circular 230 process** — repo-wide search returns zero hits for
  both.
- **No depreciation engine** — Form 1120 line 20 ("Depreciation from Form 4562")
  exists in the line mapping, but no Form 4562 renderer is registered and no
  MACRS/§179 computation exists.
- **C-corp-only derivation** — the ledger-to-return mapping module covers Form
  1120; no equivalent derivation exists for 1120-S or 1065 on the current trunk.
- **Append-only, hash-chained ledger** — enforced by database triggers that
  reject `UPDATE` and `DELETE` for every role including the table owner.

**These will drift as features ship. Re-verify before publishing.**
