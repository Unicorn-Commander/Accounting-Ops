<div align="center">

<h1>Accounting-Ops</h1>

### The first open-source autonomous AI accountant

**Your books. Your machine. Your rules.**

Autonomous agents keep your general ledger — categorizing transactions, reconciling receipts,
drafting statements, and assembling tax packages — on an append-only, hash-chained journal you can audit line by line.
It runs entirely on **your** hardware with **your** models. No cloud account. No telemetry. No black boxes in your books.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-8B5CF6?style=flat-square)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.8.0-8B5CF6?style=flat-square)](CHANGELOG.md)
[![Self-host](https://img.shields.io/badge/self--host-one%20command-22D3EE?style=flat-square)](#quickstart)
[![Telemetry](https://img.shields.io/badge/telemetry-none%20by%20default-34D399?style=flat-square)](#security--your-data)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-8B5CF6?style=flat-square)](CONTRIBUTING.md)
[![Bring your own model](https://img.shields.io/badge/models-bring%20your%20own-22D3EE?style=flat-square)](#bring-your-own-model)

**[Quickstart](#quickstart)&nbsp; · &nbsp;[Features](#what-it-does)&nbsp; · &nbsp;[Architecture](#architecture)&nbsp; · &nbsp;[Editions](#editions)&nbsp; · &nbsp;[Disclaimer](DISCLAIMER.md)&nbsp; · &nbsp;[Contributing](CONTRIBUTING.md)**

<br/>

<img src="docs/assets/hero-dashboard.png" alt="Accounting-Ops dashboard — net cash, a CFO brief of what needs you, and a live feed of agents categorizing, reconciling, and flagging transactions against your rules" width="100%" />

<sub><i>Shown on fictional demo data. Your real books never leave your machine.</i></sub>

</div>

---

> [!WARNING]
> **Not tax, legal, or accounting advice.** Accounting-Ops is software, not a licensed
> professional, and using it creates no professional relationship. You alone are responsible
> for anything you file, sign, or pay — verify every figure against your own records and
> consult a qualified CPA/EA/attorney for anything with legal or financial consequence.
> See **[DISCLAIMER.md](DISCLAIMER.md)**.

## Why it exists

Every AI bookkeeping product on the market asks you to ship your general ledger to somebody
else's cloud and trust a model you can't see. Accounting-Ops takes the opposite stance:

- **Sovereignty is the default.** Self-host on your own hardware, air-gapped if you need to.
  Your journal, your encryption keys, your egress policy. We never see your books and never will.
- **Agents draft, you approve.** Nothing posts to your ledger until either a deterministic rule
  clears it or a human signs off. No magic, no surprises — every draft cites its source and its policy.
- **Audit-defensible by construction.** The ledger is append-only and cryptographically chained.
  Every number traces back to the source document, the agent that drafted it, the rule that
  validated it, and the human who approved it.
- **Bring your own model.** Point it at a local Ollama, vLLM, an on-prem gateway, or a hosted API.
  The whole categorizer even runs with **no model at all** — Tier 1 is a deterministic rule engine.

We sit *above* the model providers and *below* the human reviewer: the substrate your agents
actually run on when the numbers have to be right.

## What it does

| Surface | What it does |
|---|---|
| **Dashboard** | Net cash + YTD Revenue / Expenses / Net Income from the journal, a CFO brief of what needs you, and a live feed of every agent decision |
| **Transactions** | Unified transaction stream with categorization, per-entity badges, and materiality |
| **Reconciliation** | Human-in-the-loop review — *Confirm* posts to the journal, *Reject* trains a category override |
| **Journal** | Paginated, per-entity SHA-256 verify-chain with double-entry detail, provenance, and reversal lineage |
| **Reports** | P&L / Balance Sheet / Cash Flow with period chips, rendered in-app — no external service |
| **Taxes** | Tax-knowledge Q&A + draft tax-form packages, watermarked **draft / do not file** |
| **CPA export** | CSV / IIF / QBO XML / PDF exports for your accountant (optional sidecar) |
| **Chat / ⌘K** | Ask anything over your live books; the agent answers with the numbers *and* their lineage |

### Trace any number back to its source

Click any figure and the provenance chain unrolls — report line → journal entries → source
transaction → the agent decision that reconciled it — ending in a verified `sha256` hash. If a
fact about your business can't be explained, it can't be posted.

<div align="center">
<img src="docs/assets/provenance-lineage.png" alt="Provenance popover tracing net cash through 1,284 hash-chained journal entries down to the source transaction and the agent decision, ending in a verified SHA-256 hash" width="88%" />
</div>

### Append-only journal with a hash chain

Every posted transaction becomes a `journal_entry` row in a per-entity SHA-256 hash chain.
Reversals create *new* entries (never overwrite). `verify_chain` walks the chain and reports
any tampered index.

```
prev_hash || canonical_payload(entry) -> sha256 = this_hash
```

A **materiality gate** runs at posting time: small auto-categorized items auto-post; mid-size
items queue for human *Confirm*; large items hard-block pending an explicit confirmation + memo;
travel/meals block without a matching receipt.

### Categorization is deterministic first

Tier 1 is a **deterministic vendor-rule engine** (`app/categorization/vendor_rules.py`) — fast,
auditable, and it covers the bulk of recurring transactions with **no model call**. A model is
only consulted as an optional Tier-2 fallback for what the rules leave `Uncategorized`, and those
are always queued for review rather than auto-posted. **You can run the whole categorizer with no
model at all.**

### Ask your books anything

<div align="center">
<img src="docs/assets/command-palette.png" alt="Command palette — asking what's my runway if the slow season hits, and the CFO agent answering from live books with monthly burn, cash on hand, and cash cushion" width="88%" />
</div>

---

## Quickstart

**Prerequisites:** Docker + Docker Compose v2, and (for the bootstrap helper) Python 3.12 and
`openssl` on the host. No GPU required — a CPU-only Ollama works, just slower.

### One command

```bash
git clone https://github.com/Unicorn-Commander/accounting-ops-community.git
cd accounting-ops-community
make selfhost
```

`make selfhost` (→ `scripts/selfhost_bootstrap.sh`) is idempotent and will:

1. create `.env` from `.env.example`;
2. generate strong secrets (Postgres, Garage, NextAuth) and the `AO_LOCAL_AUTH_SECRET`;
3. set `AO_EDITION=community` + `AO_AUTH_MODE=local`;
4. prompt you for a local admin password and store only its scrypt hash;
5. generate the credential master key (`secrets/accounting_ops_master_key`);
6. build + start the stack — **postgres + garage + ollama + app + web**;
7. run migrations, provision one workspace + entity, grant your admin the owner role, and seed a chart of accounts.

Then open **http://localhost:3000**, sign in with your admin email + password, and you're in.

Pull a vision model so the agent can categorize and read receipts:

```bash
docker compose -f docker-compose.selfhost.yml exec ollama ollama pull qwen2.5vl:7b
```

Add a manual journal entry (or import a bank CSV), open **Reports**, and render a P&L — all with
**no external fleet dependency**.

<details>
<summary><b>Manual setup</b> — what the bootstrap does under the hood</summary>

```bash
cp .env.example .env

# 1. Secrets — a long random value in each. AO_LOCAL_AUTH_SECRET must be the
#    SAME value the web and backend share:
#      POSTGRES_PASSWORD, GARAGE_RPC_SECRET, GARAGE_ADMIN_TOKEN,
#      NEXTAUTH_SECRET, AO_LOCAL_AUTH_SECRET

# 2. Edition + auth mode (already set for community in .env.example if you copy it)
#      AO_EDITION=community
#      AO_AUTH_MODE=local
#      AO_LOCAL_ADMIN_EMAIL=you@example.com
#      AO_LOCAL_ADMIN_UC_UID=admin

# 3. Admin password hash
python scripts/gen_local_admin_hash.py     # paste output into AO_LOCAL_ADMIN_PASSWORD_HASH

# 4. Credential master key
python -m scripts.bootstrap_master_key --path secrets/accounting_ops_master_key

# 5. Up
docker compose -f docker-compose.selfhost.yml up -d --build

# 6. Migrate + provision (inside the app container)
WID=$(python -c 'import uuid;print(uuid.uuid4())')
docker compose -f docker-compose.selfhost.yml exec -T app alembic -c alembic-public.ini upgrade head
docker compose -f docker-compose.selfhost.yml exec -T app python -m scripts.provision_workspace "$WID" "My Company" --entity "My Company:llc"
docker compose -f docker-compose.selfhost.yml exec -T app python -m scripts.grant_workspace_admin "$WID" admin --role owner
docker compose -f docker-compose.selfhost.yml exec -T app python -m scripts.seed_default_coa "$WID" my-company
# then set DEFAULT_WORKSPACE_ID + TENANT_ID to $WID in .env and restart app + web
```

</details>

### Bring your own model

The bundled **Ollama** service (`http://ollama:11434/v1`) is the default target for the
categorizer and receipt OCR. Set `LLM_ENDPOINT_URL` / `LLM_MODEL` in `.env` to point anywhere
OpenAI-compatible — a remote Ollama, vLLM, an on-prem gateway, or a hosted API. Choose a **vision**
model for receipt reading.

### Optional services (compose profiles)

| Profile | Service | What it adds | Degrades gracefully? |
|---|---|---|---|
| `embeddings` | Infinity | embedding backend for the Tax-Knowledge RAG index | Yes — app runs fine without it |
| `graph` | FalkorDB | best-effort agent-memory projection | Yes — a no-op if absent |

```bash
docker compose -f docker-compose.selfhost.yml --profile embeddings --profile graph up -d
```

### Demo data (optional)

```bash
make demo    # seeds two generic sample workspaces (no PII)
```

---

## Architecture

```
You + external agents (Claude / Codex / MCP clients)
        │
        ▼
   web (Next.js 15)  ─────►  app (FastAPI)  ─────►  Postgres  (per-workspace schemas,
   local-auth Credentials             │             append-only hash-chained ledger)
                                       ├─► Garage    (S3-compatible blobs: receipts / docs)
                                       ├─► Ollama    (bring-your-own model, OpenAI-compatible)
                                       └─► [optional] Infinity embeddings · FalkorDB graph
```

- **MCP-first / agent-first.** The FastAPI app exposes an MCP surface (`/mcp`) and an A2A surface
  (`/a2a`) so agents operate the ledger the same way the UI does — no wrappers, no shadow API.
- **Deterministic where it matters.** Categorization Tier 1 and the double-entry posting engine
  are rule-based and model-free; models are an optional assist, never the system of record.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the full runtime picture, data flows, and
the hash-chain spec, and **[docs/AGENT-ACTIVITY-SPEC.md](docs/AGENT-ACTIVITY-SPEC.md)** for the
agent-activity event model.

### Multi-tenancy model — `Workspace > Entity > User`

- **Workspace** — the isolation boundary (a Postgres schema per workspace).
- **Entity** — a legal entity within a workspace (your LLC, an S-corp, a personal 1040).
  Row-scoped within the workspace schema.
- **User** — the auth subject (`uc_uid`). In Community / local mode this is your single admin.

---

## Editions

Accounting-Ops is **open-core**. The Community Edition in this repo is the full self-hostable
product; a couple of multi-tenant / suite surfaces are held for the Enterprise Edition and are
simply not mounted when `AO_EDITION=community`.

| | Community (this repo, AGPL) | Enterprise |
|---|---|---|
| Ledger, categorization, reports, CPA export | ✅ | ✅ |
| Agents (categorize / reconcile / CFO / tax specialists) | ✅ | ✅ |
| Auth | **Local single-admin** (`AO_AUTH_MODE=local`) | Keycloak / OIDC SSO |
| Bring-your-own models | ✅ (Ollama by default) | ✅ + shared gateway |
| Multi-client CPA console / cross-client work inbox | — | ✅ |
| Suite federation (cross-app entitlement, agent fabric) | — | ✅ |

### What the Community Edition does **not** do

To keep the free, self-hostable edition safe to run, these are **off and clearly labeled**
(see [DISCLAIMER.md](DISCLAIMER.md)):

- **No electronic filing.** It generates tax packages *for your own records*; it does **not**
  transmit to the IRS (MeF), a **state portal**, or any authority. Generated tax documents are
  watermarked **draft / "do not file."**
- **No tax payments.** No **EFTPS** connection; it never moves money.
- **No e-signature** submission of filings.
- **No trades, transfers, or payments** are executed on your behalf.

The tax-submission tracker only *records* that you sent a filing (paper / portal / e-file) — it
does not send anything.

---

## How it compares

- **Not Pilot or Bench.** Those are bookkeeping *services* with a portal. This is software you run.
- **Not QuickBooks or Xero.** Those are your CPA's tool. This is your *agents'* substrate.
- **Not Digits, Puzzle, or Truewind.** Those are closed SaaS with one model behind a logo. This is
  self-hostable, multi-runtime, and the rules are yours to read.
- **Not Plaid or Codat.** Those are pipes. This is the layer where the pipes turn into a defensible ledger.

---

## Security & your data

- **No telemetry by default.** The OTEL endpoint defaults to none; there is no analytics SDK.
- **Encrypted credentials.** Connector credentials are encrypted at rest with a local master key
  (`secrets/accounting_ops_master_key`). **Back it up out-of-band** — losing it orphans all
  encrypted credentials.
- **Least-privilege by construction.** The runtime app role is `NOSUPERUSER` / non-owner; the
  append-only journal is enforced in the schema, not just the app.
- Found a vulnerability? See **[SECURITY.md](SECURITY.md)**.

## The design stance

Accounting-Ops is built as an **autonomous professional-substitute you supervise** — not a
co-pilot that only suggests, and not a black box that just decides. It never fabricates provenance
(a missing source is an error, HTTP 422, not a guess), it watermarks anything unfiled as *draft*,
and it routes anything it isn't sure about to you. Read the full posture in
**[docs/PROFESSIONAL-SUBSTITUTE.md](docs/PROFESSIONAL-SUBSTITUTE.md)**.

## Contributing

Issues and PRs are welcome. By contributing you agree to the **Developer Certificate of Origin**
(sign your commits with `git commit -s`). Start with **[CONTRIBUTING.md](CONTRIBUTING.md)** and our
**[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)**.

If Accounting-Ops is useful to you, **starring the repo** genuinely helps others find it. ⭐

## License

**[GNU AGPL-3.0-or-later](LICENSE)** — you may run, study, share, and modify it; if you offer it as
a network service, your users are entitled to the source of your version. Enterprise modules ship
under a separate commercial license and are not part of this repository. See **[NOTICE](NOTICE)**.

<div align="center">
<br/>
<sub><b>Accounting-Ops is not a CPA, EA, tax preparer, attorney, or fiduciary.</b><br/>
It is a tool that helps you do that work — and you are responsible for what you do with it.</sub>
</div>
