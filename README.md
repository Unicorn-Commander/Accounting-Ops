# Accounting-Ops

Agent-first, MCP-first financial operations platform for Magic Unicorn
Unconventional Technology & Stuff Inc. Brigade agents (when wired in) do
categorization, reconciliation, compliance monitoring, CPA bundle prep,
and payment proposals; the web app is the observation + approval + chat
surface.

**Anchor**: *Your books. Your machine. Your rules.*

**Production (customer SaaS)**: https://accounting-ops.unicorncommander.ai (host: centerdeep)
**Dogfood / internal**: https://accounting-ops.magicunicorn.dev (host: midboy1)
**Self-service onboarding**: new users land on `/welcome` → provision their own workspace → Setup Assistant (entity → chart of accounts → connect bank)
**Project-Ops record**: P-00050
**Standup runbook**: `docs/PROD-RING-RUNBOOK.md`

---

## What's Live

| Surface | What it does |
|---|---|
| `/dashboard` | Cash position (BofA + USAA via SimpleFIN), YTD Revenue/Expenses/Net Income KPIs from the journal, recent live transactions |
| `/transactions` | Unified canonical stream: SimpleFIN bank transactions + eBay orders, with categorization + entity badges + materiality |
| `/reconciliation-queue` | 3-column human-in-the-loop (Needs Review / Uncategorized / Auto-categorized). Confirm posts to journal; Reject trains a category override |
| `/journal` | Paginated journal with filters, per-entity summary cards with verify-chain button, click-through to entry detail |
| `/journal/[id]` | Full entry view: double-entry, source provenance, reasoning trace, hash chain context, reversal lineage |
| `/journal/new` | Manual entry form (cash, owner contributions, reclasses, year-end adjustments) |
| `/reports` | Inline P&L / Balance Sheet / Cash Flow with period chips; auto-classified operating/investing/financing |
| `/cpa-export` | CSV / IIF / QBO XML / PDF P&L / Balance Sheet / Cash Flow downloads |
| `/connectors` | SimpleFIN claim, account-to-entity mapping |

## Architecture

```
External agents (Claude / Codex / Brigade peers)
        |
        v
 accounting-ops.magicunicorn.dev (Traefik on midboy1)
        |
        +-- web (Next.js 15)  --+
        +-- app (FastAPI)      |
                |              v
                |       cpa-export :8093  (read-only journal exports)
                |       receipt-reconciler :8092 (matches receipts → txns)
                |       receipt-worker :8091    (IMAP → Qwen3-VL → Garage)
                |       ebay-finances-mcp :8000 (FastMCP, eBay Sell APIs)
                |       imap-mcp :8000          (read-only IMAP)
                |       simplefin-mcp :8000     (BofA + USAA aggregator)
                |
                v
        Postgres 16 (per-workspace schemas)
          - public:                    workspace, user_account, membership,
                                        tenant_credentials (AES-256-GCM),
                                        worker_extractions, worker_runs,
                                        receipt_transaction_link
          - accounting_ops_{wid}:      entity, journal_entry, chart_of_account,
                                        account_entity_mapping, category_override,
                                        posting_log, ...
        Garage (S3-compatible blob storage)
          - accounting-ops-receipts-{wid}: receipt PDFs by content hash
        Keycloak uchub realm (OIDC + service tokens)
```

## Append-Only Journal with Hash Chain

Every posted transaction becomes a `journal_entry` row tied into a
per-entity SHA-256 hash chain. Reversals create *new* entries (never
overwrite). `verify_chain` walks the chain and reports tampered indexes.

```
prev_hash || canonical_payload(entry) -> sha256 = this_hash
```

Materiality gate at posting time:
- **< $75** → auto-post when categorization is `auto` AND entity is mapped AND sign matches
- **$75-$1,000** → queue for human Confirm on `/reconciliation-queue`
- **> $1,000** → hard block, requires explicit user_confirmed=true with memo
- **IRS §274(d)** → travel/meals always block without a matching receipt

## Connectors

| Source | Status | What it gives us |
|---|---|---|
| SimpleFIN (BofA, USAA) | ✅ Live | All bank transactions + balances |
| eBay Sell Fulfillment | ✅ Live | Order-level revenue (gross sales) |
| eBay Sell Finances | ⏳ Application Growth Check pending (ticket 260522-000033) | Payout-level reconciliation when approved |
| IMAP Receipts (Stalwart) | 🔌 Awaits accounting@magicunicorn.dev setup | Receipt OCR → match against transactions |
| Listing-Ops bridge | 🗂 Documented, deprioritized | Alternate path to eBay orders via the existing Listing-Ops integration |

## Categorization Engine

Two-tier (Tier 2 LLM is stubbed for v1):

1. **User overrides** → `category_override` table, populated by Reject button
2. **Vendor rules** → ~50 patterns covering Stripe, Anthropic, AWS, Hostinger,
   Postmark, Mercury, eBay, etc.
3. **Fallback** → `Uncategorized`, surfaces on `/reconciliation-queue`

Sign-mismatch detection: if a vendor rule expects revenue but the txn is
negative (or vice versa), the suggestion gets flagged for review even at
tier L.

## Local Development

Prerequisites: Python 3.12, Docker Compose v2, Node 22.

```bash
cp .env.example .env
# Fill POSTGRES_PASSWORD, GARAGE_RPC_SECRET, GARAGE_ADMIN_TOKEN,
# NEXTAUTH_SECRET, Keycloak secrets. Also any RECEIPT_WORKER_*,
# RECEIPT_RECONCILER_*, CPA_EXPORT_*, ACCTOPS_SERVICE_TOKENS
# values you want set.
docker compose up -d postgres garage
docker exec accounting_ops-app-1 sh -c \
  "cd /app && alembic -c alembic-public.ini upgrade head"
python scripts/seed_local.py
docker compose up app web
```

Web shell: `http://localhost:3000`. API health: `http://localhost:8000/healthz`.

## Compose Profiles

| Profile | Services | When |
|---|---|---|
| (default) | `postgres`, `garage`, `app`, `web` | Daily use |
| `connectors` | `simplefin-mcp`, `ebay-finances-mcp`, `imap-mcp` | When testing external connector code |
| `receipts` | `receipt-worker`, `receipt-reconciler` | When IMAP creds + Garage bucket are configured |
| `exports` | `cpa-export` | CPA download service |

## Sibling Repos

Service code that lives in its own repo, used by this stack:

- `accounting-ops-receipt-worker` — IMAP → Qwen3-VL → Garage extraction worker
- `accounting-ops-reconciler` — receipt ↔ transaction matcher (deterministic 7-rule scorer)
- `accounting-ops-cpa-export` — CSV/IIF/QBO/PDF export service
- `accounting-ops-ebay-finances-mcp` — FastMCP wrapper over eBay Sell APIs
- `accounting-ops-simplefin-mcp` — SimpleFIN aggregator MCP
- `accounting-ops-imap-mcp` — IMAP read-only MCP

All wired into `docker-compose.yml` via `../<repo-name>/` build contexts.

## Service Tokens

Long-running internal workers (reconciler, cpa-export) authenticate via
`ACCTOPS_SERVICE_TOKENS` — a JSON map keyed by token, value `{uc_uid,
agent_id}`. The bearer token is constant-time-compared in `app/auth.py`
and impersonates the configured uc_uid for RBAC.

```bash
# Mint a token (32+ chars), then add to /home/muut/Accounting-Ops/.env:
ACCTOPS_SERVICE_TOKENS={"<token>":{"uc_uid":"aaron","agent_id":"my-worker"}}
```

## Multi-Tenancy Model

`Workspace > Entity > User`:

- **Workspace** is the schema boundary — a Postgres schema per workspace (`accounting_ops_<wid>`)
- **Entity** is a row predicate within a workspace — Magic Unicorn Inc, Aaron Personal, Majiks JV, Brightwater Labs
- **User** is the auth subject (`uc_uid` from Keycloak)

See `docs/WORKSPACE-ENTITY-USER-MODEL.md` for details.

## Documentation

- `docs/ARCHITECTURE.md` — runtime architecture, data flows, hash chain spec
- `docs/USER-GUIDE.md` — per-page walkthrough
- `docs/OPERATIONS.md` — deploy, monitor, restore
- `docs/PHASE-2-RECEIPT-WORKER-BOOTSTRAP.md` — receipt-worker setup
- `docs/PHASE-2-RECEIPT-VLM-PIPELINE.md` — Tier 1/2/3 VLM design
- `docs/PHASE-1-6-ENCRYPTED-CREDENTIALS.md` — credential storage spec
- `docs/STALWART-IMAP-BOOTSTRAP.md` — mailbox setup for receipts
- `docs/WORKSPACE-ENTITY-USER-MODEL.md` — tenancy model
- `docs/AGENT-ACTIVITY-SPEC.md` — agent observation surface
- `docs/DESIGN-BRIEF.md` — original product brief

## Deploy

```bash
# From this directory on aaronstransky's Mac:
rsync -a --delete --exclude='.git' --exclude='__pycache__' --exclude='web/node_modules' \
  --exclude='web/.next' --exclude='.env' --exclude='secrets/' \
  ./ midboy1:/home/muut/Accounting-Ops/

# On midboy1:
cd /home/muut/Accounting-Ops
docker compose build app web
docker compose up -d app web
```

Tenant migrations run via:
```bash
docker exec accounting_ops-app-1 sh -c "cd /app && alembic -c alembic-tenant.ini \
  -x tenant=accounting_ops_<workspace-uuid-underscored> upgrade head"
```

## Status (2026-05-22)

✅ **Production-live**. Daily usable for BofA + USAA + eBay orders + manual entries. Reports, CPA export, journal hash chain all working. Reject + override loop completes the human-in-the-loop categorization.

⏳ **Pending external** (no code work needed when they unblock):
- eBay Application Growth Check (ticket 260522-000033) — switches eBay from orders to payout-level data
- Stalwart mailbox creation — starts the receipt OCR pipeline (5-min hands-on, see `docs/STALWART-IMAP-BOOTSTRAP.md`)

🗂 **Roadmap**:
- Bulk-confirm + keyboard shortcuts on `/reconciliation-queue`
- COA editor UI (currently seeded only)
- Brigade agents wired to ledger for autonomous categorization/reconciliation
- Multi-currency support (FX rate table exists but not consumed yet)
- 1099-NEC year-end reporting
- Listing-Ops bridge as alternate eBay path

## Vendor Skills

Anthropic financial-services scaffolding is vendored read-only at
`vendor/anthropic-financial-services/`. Reference only — we don't modify
or extend their prompts in this repo.
