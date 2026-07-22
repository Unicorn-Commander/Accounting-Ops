# Data Processing Agreement — Accounting-Ops (Hosted)

> # ⚠️ UNREVIEWED DRAFT — NOT IN FORCE
>
> **This document has not been reviewed by a lawyer. It is not published, not
> linked from the product, not offered to any customer, and does not bind anyone.**
> It is a drafting input for counsel. Search for `[COUNSEL:` to find every open
> question.
>
> A DPA is a document that gets negotiated. This draft is a starting position,
> not a final one, and several clauses are deliberately left open because they
> depend on decisions nobody has made yet.
>
> Draft date: 2026-07-20 · Status: awaiting legal review

---

**Processor:** Magic Unicorn LLC
**Controller:** the customer identified in the Terms of Service
**Applies to:** the hosted Accounting-Ops service

This forms part of the Terms of Service. Where they conflict on the processing
of personal data, this document wins.

> `[COUNSEL: Confirm entity, and confirm this incorporation-by-reference structure
> is how we want to do it. Alternative is a separate signed DPA on request, which
> larger accounting-firm customers will likely demand anyway. Recommend building
> for both: auto-incorporated by default, signable on request.]`

---

## 1. Who is the controller — this is not a formality here

Accounting-Ops has two quite different customers, and they sit differently.

**1.1 — A business using it for its own books.** The customer is the controller
of the personal data in its records — its vendors, its customers, its employees.
We are the processor.

**1.2 — An accountant or bookkeeper using it for their clients.** The customer
is the controller (or is itself a processor for its clients, making us a
sub-processor). Either way, **we act only on our customer's instructions and we
have no direct relationship with their clients.** The product explicitly
supports this: one firm, many client workspaces.

This matters practically. If an accountant's client wants their data exported or
erased, **they must go to the accountant, not to us.** Our export and erasure
functions are workspace-scoped and require the workspace owner's role. We will
not act on an instruction from someone who is not our customer, because we
cannot verify their authority and because acting would breach our duty to our
actual customer.

> `[COUNSEL: Please confirm the characterization in 1.2 — specifically whether we
> are a processor or a sub-processor when our customer is itself a processor for
> its clients, and what that changes about our obligations and about the
> flow-down terms our customer needs from us. Accounting firms will ask for
> assurances they can pass to their own clients; we should know what we can
> offer.]`

**1.3 — Where we are the controller.** For the personal data of the customer's
own account users — names, emails, sign-in records, billing details — we are the
controller, and the Privacy Policy governs. This document covers the customer
data we process on the customer's behalf.

## 2. What we process

**Subject matter:** providing AI-assisted bookkeeping and tax-form preparation.

**Duration:** the term of the customer's subscription, plus the retention period
in section 8.

**Nature and purpose:** ingesting financial transactions; categorizing them;
maintaining a double-entry, append-only, hash-chained general ledger; producing
financial statements; preparing tax-form PDFs; storing uploaded documents;
answering questions about the customer's records.

**Categories of data subjects:** the customer's own personnel and account users;
its vendors, suppliers, customers, and payees; where the customer is an
accountant, its clients and those clients' counterparties.

**Categories of personal data:**
- identifying data — names, business names, email addresses, addresses;
- financial data — transaction records, amounts, payees, account balances,
  ledger entries, invoices, payment records;
- **tax identification numbers** — EIN and, for sole proprietors and individual
  filers, SSN;
- document contents — receipts, invoices, tax forms, statements, notices, which
  may contain any of the above;
- **credentials** — access tokens for the financial accounts the customer
  connects.

> `[COUNSEL: Two flags.
>
> (1) SSNs and financial account data are "sensitive" or specially-protected under
> several regimes (state law definitions, GLBA, and GDPR to the extent EU data
> subjects appear). Please advise whether that triggers additional obligations —
> a DPIA under GDPR Art. 35 seems likely if EU customers are in scope, given
> large-scale processing of financial data with automated processing.
>
> (2) Documents are freeform. A customer can upload anything, and in practice
> accounting documents sometimes contain health information (medical expense
> receipts), which would be special-category data. The product has a
> `hipaa_protected` workspace flag, which suggests someone anticipated this.
> Please advise whether we need to say something about it here, or restrict it in
> the Terms, or both.]`

## 3. Our obligations

We will:

1. Process personal data **only on the customer's documented instructions** —
   which include using the service as intended, and any configuration the
   customer sets. If we believe an instruction breaks the law, we will tell the
   customer and may pause that processing.
2. Ensure our personnel who access personal data are bound by confidentiality.
3. Apply the security measures in section 5.
4. Not engage a subprocessor except as section 4 allows.
5. Help the customer respond to data subject requests, as section 6 describes.
6. Help the customer with security, breach notification, impact assessments, and
   regulator consultation, taking into account what we know and what the service
   can do.
7. Delete or return personal data as section 8 describes.
8. Make available the information reasonably needed to show we comply, and allow
   audits on the terms in section 9.
9. **Not sell personal data, and not use it for our own purposes** — including
   not using customer data to train AI models.

> `[COUNSEL: Item 9's training commitment appears in the Terms too and carries
> the same caveat: it is only as strong as the contractual terms in force on our
> AI vendor accounts. Confirm those terms before this is published, and hold the
> evidence. If we cannot substantiate it, this must come out — a false statement
> here is materially worse than its absence, since it is a contractual warranty
> rather than a marketing claim.]`

## 4. Subprocessors

**4.1 — General authorization.** The customer authorizes us to use the
subprocessors listed in 4.4, and to appoint others subject to 4.2.

**4.2 — Change notice.** We will give the customer at least
**`[PLACEHOLDER — recommend 30]` days' notice** before a new subprocessor starts
processing, via `[PLACEHOLDER — mechanism: email to the account owner, a
subscribable change page, or both]`.

**4.3 — Objection.** The customer may object on reasonable data-protection
grounds within the notice period. We will work in good faith to find a
resolution; if we cannot, the customer may terminate the affected part of the
service without penalty and receive a pro-rata refund of prepaid fees.

> `[COUNSEL: 4.3 is the customer-favourable version and it is the one that gets
> asked for. The commercial risk is real but narrow — the AI subprocessors are
> the ones most likely to change and most likely to be objected to. Advise on
> whether to offer this as drafted, and note that a customer terminating over an
> AI subprocessor could instead be offered the self-hosted-inference
> configuration or bring-your-own-key, both of which the product already
> supports. That may be a better answer than a refund.]`

**4.4 — Current subprocessors.**

*Default — used for every hosted customer:*

| Subprocessor | Purpose | Data processed |
|---|---|---|
| OpenRouter, Inc. | AI inference routing for chat and agent features | Queries plus the ledger, transaction, and tax figures needed to answer them |
| Anthropic, PBC | Underlying model, reached via OpenRouter | As above |
| Cloudflare, Inc. | CDN, TLS termination, WAF | All traffic in transit |

*Affiliates — operated by us or by affiliated entities:*

| Service | Purpose | Data processed |
|---|---|---|
| Identity (Keycloak / uchub) | Authentication | Identity and session data |
| Unicorn Brigade | Cross-application agent tooling | Tool arguments and access tokens |
| Object storage (Garage) | Document storage | Uploaded documents, generated PDFs |
| Database (PostgreSQL) | Primary record store | All records |
| Self-hosted AI models | Receipt OCR, transaction categorization | Document images, transaction descriptions |
| Outbound email | Notifications | Email addresses, message contents |

*Customer-connected — only if the customer connects them:*

| Service | Purpose |
|---|---|
| SimpleFIN Bridge | Bank transaction retrieval |
| Stripe | Reading the customer's own Stripe revenue |
| eBay | Reading the customer's own eBay finances |
| USPS Web Tools | Mail tracking lookup (tracking number only) |
| E-signature provider | Signature workflows, when configured |
| Customer's own AI provider | Inference under the customer's own API key |

> `[COUNSEL: Same two questions as the Privacy Policy. (1) Which affiliate rows
> are legally subprocessors depends on corporate structure — please determine.
> (2) The customer-connected services are arguably not our subprocessors at all,
> since the customer directs the connection and holds the relationship. Advise on
> whether to characterize them as subprocessors (conservative, over-discloses, but
> triggers our 4.2 notice obligation for things outside our control) or as
> customer-directed transfers (more accurate, but requires clear drafting so the
> customer understands they own that relationship). Engineering's view is that the
> second is more honest, but it is a legal call.]`

**4.5** We remain liable for our subprocessors' performance of these obligations.

## 5. Security measures

We implement appropriate technical and organizational measures, including:

**Encryption**
- Connector credentials encrypted at rest with AES-256-GCM, master key held
  outside the database.
- Tax identification numbers encrypted at rest.
- TLS in transit.

**Tenant isolation**
- Every record carries a workspace identifier.
- PostgreSQL row-level security in **forced** mode — applying even to the
  database owner — restricts access to the workspace declared for the
  transaction.
- A query that does not declare a workspace returns **zero rows**. The failure
  mode is denial, not disclosure.

**Access control**
- Role-based access from viewer through owner, enforced per endpoint.
- Entity-level scoping within a workspace.
- Sensitive operations restricted to elevated roles; destructive operations
  restricted to the owner and requiring explicit confirmation.

**Integrity and auditability**
- Append-only, hash-chained general ledger. Database triggers reject
  modification or deletion of posted entries for every database role, including
  the owner.
- Corrections are made by reversing entry, never by overwriting.
- Append-only audit trail of agent and user actions, including a separate access
  log for each use of a stored credential.

**Operational**
- Credentials never written to logs.
- No fallback to plaintext credentials from environment variables — specifically
  tested against.
- `[PLACEHOLDER — backup, disaster recovery, vulnerability management, patching,
  personnel security, access review cadence, incident response testing]`

> `[COUNSEL: The placeholders are genuine gaps, not drafting laziness. Before this
> is signed with anyone, someone must document the actual backup, DR, and
> vulnerability-management practices. A DPA that describes controls we do not
> operate is worse than one that says less. Please tell us the minimum set a
> customer will require so engineering can build and document it.
>
> Note also: we have no SOC 2 or ISO 27001. Larger accounting firms will ask.]`

## 6. Data subject requests

**6.1** If we receive a request directly from a data subject about data we
process for the customer, we will not respond substantively. We will redirect
them to the customer and inform the customer, unless legally required to do
otherwise.

**6.2** We will help the customer respond, through:

- **Self-service export.** A workspace owner can export the workspace's data at
  any time — records, documents, and files, in machine-readable form. Most access
  and portability requests can be satisfied by the customer without contacting us.
- **Self-service erasure.** A workspace owner can request erasure of a workspace,
  which immediately revokes access and destroys credentials, tax identifiers, and
  stored documents, and purges the financial records as described in section 8.
- **Correction.** Ledger corrections are made by reversing entry, preserving the
  audit trail.

**6.3 — A limitation the customer needs to understand.** Our erasure operates at
the **workspace level**, not the individual-record level. Because the ledger is
append-only and hash-chained, removing individual entries from a live ledger
would break the integrity guarantee protecting the customer's remaining records.

If the customer must erase one data subject's personal data from within a live
ledger — for example, a single vendor's details across many entries — **we cannot
do that today**, and the customer should factor this into its own compliance
analysis before relying on our service for that purpose. We can post correcting
entries, and we can erase an entire workspace.

> `[COUNSEL: This is the most legally significant limitation in the whole product
> and it needs your judgment, not ours.
>
> The question: is workspace-level-only erasure defensible under GDPR Art. 17 and
> CCPA? Arguments in favour: Art. 17(3)(b) and (e) exemptions; the integrity of
> accounting records is a legitimate and arguably legally-required interest; the
> customer's own tax record-retention obligations independently require keeping
> the ledger; and the data at issue is business-transaction data rather than
> profile data. Arguments against: "our architecture makes it hard" is not a
> recognised exemption, and a regulator may not accept it.
>
> If it is NOT defensible, engineering needs to know, because the fix is
> substantial: it would mean crypto-shredding — storing personal fields by
> reference to a per-subject key that can be destroyed, leaving the hash chain
> intact over opaque tokens. That is a real design and it would work, but it
> requires a ledger format change and re-verification of historical entries. It
> is months, not days. Please advise early rather than late.
>
> Whatever the answer, section 6.3 must be stated plainly to customers rather
> than discovered by them during their own audit.]`

**6.4** We may charge for assistance that is excessive or repetitive.
`[COUNSEL: confirm this is permitted for a processor.]`

## 7. Personal data breach

**7.1** We will notify the customer **without undue delay** and in any case
within **`[PLACEHOLDER — recommend 48 or 72 hours]`** of becoming aware of a
breach affecting their personal data.

**7.2** The notification will describe, so far as known: the nature of the
breach, the categories and approximate number of data subjects and records, the
likely consequences, the measures taken or proposed, and a contact point. We
will supplement as we learn more rather than delaying the first notification
until everything is known.

**7.3** We will not make a public statement identifying the customer without
consulting them first, unless legally required.

> `[COUNSEL: Set the deadline. 72 hours mirrors GDPR Art. 33 but note that is the
> *controller's* deadline to the regulator — the processor's obligation is
> "without undue delay", and a customer needs time to act within their own 72
> hours. Customers therefore commonly demand 24 or 48. Advise on what we can
> actually meet given our monitoring, and do not commit to a number operations
> cannot hit. Also confirm whether GLBA or state breach-notification statutes
> impose shorter or additional obligations here.]`

## 8. Deletion and return

**8.1 — During the term.** The customer can export at any time, self-service.

**8.2 — On termination.** For **`[PLACEHOLDER — recommend at least 30]` days**
after termination, the customer can request a full export.

**8.3 — Erasure.** After the export window, or on the customer's earlier
instruction, we erase, in this order:

1. **Immediately:** revoke all access; destroy all connector credentials;
   destroy tax identifiers; delete all stored documents and receipts from object
   storage.
2. **Then:** purge the workspace's financial records as a unit — either at the
   end of the retention period in 8.4, or immediately on the customer's explicit
   instruction.

The order is deliberate. Credentials are destroyed first because a live bank
access token is the item whose continued existence carries the most risk.

**8.4 — Retention period.** `[PLACEHOLDER — default retention window]`

The default is configurable per workspace within limits. It exists because these
are accounting records and customers regularly need them back after an
unexpected audit notice. **A customer can instruct immediate purge and we will
honor it.**

> `[COUNSEL: Set this. See the equivalent note in the Privacy Policy — the same
> number should appear in both, and the tension between erasure rights and the
> practical value of a retention window needs a legal answer.]`

**8.5 — What survives.** After purge we retain a record that the erasure
occurred — who requested it, when, what was destroyed, what was retained. It
contains no financial records and no documents. We keep it as evidence that we
performed the erasure, which is what we would show a regulator or the customer
asking whether we did.

We also retain anything law requires us to keep, and will tell the customer what
that is on request.

**8.6 — Backups.** `[PLACEHOLDER]`

> `[COUNSEL: Backups are the standard hard case and this draft does not yet have
> an answer. Erased data persists in backups until they age out. The conventional
> clause says backups are overwritten on their normal cycle and remain protected
> and isolated in the meantime. Engineering must confirm the actual backup
> retention cycle before we state one — please ask for it and do not let this be
> filled in with a plausible-sounding number.]`

## 9. Audits

**9.1** We will provide the information reasonably necessary to demonstrate
compliance.

**9.2** `[PLACEHOLDER — audit rights: frequency, notice, cost, scope, whether a
third-party report satisfies the obligation]`

> `[COUNSEL: Draft this to be workable at our scale. An unrestricted on-site
> audit right granted to every customer is not operable for a company of this
> size. Usual approach: satisfy via a third-party report where one exists, a
> written questionnaire otherwise, on-site limited to once a year with notice
> and at the customer's cost, and only where legally required. We have no SOC 2,
> which weakens the "here is our report" answer — factor that in.]`

## 10. International transfers

`[PLACEHOLDER]`

> `[COUNSEL: Open until we decide whether to accept EU/UK customers. If we do,
> this needs: the EU SCCs with the right modules and annexes, the UK Addendum,
> a transfer impact assessment, and the Swiss position if relevant. Our AI
> subprocessors are US-based, so EU customer data reaching them is a restricted
> transfer requiring a mechanism.
>
> This is a launch-gating decision, and the honest engineering view is that it is
> cheaper to decide "no EU customers for now" deliberately than to discover the
> obligation after the first EU signup. Please advise on the cost of doing this
> properly so the business can make an informed choice.]`

## 11. Liability
Liability under this DPA is subject to the limitations in the Terms of Service.
`[COUNSEL: confirm this is enforceable for data-protection claims — some regimes
limit contractual caps on statutory liability, and customers often push back on
capping privacy claims at the general cap.]`

## 12. Term
This DPA lasts as long as we process personal data for the customer. Sections 8
and 11 survive.

---

## Annex A — technical and organizational measures
See section 5. `[COUNSEL: some customers require this as a separate signable
annex — consider extracting it.]`

## Annex B — subprocessors
See section 4.4. `[COUNSEL: consider publishing this as a versioned page with a
change feed customers can subscribe to. That is the cleanest way to satisfy the
4.2 notice obligation and it is what sophisticated customers expect.]`

---

## Appendix — verification note for counsel

The subprocessor list in 4.4 and the security measures in section 5 were
verified against the source at the time of drafting, by reading the deployment
configuration, the settings module, and each connector module directly. They
were not taken from a template.

The limitation described in 6.3 is a real architectural property, not a
temporary gap: database triggers reject deletion of posted ledger entries for
every database role including the owner, and the hash chain covers the payee and
description fields, so redaction in place would permanently fail integrity
verification. The rationale and the implementation are documented in
`docs/DATA-RIGHTS.md` and `app/data_rights/erasure.py`.
