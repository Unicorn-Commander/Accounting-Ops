# Architecture

Accounting-Ops is an **agent-first** financial-operations platform: autonomous agents do the
bookkeeping work, and the application is the observation, approval, and chat surface over an
append-only ledger. This document describes the Community Edition runtime — everything here runs
on your own hardware with no external dependency.

## Runtime topology

```
You + external agents (Claude / Codex / any MCP client)
        │
        ▼
┌───────────────────┐        ┌──────────────────────────────┐
│  web (Next.js 15) │  HTTP  │        app (FastAPI)         │
│  local-auth login │ ─────► │  REST + /mcp + /a2a surfaces │
└───────────────────┘        └──────────────┬───────────────┘
                                             │
        ┌────────────────────────────────────┼───────────────────────────────┐
        ▼                    ▼                ▼                 ▼              ▼
  ┌───────────┐      ┌──────────────┐   ┌──────────┐   ┌───────────────┐  ┌──────────┐
  │ Postgres  │      │   Garage     │   │  Ollama  │   │  Infinity     │  │ FalkorDB │
  │ per-ws    │      │  S3 blobs    │   │  BYO LLM │   │  embeddings   │  │  graph   │
  │ schemas + │      │ receipts/doc │   │ (OpenAI- │   │  (optional)   │  │(optional)│
  │ hash ledg.│      │              │   │  compat) │   │               │  │          │
  └───────────┘      └──────────────┘   └──────────┘   └───────────────┘  └──────────┘
```

- **web** — Next.js 15 (App Router). In Community mode it authenticates with a NextAuth
  **Credentials** provider against a single env-configured local admin (`AO_AUTH_MODE=local`).
- **app** — FastAPI. Exposes the same operations three ways: a REST API for the web app, an
  **MCP** surface (`/mcp`) for agent clients, and an **A2A** surface (`/a2a`) for agent-to-agent
  calls. There is no shadow API — the UI uses the same endpoints agents do.
- **Postgres** — the system of record. One schema per workspace (isolation boundary); the journal
  is an append-only, per-entity SHA-256 hash chain.
- **Garage** — S3-compatible object storage for receipts and documents.
- **Ollama** — the default bring-your-own-model target (OpenAI-compatible). Swappable for vLLM,
  a remote Ollama, an on-prem gateway, or a hosted API via `LLM_ENDPOINT_URL` / `LLM_MODEL`.
- **Infinity / FalkorDB** — optional. The embeddings index (Tax-Knowledge RAG) and a best-effort
  agent-memory graph. Both degrade gracefully: the app runs fully without them.

## The posting pipeline

```
source txn ──► categorize ──► materiality gate ──► [auto-post | review queue | hard-block]
 (bank/CSV/     Tier 1: rules    small→auto            │
  connector)    Tier 2: model    mid→confirm           ▼
                (optional)       large→block      journal_entry appended to the hash chain
                                 no receipt→block  (double-entry, per-entity)
```

1. **Ingest.** Transactions arrive from a bank/CSV import or a connector. Ingest stays on your
   machine.
2. **Categorize — deterministic first.**
   - *Tier 1* is `app/categorization/vendor_rules.py`, a deterministic vendor-rule engine. Fast,
     auditable, **no model call**, and it handles the bulk of recurring transactions.
   - *Tier 2* is an optional model fallback for whatever Tier 1 leaves `Uncategorized`. Tier-2
     results are **always queued for review**, never auto-posted. The categorizer runs end-to-end
     with **no model at all**.
3. **Materiality gate** (at posting time):
   - small auto-categorized items **auto-post**;
   - mid-size items queue for a human **Confirm**;
   - large items **hard-block** pending an explicit confirmation + memo;
   - travel/meals **block** without a matching receipt.
4. **Post.** An approved transaction becomes a `journal_entry` row appended to the per-entity hash
   chain (double-entry). Reversals create *new* entries — nothing is ever overwritten.

## The hash chain

Each journal entry commits to its predecessor:

```
this_hash = sha256( prev_hash || canonical_payload(entry) )
```

- `canonical_payload` is a stable, canonical serialization of the entry (amounts, accounts,
  entity, timestamps, provenance).
- The chain is **per entity** within a workspace schema.
- `verify_chain` walks the chain from genesis and reports the first index whose recomputed hash
  disagrees with the stored hash — i.e. the exact point of any tampering.

Because reversals are additive, the ledger is **append-only**: history is immutable and every
current balance is the deterministic result of replaying the chain.

## Provenance

Every posted figure is explainable. The provenance chain links, in order:

```
report line ──► journal entries ──► source transaction ──► agent decision ──► sha256 verified
```

A missing or unverifiable source is treated as an **error** (HTTP 422), not something the system
guesses around. If a fact about the business can't be explained, it can't be posted. See
[PROFESSIONAL-SUBSTITUTE.md](PROFESSIONAL-SUBSTITUTE.md) for the design stance behind this.

## Multi-tenancy — `Workspace > Entity > User`

- **Workspace** — the isolation boundary. Each workspace is a dedicated Postgres schema; a
  workspace-scoped session pins every query to that schema.
- **Entity** — a legal entity within a workspace (an LLC, an S-corp, a personal 1040). Entities are
  row-scoped within the workspace schema, so consolidated and per-entity views come from the same
  ledger.
- **User** — the auth subject (`uc_uid`). In Community/local mode this is your single admin; RBAC
  still resolves through the normal membership checks.

Provisioning a workspace (`scripts/provision_workspace.py`) creates the schema, runs tenant
migrations, and seeds a chart of accounts.

## Security model

- **Least privilege.** The runtime database role is `NOSUPERUSER` and does not own its objects, so
  the append-only guarantee is enforced by the schema, not merely by application code.
- **Encrypted credentials.** Connector credentials are encrypted at rest with a local master key
  (`secrets/accounting_ops_master_key`, an untracked Docker secret). Back it up out-of-band —
  losing it orphans every stored credential.
- **No telemetry by default.** The OpenTelemetry endpoint defaults to none and there is no
  analytics SDK. Nothing about your books leaves the host unless you configure an egress yourself.

## Editions & the enterprise seam

Community mode is a runtime gate driven by two env flags:

| Flag | Community | Enterprise (default) |
|---|---|---|
| `AO_EDITION` | `community` | `enterprise` |
| `AO_AUTH_MODE` | `local` | `keycloak` |

With `AO_EDITION=community`, the enterprise routers (multi-client CPA console, cross-app
federation, hosted-gateway key minting) are **not imported or mounted**, and the enterprise nav is
hidden. The community app has no runtime dependency on any of them. Enterprise modules are shipped
separately under a commercial license and are not part of this repository.

## Where things live

| Path | What |
|---|---|
| `app/main.py` | FastAPI app; router mounting; edition gate |
| `app/categorization/` | Tier-1 vendor rules + Tier-2 model fallback |
| `app/journal/` | double-entry posting, hash chain, `verify_chain` |
| `app/connectors/`, `app/clients/` | ingest + connector clients |
| `app/tax/`, `app/exports/` | tax knowledge/forms + CPA exports |
| `agents/` | sentinel, cpa_team, tax_specialists, tax_research, chat |
| `migrations/public/`, `migrations/tenant/` | schema migrations |
| `scripts/` | bootstrap, provisioning, seeding |
| `web/app/` | Next.js routes (dashboard, journal, reports, taxes, …) |
