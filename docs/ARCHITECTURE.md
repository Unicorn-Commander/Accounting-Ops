# Architecture

How Accounting-Ops works at runtime, what data flows where, and where
the trust boundaries are.

## Runtime topology

```
                       ┌──────────────────────────────────────┐
                       │  Browser (Aaron / future users)      │
                       │  Keycloak SSO → NextAuth.js session  │
                       └──────────────────────────────────────┘
                                       │ https
                                       ▼
            ┌────────────────────────────────────────────────┐
            │  Traefik on midboy1 :443                       │
            │  accounting-ops.magicunicorn.dev               │
            │  - / and /journal/* /reports/* /etc → web      │
            │  - /api/{nextauth} → web                       │
            │  - /api, /mcp, /a2a, /.well-known → app         │
            └────────────────────────────────────────────────┘
                    │                       │
                    ▼                       ▼
        ┌─────────────────┐         ┌──────────────────────┐
        │  web (Next 15)  │         │  app (FastAPI)       │
        │  :3000          │ ←──→    │  :8000               │
        │  Server pages   │  HTTP   │  REST + MCP + A2A    │
        │  Client comps   │         │  Keycloak JWT auth   │
        └─────────────────┘         │  Service-token auth  │
                                    └──────────┬───────────┘
                                               │
                       ┌───────────────────────┼───────────────────────┐
                       ▼                       ▼                       ▼
              ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
              │ Postgres 16      │  │ Garage S3        │  │ Sidecar services │
              │ per-workspace    │  │ receipt PDFs +   │  │ on the same      │
              │ schemas          │  │ CPA bundles      │  │ docker network   │
              └──────────────────┘  └──────────────────┘  └──────────────────┘
                                                                  │
                       ┌──────────────────────────────────────────┤
                       ▼                          ▼               ▼
              ┌────────────────┐  ┌────────────────┐  ┌──────────────────┐
              │ receipt-worker │  │ receipt-recon  │  │ cpa-export       │
              │ :8091          │  │ :8092          │  │ :8093            │
              │ IMAP→VLM→Garage│  │ score matches  │  │ CSV/IIF/QBO/PDF  │
              └────────────────┘  └────────────────┘  └──────────────────┘
                       ▼
              ┌────────────────────┐
              │ Qwen3-VL-8B        │
              │ host.docker.intl   │
              │ midboy1:8087/v1    │
              └────────────────────┘

              ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
              │ simplefin-mcp    │  │ ebay-finances-mcp│  │ imap-mcp         │
              │ FastMCP stdio    │  │ FastMCP streaming│  │ FastMCP stdio    │
              └──────────────────┘  └──────────────────┘  └──────────────────┘
                       │                      │                     │
                       ▼                      ▼                     ▼
                  beta-bridge          api.ebay.com           IMAPS server
                  .simplefin.org       (OAuth refresh)        (Stalwart)
```

External federation peers (Tailscale-meshed):
- `bigboy` — GPU host, secondary Qwen, FalkorDB for graph memory
- `lilguy1` — DR for Postgres + Garage (planned, not wired yet)
- `centerdeep` — OTel + Loki + Prometheus stack

## Data flow: a transaction landing in the journal

```
1. SimpleFIN poll (or eBay Fulfillment fetch)
        │
        ▼ canonical txn JSON
2. /api/workspaces/{wid}/recent-transactions
        │
        ▼ joined with account_entity_mapping + categorization
        │   (user overrides → vendor rules → Uncategorized)
3. UI: /transactions, /dashboard, /reconciliation-queue
        │
        ▼ user clicks Confirm (or auto-post for tier-L matches)
4. POST /api/workspaces/{wid}/journal-entries
        │
        ▼ materiality gate (L: auto, M: confirm, H: block)
        ▼ resolve_accounts_for_txn() → (debit_canonical, credit_canonical)
        ▼ idempotency check on (entity_id, source, source_external_id)
        ▼ get_last_chain_hash(entity_id) → prev_hash
        ▼ compute_hash(prev_hash, payload) → this_hash
        ▼ INSERT INTO journal_entry (...) inside SAVEPOINT
        ▼ INSERT INTO posting_log (outcome=posted, ...)
5. UI: /journal, /journal/[id], /reports recompute
```

## Per-workspace schema layout

```
accounting_ops_<workspace_uuid_underscored>
├── entity                        — Magic Unicorn Inc, etc.
├── account_entity_mapping        — connector account → entity
├── chart_of_account              — per-entity COA, 46 default accounts
├── journal_entry                 — append-only ledger with hash chain
├── posting_log                   — every post attempt (success or refused)
├── category_override             — user-trained category rules
├── receipt                       — receipt metadata
├── recon_link                    — receipt ↔ journal_entry links
├── counterparty / vendor         — payee normalization
├── compliance_profile / rule     — per-entity compliance
├── activity_event                — agent activity feed
├── decision_audit                — agent decision trail
├── approval_queue                — items awaiting human approval
├── event_log                     — event-sourced base events
├── connector_state               — last-sync watermarks
├── agent_session                 — agent-of-agent sessions
└── fx_rate                       — multi-currency normalization (unused v1)
```

Shared public schema:
```
public
├── workspace                     — workspace registry
├── workspace_membership          — uc_uid ↔ workspace role
├── user_account                  — uc_uid registry
├── tenant_credentials            — encrypted credential blobs (AES-256-GCM)
├── credential_access_log         — audit of credential reads
├── worker_extractions            — receipt-worker output
├── worker_runs                   — receipt-worker poll log
├── worker_dedup_index            — content + fingerprint hash index
├── worker_reconcile_runs         — reconciler poll log
└── receipt_transaction_link      — matched receipts (deterministic scorer)
```

## Hash chain spec

Per-entity SHA-256 chain. Each entry computes:

```
prev_hash = (most recent entry for this entity).this_hash  (32 zero bytes if genesis)

canonical_payload = effective_date || \x1f
                 || amount_cents     || \x1f
                 || debit_account    || \x1f
                 || credit_account   || \x1f
                 || source           || \x1f
                 || source_external_id || \x1f
                 || payee            || \x1f
                 || description      || \x1f
                 || posted_at

this_hash = sha256(prev_hash || canonical_payload)
```

`\x1f` is ASCII Unit Separator — used as a delimiter so a payee that
happens to contain the separator value can't forge a different
canonical payload.

The DB enforces uniqueness on `(entity_id, this_hash)` so concurrent
posts can't fork the chain — losers retry with the fresh prev_hash.

`verify_chain(entity_id)` walks the entries in (effective_date,
posted_at, id) order, recomputes each hash, and returns the list of
tampered positions.

## Materiality gate

`app/journal/materiality.py`:

```
def evaluate(amount_cents, category, category_status, entity_mapped,
             sign_matched, has_receipt_link, user_confirmed):

    tier = "L" if abs < 7500 else "M" if abs < 100000 else "H"

    # IRS §274(d) override (non-negotiable)
    if category in {Travel·Lodging, Travel·Transport, Meals}:
        if not has_receipt_link:
            return MaterialityVerdict(tier, "block_274d", ...)

    if user_confirmed:
        return MaterialityVerdict(tier, "auto_post", ...)

    if tier == "H":
        return MaterialityVerdict("H", "block_high_value", ...)

    if tier == "M":
        return MaterialityVerdict("M", "queue_for_confirm", ...)

    # Tier L: auto only if categorization is fully trustable
    if category_status == "auto" AND entity_mapped AND sign_matched
       AND category != "Uncategorized":
        return MaterialityVerdict("L", "auto_post", ...)

    return MaterialityVerdict("L", "queue_for_confirm", ...)
```

## Categorization tiers

1. **Tier 0: user overrides** (`category_override` table, populated by Reject button)
2. **Tier 1: vendor rules** (~50 substring patterns → category mapping)
3. **Tier 2: LLM** (stubbed; would call local Qwen 3.6 with GBNF constrained to canonical categories)
4. **Fallback: Uncategorized** (surfaces in /reconciliation-queue)

A tier-1 vendor rule produces `(category, kind)` where kind ∈ {revenue,
expense, transfer}. The engine checks if the txn sign matches the
expected kind; mismatch → `sign_matched=false`, which the materiality
gate uses to refuse auto-post.

## Auth + tenancy

Two auth paths land at the same `WorkspaceContext`:

```
Path A — User session (browser):
  Keycloak login → NextAuth.js session → forwards access_token to app
  → app/auth.py validates JWT against Keycloak JWKS
  → uc_uid extracted from claims
  → resolve_workspace() looks up workspace_membership row
  → WorkspaceContext(uc_uid, workspace_id, role, entity_scope)

Path B — Service token (long-running worker):
  Worker sends `Authorization: Bearer <random-token>`
  → app/auth.py constant-time compares against ACCTOPS_SERVICE_TOKENS
  → impersonates the configured uc_uid
  → synthesizes AuthContext, same downstream behavior
  → resolve_workspace() still requires the impersonated user to have a
     workspace_membership row (no privilege escalation)
```

`workspace_session(workspace_id)` opens a single transaction and sets
`SET LOCAL search_path = accounting_ops_<wid>, public` so every query in
that request scope automatically lands in the right schema. The schema
boundary is what makes cross-workspace leakage a SQL impossibility.

## Encrypted credentials at rest

`public.tenant_credentials` stores connector secrets (SimpleFIN access
URLs, eBay refresh tokens, future IMAP creds):

```
ciphertext = AES-256-GCM(
    plaintext,
    key   = HKDF-SHA256(master_key, info="acctops:tenant-dek:v1:" || workspace_uuid),
    nonce = random 96-bit,
    aad   = workspace_uuid || credential_id
)
```

The master key lives in Docker secrets at
`/run/secrets/accounting_ops_master_key`, chmod 600, mounted read-only
into the app container. Losing the master key orphans all ciphertext.

See `docs/PHASE-1-6-ENCRYPTED-CREDENTIALS.md` for the threat model and
rotation flow.

## Idempotency

Three independent idempotency layers:

1. **Journal posting**: UNIQUE `(entity_id, source, source_external_id)` —
   the same SimpleFIN or eBay transaction can't post twice
2. **Hash chain**: UNIQUE `(entity_id, this_hash)` — concurrent posts
   can't fork the chain
3. **Receipt extraction**: `worker_dedup_index` on content_hash +
   fingerprint_hash — same PDF arriving twice via IMAP is detected as
   a soft duplicate

## Observability (planned, not wired)

OTel traces emitted by the FastAPI middleware in `app/otel.py`. Target
collector is centerdeep's Prometheus/Loki/Tempo stack at
`https://obs.magicunicorn.dev`. See `docs/AGENT-ACTIVITY-SPEC.md` for
the audit-span schema.
