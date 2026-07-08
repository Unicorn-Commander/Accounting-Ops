# Accounting-Ops — the first open-source autonomous AI accountant

**Your books. Your machine. Your rules.**

Accounting-Ops is an agent-first, self-hostable bookkeeping and tax-prep
platform. Autonomous agents categorize transactions, reconcile receipts,
maintain an append-only general ledger, draft financial statements, and
assemble tax-form packages — while you stay in control through an observation,
approval, and chat surface. It runs entirely on **your** hardware with **your**
models. No cloud account required, no telemetry by default.

> ⚠️ **Not tax, legal, or accounting advice.** Accounting-Ops is software, not a
> licensed professional, and using it creates no professional relationship. You
> alone are responsible for anything you file, sign, or pay — verify every figure
> against your own records and consult a qualified CPA/EA/attorney for anything
> with legal or financial consequence. See **[DISCLAIMER.md](DISCLAIMER.md)**.

Licensed under the **[GNU AGPL-3.0](LICENSE)**.
See **[CONTRIBUTING.md](CONTRIBUTING.md)** · **[SECURITY.md](SECURITY.md)** ·
**[docs/PROFESSIONAL-SUBSTITUTE.md](docs/PROFESSIONAL-SUBSTITUTE.md)**.

---

## What it does

| Surface | What it does |
|---|---|
| `/dashboard` | Cash position + YTD Revenue / Expenses / Net Income from the journal, recent transactions |
| `/transactions` | Unified transaction stream with categorization, entity badges, and materiality |
| `/reconciliation-queue` | Human-in-the-loop review: Confirm posts to the journal; Reject trains a category override |
| `/journal` + `/journal/[id]` | Paginated journal with per-entity verify-chain, double-entry detail, provenance, reversal lineage |
| `/reports` | P&L / Balance Sheet / Cash Flow with period chips (rendered in-app, no external service) |
| `/cpa-export` | CSV / IIF / QBO XML / PDF exports (optional sidecar) |
| `/chat` | Chat with the accountant agent over your ledger (bring your own model) |
| `/taxes` | Tax-knowledge Q&A + draft tax-form packages (labeled **draft / do not file**) |

### Append-only journal with a hash chain

Every posted transaction becomes a `journal_entry` row in a per-entity SHA-256
hash chain. Reversals create *new* entries (never overwrite). `verify_chain`
walks the chain and reports any tampered index.

```
prev_hash || canonical_payload(entry) -> sha256 = this_hash
```

Materiality gate at posting time: small auto-categorized items auto-post;
mid-size items queue for human Confirm; large items hard-block pending an
explicit confirmation + memo; travel/meals block without a matching receipt.

### Categorization is deterministic first

Tier 1 is a **deterministic vendor-rule engine** (`app/categorization/vendor_rules.py`)
— fast, auditable, **no model call**, and it covers the bulk of recurring
transactions. A model is only consulted as an optional Tier-2 fallback for
transactions the rules leave `Uncategorized`, and those are always queued for
review rather than auto-posted. **You can run the whole categorizer with no
model at all.**

---

## Quickstart (self-host)

**Prerequisites:** Docker + Docker Compose v2, and (for the bootstrap helper)
Python 3.12 and `openssl` on the host. No GPU required — a CPU-only Ollama works,
just slower.

### One command

```bash
git clone <this-repo> accounting-ops && cd accounting-ops
make selfhost
```

`make selfhost` (→ `scripts/selfhost_bootstrap.sh`) is idempotent and will:

1. create `.env` from `.env.example`,
2. generate strong secrets (Postgres, Garage, NextAuth) and the
   `AO_LOCAL_AUTH_SECRET`,
3. set `AO_EDITION=community` + `AO_AUTH_MODE=local`,
4. prompt you for a local admin password and store its scrypt hash
   (via `scripts/gen_local_admin_hash.py`),
5. generate the credential master key (`secrets/accounting_ops_master_key`),
6. build + start the stack (`docker-compose.selfhost.yml`): **postgres +
   garage + ollama + app + web**,
7. run migrations, provision one workspace + entity, grant your admin the
   owner role, and seed a chart of accounts.

Then open **http://localhost:3000**, log in with your admin email + password,
and you're in.

Pull a vision model so the agent can categorize and read receipts:

```bash
docker compose -f docker-compose.selfhost.yml exec ollama ollama pull qwen2.5vl:7b
```

Add a manual journal entry (or import a bank CSV), open **/reports**, and render
a P&L — all with **no external fleet dependency**.

### Manual (what the bootstrap does under the hood)

```bash
cp .env.example .env

# 1. Secrets — a long random value in each. AO_LOCAL_AUTH_SECRET must be the
#    SAME value the web and backend share.
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

# 6. Migrate + provision (run inside the app container)
WID=$(python -c 'import uuid;print(uuid.uuid4())')
docker compose -f docker-compose.selfhost.yml exec -T app alembic -c alembic-public.ini upgrade head
docker compose -f docker-compose.selfhost.yml exec -T app python -m scripts.provision_workspace "$WID" "My Company" --entity "My Company:llc"
docker compose -f docker-compose.selfhost.yml exec -T app python -m scripts.grant_workspace_admin "$WID" admin --role owner
docker compose -f docker-compose.selfhost.yml exec -T app python -m scripts.seed_default_coa "$WID" my-company
# then set DEFAULT_WORKSPACE_ID + TENANT_ID to $WID in .env and restart app + web
```

### Bring your own model

The bundled **Ollama** service (`http://ollama:11434/v1`) is the default target
for the categorizer and receipt OCR — set `LLM_ENDPOINT_URL` / `LLM_MODEL` in
`.env` to point anywhere OpenAI-compatible (a remote Ollama, vLLM, a gateway, or
a hosted API). Choose a **vision** model for receipt reading.

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

## Editions

Accounting-Ops is **open-core**. The Community Edition is the full self-hostable
product; a couple of multi-tenant/suite surfaces are held for the Enterprise
Edition and are simply not mounted when `AO_EDITION=community`.

| | Community (this repo, AGPL) | Enterprise |
|---|---|---|
| Ledger, categorization, reports, CPA export | ✅ | ✅ |
| Agents (categorize / reconcile / CFO / tax specialists) | ✅ | ✅ |
| Auth | **Local single-admin** (`AO_AUTH_MODE=local`) | Keycloak / OIDC SSO |
| Bring-your-own models | ✅ (Ollama by default) | ✅ + shared gateway |
| Multi-client CPA console / cross-client work inbox | — | ✅ |
| Suite federation (Brigade, Contact-Ops, cross-app entitlement) | — | ✅ |

### What the Community Edition does **not** do

To keep the free, self-hostable edition safe to run, these are **off and clearly
labeled** (see [DISCLAIMER.md](DISCLAIMER.md)):

- **No electronic filing.** It generates tax packages *for your own records*; it
  does **not** transmit to the IRS (MeF), a **state portal**, or any authority.
  Generated tax documents are watermarked **draft / "do not file."**
- **No tax payments.** No **EFTPS** connection; it never moves money.
- **No e-signature** submission of filings.
- **No trades, transfers, or payments** are executed on your behalf.

The tax-submission tracker only *records* that you sent a filing (paper /
portal / e-file) — it does not send anything.

---

## Architecture

```
You + external agents (Claude / Codex / MCP clients)
        |
        v
   web (Next.js 15)  --->  app (FastAPI)  --->  Postgres (per-workspace schemas,
        (local-auth Credentials provider)          append-only hash-chained ledger)
                                |
                                +--> Garage (S3-compatible blob storage: receipts/docs)
                                +--> Ollama (bring-your-own model, OpenAI-compatible)
                                +--> [optional] Infinity embeddings · FalkorDB graph
```

- **MCP-first / agent-first.** The FastAPI app exposes an MCP surface (`/mcp`) and
  A2A surface (`/a2a`) so agents operate the ledger the same way the UI does — no
  wrappers, no shadow API.
- **Deterministic where it matters.** Categorization Tier 1 and the double-entry
  posting engine are rule-based and model-free; models are an optional assist.

## Multi-tenancy model

`Workspace > Entity > User`:

- **Workspace** — the isolation boundary (a Postgres schema per workspace).
- **Entity** — a legal entity within a workspace (e.g. your LLC, an S-corp, a
  personal 1040). Row-scoped within the workspace schema.
- **User** — the auth subject (`uc_uid`). In Community/local mode this is your
  single admin.

See [docs/WORKSPACE-ENTITY-USER-MODEL.md](docs/WORKSPACE-ENTITY-USER-MODEL.md).

## Documentation

- [docs/PROFESSIONAL-SUBSTITUTE.md](docs/PROFESSIONAL-SUBSTITUTE.md) — the design stance: an autonomous professional-substitute you supervise
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — runtime architecture, data flows, hash-chain spec
- [docs/USER-GUIDE.md](docs/USER-GUIDE.md) — per-page walkthrough
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — operate, monitor, restore
- [docs/WORKSPACE-ENTITY-USER-MODEL.md](docs/WORKSPACE-ENTITY-USER-MODEL.md) — tenancy model
- [COMMUNITY_MANIFEST.md](COMMUNITY_MANIFEST.md) — which paths ship in Community vs Enterprise
- [DEPLOY.md](DEPLOY.md) — advanced deployment (reverse proxy, least-privilege DB roles, backups)

## Security & your data

- Connector credentials are encrypted at rest with a local master key
  (`secrets/accounting_ops_master_key`). **Back it up out-of-band** — losing it
  orphans all encrypted credentials.
- No telemetry by default. Report vulnerabilities per [SECURITY.md](SECURITY.md).

---

*Accounting-Ops is not a CPA, EA, tax preparer, attorney, or fiduciary. It is a
tool that helps you do that work, and you are responsible for what you do with
it. See [DISCLAIMER.md](DISCLAIMER.md).*
