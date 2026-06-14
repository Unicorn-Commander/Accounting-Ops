# Changelog

## Unreleased

## 0.7.0 - 2026-06-14

- feat(dashboard): **Per-number lineage** — click any figure (cash, revenue,
  expenses, net income, or a single account) to trace it to the posted journal
  entries behind it: provenance (source feed, category, materiality), the agent
  decision trail + reversal links, per-entry hash-chain verify status, and a
  reconciling net that matches the displayed number.
- feat(contacts): **Manual contact create** — a "+ New contact" form on
  `/contacts` (header + empty state) beside Import — and **"add as contact" from
  a transaction payee** in the reconciliation queue (kind inferred from the
  amount's sign). Reuses the existing counterparty upsert; no schema change.
- feat(dashboard): **Cash-ribbon daily-balance** endpoint — true end-of-day cash
  over the full posted journal, reconciling to the canonical cash figure.
- feat(dashboard): **Real-time agent stream** — the "Agents at work" ticker now
  consumes the SSE activity stream instead of polling every 6s (idle DB polling
  drops to a 30s safety net).
- feat(dashboard): **⌘K live mini-cards** — burn / cash / runway in the command
  palette, from the same CFO-brief assembler the dashboard uses.
- fix(tenancy): Stop serving one workspace's hardcoded document/decision
  checklist to other tenants — gated to the owning workspace by immutable UUID.
- fix(chat): Restore the per-entity chat session on page refresh, and never bind
  one entity's session to another on a workspace switch.
- fix(onboarding): After connecting a bank (SimpleFIN), a primary CTA routes to
  the Reconciliation Queue — connecting a bank is not the same as posted books.

## 0.6.0 - 2026-06-13

- feat(web): In-app **Guide** help agent on `/help`. A new read-only
  `product-guide` chat persona answers "how do I use Accounting-Ops?"
  (wayfinding, workflow walk-throughs, concept explanations — materiality
  gate, hash chain, sign mismatch) grounded in the real app. Reuses the
  shared chat surface: a guide-locked `EmbeddedChat` with clickable starter
  prompts sits atop the static help cards; replaces the stale "Ask the agent
  (planned)" link. Read-only tools only (no draft/queue/write); hands
  finance/tax substance to the existing CFO/tax personas. `EmbeddedChat` +
  `Composer` gained backward-compatible props (custom empty state, starter
  prompts, configurable footnote) — the global launcher is unchanged.
- fix(chat): Root-caused and fixed the gateway 403 "Your request was
  blocked". Cloudflare bot-management in front of the UC LLM gateway was
  rejecting the openai SDK's default `User-Agent`; the chat runner now sends
  a descriptive UA (override via `AO_CHAT_USER_AGENT`). Keeps the metering
  architecture intact (gateway + per-workspace keys); fixes both dogfood and
  prod. (Belt-and-suspenders follow-up: a Cloudflare WAF exemption for the
  gateway API.)
- fix(chat): Surface LLM/gateway errors instead of an infinite typing
  spinner — the SSE `error` frame is now terminal and rendered. Also repair
  the `Composer` send-arrow button, which forwarded a `MouseEvent` into the
  new `send(textOverride)` and crashed on every `EmbeddedChat` surface (the
  Cmd+Enter and starter-prompt paths masked it). And reset a surfaced error
  on session navigation (New chat / switch session) so it doesn't linger.
- polish(web): Configurable `Composer` footnote; the read-only Guide shows
  "The Guide is read-only — it explains how to use Accounting-Ops and never
  changes your books."

## 0.5.1 - 2026-06-10

- fix(web): UI/UX audit pass. Define the missing `brand-200/300/700` Tailwind
  tokens (137 colorless `text-brand-*` usages across 48 files now render their
  intended violet accents — active nav, selected filters, onboarding accents,
  tax-review tabs). Plus: mobile-safe SimpleFIN + invite dialogs (invite modal
  converted to a real Radix Dialog — focus-trap, Escape, `role`/`aria-modal`);
  `htmlFor`/`id` + `aria-describedby` on the manual journal-entry form; sticky
  table headers (transactions / journal / trial-balance / invoices / PATs);
  `cursor-pointer` cleanup on non-clickable rows (journal rows stay navigable
  with `focus-within`); shared `EmptyState` on the dashboard; contacts table
  min-width; card-radius consistency; safe-area offset for the chat launcher;
  compact setup-chat height on mobile.

## 0.5.0 - 2026-06-10

- feat(onboarding): Self-service first-run onboarding. A brand-new user (zero
  workspace memberships) can now provision their OWN workspace and become its
  `owner` via `POST /api/onboarding/provision-first-workspace` — the one
  provisioning path that does NOT require an existing admin/owner (additional
  workspaces stay admin-gated via `create_workspace`). The web `/welcome` route
  auto-catches no-workspace users (dashboard / root / setup / chat redirect
  there), provisions on one click, sets the active-workspace cookie, then hands
  off to the existing `/setup` Setup Assistant (entity → chart of accounts →
  connect bank). As `owner` the user can connect banks immediately. Idempotent
  under double-submit (returns the existing workspace, never a duplicate).
  Tests: `tests/smoke/test_onboarding_smoke.py` (model + route),
  `tests/integration/test_onboarding_provision.py` (auth + no-duplicate guard).
- feat(contacts): Contacts module — counterparties (customers/vendors) with a
  list/search page (`/contacts`, Books nav) and an **Import** flow. Two sources
  into one review-then-confirm screen (tag customer/vendor/skip; nothing
  written until confirmed; idempotent upsert matched on contact_id → email →
  name):
  - **File**: vCard (`.vcf` — macOS/iPhone/Google/Outlook) or CSV. Pure parsers
    (`app/contacts/import_parse.py`) auto-detect format + map varied headers.
  - **Contact-Ops sync** (gated go-live): pull the caller's people from
    Contact-Ops via uchub RFC-8693 token-exchange → MCP `list/search_people`,
    carrying `external_refs.contact_id` for cross-app dedup. Dormant until a
    dedicated uchub client + audience + tenant alignment are configured (button
    hidden, endpoint 503s cleanly). See `docs/CONTACT-OPS-SYNC.md`
    (depends on P-00062 KC token-exchange upgrade + P-00059 tenant confirm).
  - API: `GET/POST /counterparties`, `/parse`, `/import`, `/sources`,
    `/sync-contact-ops`. 15 unit tests (parsers + JSON-RPC unwrap + mapping).
- feat(cpa): cross-client onboarding. `POST /api/me/clients` provisions a new
  client workspace, adds the caller as a `cpa` member (runs the books, cannot
  self-grant), and optionally emails the business owner an `owner` invite.
  `GET /api/me/persona` drives a persona-aware `/start` landing (CPAs → the
  `/clients` practice view). "Add client" surfaced in the Clients page + empty
  states. Fixed a latent bug: `setup_assistant.create_workspace` referenced a
  non-existent `workspace_membership.status` column.
- security(C3): DB-enforced tenant isolation + ledger immutability.
  - New least-privilege runtime role `accounting_ops_app` (created by
    migrations; SELECT/INSERT broad, UPDATE on mutable tables, DELETE only on
    `category_override`). It is NOT the table owner and is NOSUPERUSER/NOBYPASSRLS,
    so it cannot UPDATE/DELETE/TRUNCATE or DISABLE TRIGGER the append-only ledger.
    The owner role `accounting_ops` is now used only by migrations + provisioning.
  - `BEFORE TRUNCATE` statement triggers on `journal_entry`, `event_log`,
    `credential_access_log` (row triggers don't fire on TRUNCATE).
  - Row-Level Security on every workspace-scoped table, keyed on the
    `app.current_workspace` GUC the app sets per request — a missed/stale
    search_path now yields zero rows instead of another tenant's data.
  - Migrations: `migrations/tenant/0005_rls_and_runtime_grants`,
    `migrations/public/0003_rls_and_runtime_grants` (both reversible).
  - App: `workspace_session`/`entity_session` set the `app.current_workspace`
    (+ `app.current_entity`) GUC; credential store paths set it too. New
    `ADMIN_DATABASE_URL` for owner-only migrations/provisioning (falls back to
    `DATABASE_URL`). New `accounting_ops_app` wiring in docker-compose + DEPLOY.
  - Tests: `tests/integration/test_db_rls_roles.py` proves the role cannot
    mutate/truncate the ledger or disable its trigger, and that RLS blocks
    cross-tenant reads (wrong/unset GUC) + cross-workspace inserts.

## 0.1.0 - Phase 0

- Scaffolded Accounting-Ops repository for P-00050.
- Added Docker Compose topology, FastAPI core, Alembic schema, MCP/A2A entrypoints, Keycloak scripts, OpenAPI contract, Next.js web shell, and smoke tests.
- Left Phase 1 connector, Brigade, receipt-processing, and payment execution behavior as explicit stubs.
