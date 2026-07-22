# Privacy Policy — Accounting-Ops (Hosted)

> # ⚠️ UNREVIEWED DRAFT — NOT IN FORCE
>
> **This document has not been reviewed by a lawyer. It is not published, not
> linked from the product, and does not describe any commitment currently made
> to any customer.** It is a drafting input for counsel. Search for `[COUNSEL:`
> to find every open question.
>
> Draft date: 2026-07-20 · Status: awaiting legal review

---

This describes what the **hosted** Accounting-Ops service does with your
information. The self-hosted Community Edition runs on your own hardware and
sends us nothing — this policy does not apply to it.

We have tried to write this honestly rather than defensively. Where the answer
is uncomfortable — we handle bank credentials, we send some of your financial
data to an AI vendor, our deletion is workspace-level rather than row-level —
we have said so plainly instead of burying it.

---

## 1. The short version

- We hold your **bank access credentials**, your **financial records**, and your
  **tax identifiers**. This is sensitive data and we treat it that way.
- Credentials are **encrypted at rest** with AES-256-GCM under a master key held
  outside the database.
- Your workspace is **isolated from every other workspace** by database-enforced
  row-level security that defaults to showing nothing.
- **Some of your financial data is sent to a third-party AI provider** when you
  use the chat and agent features. Receipt scanning and transaction
  categorization, by contrast, run on our own hardware. Section 4 explains
  exactly which is which, because the difference matters.
- You can **export everything** yourself, at any time.
- You can **ask us to delete everything**. Section 8 explains how that works and
  where its limits are.
- **We do not sell your data.** We do not share it for advertising. There is no
  advertising or analytics tracking in the product.

## 2. Who we are

Magic Unicorn LLC. `[PLACEHOLDER — registered address, contact email, and, if
required, a privacy contact or DPO]`

> `[COUNSEL: Confirm the contracting entity (same open question as the Terms).
> Advise whether a DPO or an EU/UK representative is required — that depends on
> whether we accept EU/UK customers, which is a go-to-market decision that should
> be made deliberately rather than by accident when the first EU signup arrives.]`

## 3. What we collect, and why

### 3.1 Account and identity
Name, email address, and authentication identifiers, held in our identity system.
Used to sign you in, control who can see which workspace, and contact you about
the service.

### 3.2 Financial records — the core of the product
- Transactions pulled from accounts you connect (date, amount, description,
  payee, account, balances).
- The double-entry general ledger built from them: journal entries, chart of
  accounts, trial balances, period closes.
- Documents you upload: receipts, invoices, tax forms, IRS notices, statements.
- Work products the service generates: financial statements, memos, filled tax
  forms.

Used to do the bookkeeping and prepare the forms. That is the service.

### 3.3 Tax identifiers
Entity tax identification numbers (EIN, and SSN where an entity is a sole
proprietorship or an individual filer). **Stored encrypted.** Needed to fill in
tax forms — a return without a TIN is not a return.

### 3.4 Bank and connector credentials
Access tokens and keys for the accounts you connect — SimpleFIN bank access
URLs, Stripe restricted keys, eBay OAuth tokens. **Stored encrypted, never in
plaintext, never in logs.** These are the most sensitive items we hold: a bank
access token is live access to your financial data. Section 6 covers how they
are protected and section 8 covers why they are the first thing we destroy.

### 3.5 Usage and operational data
Log entries, error records, request metadata, and an audit trail of actions
taken in your workspace — including actions taken by AI agents, which we record
so you can see what the software did and why. Used to run the service, debug it,
and give you an auditable history.

### 3.6 Payment information
`[PLACEHOLDER — depends on the payment processor chosen for the hosted offering.
If we use Stripe for our own billing, card data goes to Stripe and we hold only
the last four digits and a token. Confirm and add Stripe-as-our-processor to the
subprocessor table in §4, which is a separate role from the customer-connected
Stripe integration.]`

> `[COUNSEL: Note the two distinct Stripe relationships — (1) customers connect
> *their* Stripe account so we can read *their* revenue, which is a
> customer-directed integration; (2) we may use Stripe to bill customers, which
> makes Stripe our own processor. These have different legal characters and both
> need to appear, separately, in §4.]`

### 3.7 What we do NOT collect
No advertising identifiers. No third-party analytics. No session-replay or
behavioural tracking. There are no third-party SaaS SDKs in the web front end,
and no browser-side telemetry. Application-level tracing exists but is disabled
by default and, when enabled, goes to infrastructure we operate.

## 4. Who we send your data to — subprocessors

This is the section most privacy policies are vague about. We have tried not to
be.

### 4.1 The AI question, answered directly

There are **two separate AI paths** in the product and they behave differently.
If you read only one thing in this policy, read this.

**Runs on hardware we operate — your data does not leave our infrastructure:**
- **Receipt and document scanning (OCR/vision).** Uploaded receipts, invoices,
  and tax documents are read by a vision model on our own GPUs.
- **Transaction categorization.** Bank transaction descriptions are categorized
  by a model on our own GPUs.

**Sent to a third-party AI provider:**
- **Chat, the CFO agent, and the tax specialist agents.** When you ask a
  question in plain language, your question and the ledger data needed to answer
  it — journal entries, transactions, profit-and-loss figures, balance sheet
  figures, trial balances, tax review items — are sent to a third-party model
  API to generate the answer.

So: **your receipts and your categorization stay in-house; your conversations
with the assistant, and the financial figures those conversations touch, do
not.** We would rather state this precisely than claim a blanket "your data
never leaves" that is not true, or a blanket "we use AI vendors" that
understates how much stays local.

> `[COUNSEL: This is very likely the highest-risk item in this policy, for two
> reasons.
>
> First, IRC §7216 criminally restricts a return preparer's disclosure or use of
> "tax return information", with narrow exceptions and specific consent-form
> requirements (Treas. Reg. §301.7216-3, and Rev. Proc. 2013-14 governing the
> form of consent). If §7216 applies to us — which turns on the preparer question
> flagged in Terms §2.2 — then sending ledger and tax content to a third-party
> model API may require prior written consent in a prescribed format, and a
> general privacy-policy disclosure would NOT be sufficient. Please advise
> urgently, because if consent is required the product needs a consent flow
> before launch, not a policy paragraph.
>
> Second, state financial-privacy and biometric/AI-disclosure laws may impose
> their own requirements. And GLBA may apply to us as a service provider to
> financial institutions or by virtue of handling consumer financial data —
> please determine whether the GLBA Safeguards Rule applies, because it carries
> concrete security-program obligations, not just disclosure ones.
>
> If the answer to any of this is restrictive, the engineering fallback exists:
> the codebase already supports routing chat inference to self-hosted models, and
> supports per-workspace customer-supplied API keys. Turning that on is a
> configuration change, not a rebuild. Please tell us if we should.]`

### 4.2 Subprocessors we use by default

| Subprocessor | What it does | What it receives |
|---|---|---|
| **OpenRouter, Inc.** | Routes AI inference for chat and the agent features | Your questions, plus the ledger, transaction, and tax figures needed to answer them; entity names |
| **Anthropic, PBC** | Provides the underlying model reached through OpenRouter | Same as above |
| **Cloudflare, Inc.** | CDN, TLS termination, and web application firewall in front of the service | All web traffic to the service, including request contents in transit |

### 4.3 Services operated by us or our affiliates

These are run by us or by affiliated Unicorn Commander entities, not by
independent vendors, but your data reaches them so they are listed.

| Service | What it does | What it receives |
|---|---|---|
| **Identity (Keycloak / uchub SSO)** | Sign-in and session management | Identity and authentication data |
| **Unicorn Brigade** | Cross-application agent tool access | Tool call arguments and your access token, when a cross-application tool is invoked |
| **Object storage (Garage)** | Stores your uploaded documents and generated PDFs | Documents, receipts, and generated forms |
| **Database (PostgreSQL)** | Stores your ledger and records | All financial records |
| **AI inference (self-hosted models)** | Receipt scanning and transaction categorization | Document images, transaction descriptions |
| **Outbound email** | Notifications and account email | Your email address and message contents |
| **Usage metering** | Measures AI usage for billing, when enabled | Model, token counts, cost, and workspace identifier — **not** message contents |

> `[COUNSEL: Two things.
>
> (1) Whether these are "subprocessors" for GDPR/CCPA purposes depends on whether
> the affiliated entities are the same legal person as the contracting entity.
> Please determine the corporate structure and tell us which of these rows must be
> disclosed as subprocessors and which are internal. We have listed them all,
> which is over-disclosure rather than under-disclosure — that seems the right
> error to make, but confirm.
>
> (2) Flagging one honestly: **Unicorn Brigade is effectively on by default** in
> the hosted configuration, via a fallback in the code that most readers of the
> configuration file would not notice. Engineering's recommendation is either to
> disclose it as we have done here, or to explicitly disable it before launch and
> remove the row. It should not be left in the current state, where it is on but
> looks off.]`

### 4.4 Services you choose to connect

None of these receive anything unless you connect them. Each one is your choice
and you can disconnect it at any time.

| Service | What it does | What it receives |
|---|---|---|
| **SimpleFIN Bridge** | Pulls your bank transactions and balances | Requests authenticated with the access token you provide |
| **Stripe** | Reads your Stripe revenue, charges, refunds, and payouts | Your restricted API key and read requests |
| **eBay** | Reads your eBay sales and finances | Your OAuth token and read requests |
| **USPS Web Tools** | Looks up a mail tracking number | The tracking number only — no personal or financial data |
| **E-signature provider** | Signature workflows, when configured | Signer email and document contents |
| **Your own AI provider** | If you supply your own model API key, inference goes to the provider you chose instead of ours | Whatever the chat feature would otherwise send to OpenRouter |

**SimpleFIN deserves a specific note:** it is a bank-data aggregator, so it sits
between us and your bank, and it has its own relationship with you and its own
privacy terms. Read them. When you disconnect it or we erase your workspace, the
token we hold is destroyed — but that does not by itself terminate any
relationship you have directly with SimpleFIN or with your bank.

### 4.5 Others
We may disclose information if legally required (subpoena, court order, lawful
government request), to enforce our terms, to protect against fraud or a
security threat, or in connection with a merger or acquisition — in which case
we will tell you.

> `[COUNSEL: Do we commit to notifying the customer of a government data request
> before complying, where we are legally permitted to? This is a meaningful trust
> signal for an accounting product and is commonly offered. Recommend yes, with
> the standard "unless legally prohibited" carve-out — but it is your call.]`

### 4.6 How this list was verified, and how to re-verify it

This list was built by reading the deployment configuration, the application
settings module, and every connector module in the source — not from a template
and not from memory. Specifically: the container and service definitions, the
settings defaults, and each integration client.

**It will drift.** Before this policy is published, and whenever an integration
is added, re-verify against the running production configuration rather than
against the defaults in the source, because deployment environment variables can
override any default.

One known sharp edge for whoever re-verifies: the text-embedding path falls back
to a third-party provider if its self-hosted endpoint setting is ever cleared.
It is set correctly today, so the fallback does not trigger, but a configuration
change could silently route content to a third party without an error. That
should be turned into a hard failure rather than a silent fallback.

## 5. Where data is processed

`[PLACEHOLDER — hosting locations]`

> `[COUNSEL: Needed once the production footprint is confirmed. If we accept
> EU/UK customers, this drives the transfer-mechanism question in the DPA. Note
> our AI subprocessors are US-based, so EU customer data reaching them is a
> restricted transfer requiring a lawful mechanism — this may be a launch blocker
> for EU sales and should be decided before, not after, we take EU money.]`

## 6. How we protect it

**Encryption of credentials at rest.** Connector credentials are encrypted with
AES-256-GCM. The master key is stored outside the database, so a database
compromise alone does not yield usable bank credentials. Tax identifiers are
likewise stored encrypted.

**Tenant isolation enforced by the database.** Every record carries a workspace
identifier, and PostgreSQL row-level security is applied in *forced* mode —
meaning it applies even to the database owner. A query that fails to declare
which workspace it is acting for returns **zero rows** rather than everything.
The failure mode is "see nothing", not "see everything". This is the single most
important control we have, and it is enforced by the database rather than by
application code remembering to add a filter.

**Access control.** Roles from viewer through owner, enforced on every endpoint.
Sensitive operations require elevated roles. Entity-level scoping restricts a
user to named entities within a workspace.

**Tamper-evident records.** The general ledger is append-only and hash-chained.
Database triggers reject any attempt to modify or delete a posted journal entry
— for every database role, including the owner. Corrections are made by posting
a reversing entry, so the history stays intact and visible. Agent actions are
recorded in an append-only audit trail.

**Credentials in transit.** TLS everywhere. Credentials are never written to
logs. There is a separate access log recording each use of a stored credential.

**No plaintext credential fallback.** The application will not fall back to
reading credentials from environment variables — a class of cross-tenant leak
that is specifically tested against.

> `[COUNSEL: Every statement in this section is a factual claim about the system
> that a regulator or a plaintiff could test in discovery. They were accurate at
> drafting. Please advise on how much specificity to publish — detail builds
> trust with accounting-firm buyers who will run a security questionnaire, but it
> also creates a documented standard we must continuously meet. Recommend keeping
> the substance and adding "as of" language, plus an internal commitment to review
> this section on a schedule.]`

**What we do not claim:** we have no SOC 2 report, no ISO 27001 certification,
and no independent security audit to point to. Saying so seems better than
implying otherwise.

> `[COUNSEL / BUSINESS: Accounting firms will ask for SOC 2 in procurement. Worth
> deciding now whether that is on the roadmap, because the answer affects what we
> can honestly say here and which customers we can close.]`

## 7. How long we keep it

**While your account is active**, we keep your data so the service works. Books
and tax records are inherently historical — a ledger that quietly deleted last
year would be worthless, and your own tax record-retention obligations typically
run for several years.

**Default retention after your workspace is closed or erasure is requested:**

| Data | Default |
|---|---|
| Connector credentials (bank tokens, API keys) | Destroyed **immediately** on request — no waiting period |
| Stored documents and receipts | Destroyed **immediately** on request |
| Tax identifiers | Destroyed **immediately** on request |
| Access (memberships, sessions) | Revoked **immediately** on request |
| Ledger, journal entries, financial records | Retained for a default window, then purged as a unit — see §8 |
| Erasure record (proof we honored your request) | Retained after the purge — see §8.4 |
| Billing records | `[PLACEHOLDER — as required by tax and accounting law]` |

The ledger retention window is configurable per workspace, within limits, and the
current default is documented with the implementation. The reason there is a
window at all — rather than immediate destruction — is that these are accounting
records: customers routinely need them back after an unexpected audit notice, and
destroying them the instant someone clicks a button has burned real businesses.
**If you want immediate ledger destruction, you can ask for it and we will honor
it,** subject only to anything we are legally required to keep.

> `[COUNSEL: Please set the default retention window and confirm it is compatible
> with (a) any record-keeping obligation we have as a service provider, (b) the
> GDPR storage-limitation principle if EU customers are in scope, and (c) the
> "delete without undue delay" expectation in GDPR Art. 17 and CCPA. There is
> genuine tension between a customer's right to erasure and the practical value of
> a retention window, and engineering should not be the one resolving it. The
> default currently coded is a placeholder chosen to be conservative; tell us what
> it should be.]`

## 8. Your rights over your data

### 8.1 Export — self-service
A workspace owner can export **everything** at any time, without asking us:
journal entries, transactions, entities, chart of accounts, documents and
receipts (the actual files, not just references), work products, and audit
records. Machine-readable JSON and CSV, plus the stored files.

The export is scoped to your workspace and no one else's, and it requires owner
role. Large exports run in the background and you poll for completion.

### 8.2 Access and correction
You can see your data in the product. Ledger corrections are made by posting a
reversing entry rather than by editing history — the correction shows, and so
does what it corrected. That is deliberate: silently rewriting an accounting
record is exactly what an audit trail exists to prevent.

### 8.3 Deletion — and its honest limits

**What happens immediately when a workspace owner requests erasure:**

1. All access is revoked. The workspace is suspended and no user or agent can
   reach it.
2. **All connector credentials are destroyed.** Bank access tokens, Stripe keys,
   eBay tokens — gone. This is first because it is the item whose continued
   existence carries the most risk to you.
3. **Tax identifiers are destroyed** — the encrypted values are erased.
4. **All stored documents and receipts are deleted** from object storage.

**What happens after that:** the ledger and financial records are purged as a
unit, either at the end of the retention window or immediately if you ask for
immediate purge.

**Why deletion is workspace-level and not row-level — the honest explanation:**

Your general ledger is append-only and hash-chained. Each entry's hash covers
its own contents, including the payee and description, and links to the previous
entry. That chain is what makes your books tamper-evident, and it is a feature
you are paying for.

Deleting or redacting a single entry from a live ledger would break that chain
at that point, permanently. The verifier would report your books as tampered —
correctly, because they would have been. We would be destroying the integrity of
the records you kept, in order to honor a request about records you asked us to
remove. We do not think that is a good trade, and we do not think you would
either.

So we erase at the level of the whole workspace, as a unit. Every entry goes
together, the chain goes with it, and nothing is left in an inconsistent state.
Because each entity's chain is independent, erasing your workspace has no effect
whatsoever on any other customer's records — that property is verified by an
automated test.

**The practical consequence for you:** we cannot delete "just this one
transaction" from a posted ledger while keeping the rest. We can post a
correcting entry, which is the accounting-correct answer, and we can erase the
whole workspace. If you need something in between, contact us and we will tell
you honestly whether it is possible rather than quietly doing something that
looks like it worked.

### 8.4 What survives erasure, and why
After a purge we keep a **record that the erasure happened** — who requested it,
when, what was destroyed, and what was retained. We keep it deliberately: it is
the only evidence that we honored your request, and it is what we would show a
regulator asking whether we did. It contains no financial records and no
documents.

We may also keep anything we are legally required to keep, such as billing
records for tax purposes.

### 8.5 Rights under GDPR, CCPA, and similar laws
Depending on where you are, you may have rights to access, correct, delete,
port, restrict, or object to processing, and to not be discriminated against for
exercising them.

> `[COUNSEL: This section needs real drafting once we know which regimes apply,
> which is a function of who we sell to. It must specify: how to make a request,
> our response deadline, identity verification, the appeal or complaint route,
> whether we honor Global Privacy Control signals, and — for CCPA — the specific
> "categories collected / disclosed / sold" table in the statutory format. We do
> not sell data, which simplifies the CCPA position considerably.
>
> One item needs a product decision, not just drafting: our export and erasure are
> currently **owner-role, workspace-level** operations. Where our customer is an
> accounting firm and the data subject is that firm's client, the individual
> cannot exercise rights through our product at all — they must go through our
> customer, who is the controller. That is the legally correct architecture and it
> matches the DPA, but the policy should state it plainly so nobody is surprised.
> Please confirm and draft the wording.]`

`[PLACEHOLDER — request mechanism, response deadline, verification, appeals]`

### 8.6 How to make a request
`[PLACEHOLDER — email address and/or in-product route]`

## 9. Children
The service is for business and tax use and is not directed at children. We do
not knowingly collect data from anyone under `[PLACEHOLDER — 13/16/18]`.

## 10. Changes
We will post material changes and give notice `[PLACEHOLDER — mechanism and
period]` before they take effect. `[COUNSEL: for material changes affecting
existing customers, is notice sufficient or is consent needed? This may differ
between the privacy policy and the DPA.]`

## 11. Contact
`[PLACEHOLDER — privacy contact]`

---

## Appendix — verification note for counsel

The subprocessor list in §4 and the security claims in §6 are statements of
engineering fact, verified against the source at the time of drafting by reading
the deployment configuration, the settings module, and each connector module
directly.

Two things counsel should know because they affect the legal analysis, not just
the drafting:

1. **The split in §4.1 is real and unusual.** Most AI products send everything
   to a vendor. This one genuinely runs OCR and categorization on its own
   hardware and only sends the chat/agent path out. That is a meaningfully
   better privacy position than the category norm and it is worth preserving —
   if the §7216 analysis is unfavourable, the engineering fix is to move the
   chat path in-house too, which the code already supports.

2. **The claims in §6 are testable.** Tenant isolation, the append-only
   triggers, and the no-plaintext-credential-fallback behaviour all have
   automated tests. If a security questionnaire asks for evidence, it exists.
