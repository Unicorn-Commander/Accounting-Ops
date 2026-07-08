# The professional-substitute stance

Accounting-Ops is built to be an **autonomous professional substitute**, not a
form-filler you babysit. The agents are designed to do the work a bookkeeper or
staff accountant would do — end to end — and to keep going without asking for
permission at every step. Where a traditional tool stops at "here's a suggestion,
you decide," Accounting-Ops is willing to *do the job*: categorize, post,
reconcile, draft the statements, assemble the package.

> **This is a design stance, not a legal claim.** Accounting-Ops is software. It
> is **not** a CPA, an Enrolled Agent, a tax preparer, an attorney, or a
> fiduciary, and using it creates no professional relationship. You are
> responsible for everything you file, sign, or pay. See
> [DISCLAIMER.md](../DISCLAIMER.md).

## Optional autonomy, not mandated human-in-the-loop

The default is that the agent gets real work done. Human review is a **control
you can turn up or down**, not a tax on every action:

- **Deterministic auto-post** for low-risk, high-confidence work (rule-matched,
  entity-mapped, sign-consistent, below the materiality threshold).
- **Queue for confirm** for mid-materiality or model-suggested items.
- **Hard block** for high-materiality items and for substantiation-required
  categories (e.g. travel/meals without a receipt) — these require an explicit
  human confirmation and a memo.

You set where those lines fall. Nothing forces a human into the loop for routine
bookkeeping; the guards exist for the cases that actually warrant a second look.

## The guardrails that make autonomy safe

Autonomy is paired with hard, auditable guards:

- **Append-only ledger with a hash chain.** Postings are immutable; corrections
  are *new* reversing entries, and `verify_chain` proves the history wasn't
  altered.
- **Provenance-or-nothing.** The software refuses to fabricate a figure it can't
  trace to a source — it returns an error rather than inventing a number.
- **Draft / "do not file" watermarks** on every generated tax document.
- **No transmission, no money movement.** In the Community Edition there is no
  e-file (MeF), no state-portal submission, no EFTPS, no e-sign, and no
  trades/transfers/payments on your behalf (see [DISCLAIMER.md](../DISCLAIMER.md)).

## CPA review is optional — and encouraged where it counts

For anything with legal or financial consequence — a tax return, a filing with a
government agency, financial statements a third party will rely on — have a
qualified, licensed professional review it. Accounting-Ops makes that review
*cheaper and faster* by producing clean, sourced, verifiable work — but it does
not replace the professional's judgment or their license. The choice to involve
one is yours.

**Your books. Your machine. Your rules.**
