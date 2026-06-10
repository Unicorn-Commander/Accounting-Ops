# Changelog

## Unreleased

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
