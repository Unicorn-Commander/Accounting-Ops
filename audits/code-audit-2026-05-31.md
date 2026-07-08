# Accounting-Ops Code Audit - 2026-05-31

## Executive summary
Accounting-Ops has a solid security spine: workspace schema isolation, RLS backstops, append-only grants, AES-GCM credential storage, and shared posting primitives are all directionally right. The blocker is not the architecture; it is a handful of bypasses and unfinished auth surface that cut around that spine. Overall grade: **C+ for internal use, not safe for external customers/CPAs yet**. Fix the COA entity-scope bypass, auth-chain/PAT gap, closed-period recategorization gap, bulk-approval cap, and public migration branch before onboarding anyone outside Magic Unicorn.

## CRITICAL (must fix before any external customer)
- Bug 1: COA REST endpoints bypass entity_scope.
  - File: `app/api/journal.py:149`, `app/api/journal.py:175`, `app/api/journal.py:241`, `app/api/journal.py:322`
  - What's wrong: `list_coa` has no `require_role()` and no `assert_entity_in_scope()`. Create/update/delete require accountant role, but never check that `body.entity_id` or the account's owning entity is inside the member's `entity_scope`.
  - Why it matters: A scoped external CPA/bookkeeper in workspace A can read, create, rename, reclassify, or deactivate COA accounts for another entity in the same workspace. Because posting uses COA canonical forms, this can corrupt books even without direct journal writes.
  - Suggested fix: Add `require_role(workspace, "viewer")` and `assert_entity_in_scope(workspace, entity_id)` to `list_coa`; add `assert_entity_in_scope(workspace, body.entity_id)` before create; for update/delete, fetch the account's `entity_id` first, then assert it before mutation. Add integration tests with a scoped membership targeting an out-of-scope account.
  - Estimated effort: S

- Bug 2: COA MCP update bypasses entity_scope.
  - File: `app/tools/coa.py:205`, `app/tools/coa.py:217`
  - What's wrong: `update_coa_account()` authorizes `workspace_id` with `entity_id=None`, then updates any account id in the workspace.
  - Why it matters: The MCP surface is meant to be agent-safe. A scoped CPA agent can mutate another entity's chart of accounts by guessing or learning an account UUID.
  - Suggested fix: Load the account's `entity_id`, call `assert_entity_in_scope(ctx, loaded_entity_id)`, then update. Better: change the tool signature to require `entity_id` and filter `WHERE id=:aid AND entity_id=:eid`.
  - Estimated effort: S

- Bug 3: Documented auth chain is not implemented; PAT and SSO-header auth are missing.
  - File: `app/auth.py:243`, `app/auth.py:255`, `app/auth.py:281`, `app/auth.py:340`
  - What's wrong: The implemented chain is service token -> Brigade precheck -> Keycloak -> Brigade retry. There is no SSO-header strategy and no PAT verification path. Repository search found only manifest/docs references to `aops_pat_`, not a `personal_access_token` verifier/table use.
  - Why it matters: The public discovery manifest advertises PAT auth, and external automation will fail or be routed into service-token workarounds. More importantly, the intended precedence and threat model are not the code that is running.
  - Suggested fix: Implement the exact chain in one function: trusted SSO header only from a trusted proxy/network, Keycloak bearer, service token JSON, PAT hash lookup using `secrets.compare_digest`, Brigade JWT. Add negative tests for malformed JSON, wrong PAT hash, expired PAT, and strategy fallthrough.
  - Estimated effort: M

- Bug 4: Closed-period enforcement misses recategorization reversal/restatement.
  - File: `app/journal/posting.py:657`, `app/journal/posting.py:681`, `app/journal/posting.py:721`, `app/journal/posting.py:1039`
  - What's wrong: `post_journal_entry()` checks `closed_period`, but `_apply_recategorization()` directly inserts reversal and restatement rows using the original effective date without checking `closed_period`.
  - Why it matters: A user can change closed-period books through `categorize_transaction` or approval of a held recat. That violates the period-close guarantee and can change filed tax-period numbers after lock.
  - Suggested fix: Add a shared `assert_period_open(session, entity_id, effective_date)` used by both `post_journal_entry()` and `_apply_recategorization()`. Return `PostingRefusal("blocked_period_closed", ...)` so approval decisions roll back cleanly. Add tests for tier-L recat and approved material recat inside a closed period.
  - Estimated effort: S

- Bug 5: Public Alembic migrations have two heads.
  - File: `migrations/public/versions/0008_rbac_expansion.py:32`, `migrations/public/versions/0008_regulatory_feed.py:26`
  - What's wrong: Both public migrations declare `down_revision = "0007_tax_chunking"`.
  - Why it matters: `alembic upgrade head` against the public schema can fail with multiple heads or apply only one branch depending on invocation. Fresh deploys and your-host drift checks are not deterministic.
  - Suggested fix: Renumber one migration, set its `down_revision` to the other `0008_*` revision, then verify `alembic -c alembic-public.ini heads` returns one head. Check production's `alembic_version_public` before applying.
  - Estimated effort: S

## HIGH (fix within sprint)
- Bug 6: Bulk approval cap is documented but not enforced.
  - File: `app/tools/approvals.py:253`, `app/tools/approvals.py:259`, `app/tools/approvals.py:302`
  - What's wrong: `bulk_decide_approvals()` validates only that `decisions` is a list, then iterates every item and returns `total=len(decisions)`. No server-side 200 cap exists.
  - Why it matters: A buggy UI or agent can run thousands of per-item transactions, hammer the hash chain, and create long-running approval batches. The advertised cap gives a false safety signal.
  - Suggested fix: Reject `len(decisions) > 200` before authorization work starts, and add a smoke/integration test with 201 items.
  - Estimated effort: S

- Bug 7: Service tokens are still a full-impersonation footgun when scope fields are omitted.
  - File: `app/auth.py:251`, `app/auth.py:260`, `app/auth.py:265`
  - What's wrong: If a service token JSON entry omits `workspace_ids`, `entity_ids`, and `role_floor`, it receives the impersonated user's full workspace role.
  - Why it matters: One leaked worker token can become owner/admin across every workspace that the impersonated user can reach. This is especially risky once external CPA/customer workspaces exist.
  - Suggested fix: Require scoped service-token fields in production. Treat missing `role_floor` or `workspace_ids` as invalid unless an explicit `allow_unscoped_service_token` env flag is set for dev.
  - Estimated effort: M

- Bug 8: eBay access tokens are logged, even if redacted.
  - File: `app/connectors/ebay/client.py:229`, `app/connectors/ebay/client.py:289`, `app/connectors/ebay/client.py:314`
  - What's wrong: Refresh failures log `old_token=<first4>..<last4>` and successful refreshes log `new_token=<first4>..<last4>`.
  - Why it matters: Bearer tokens should not appear in stdout at all. Prefix/suffix leakage is still useful for correlating stolen tokens and violates the "no raw credential material in logs" rule.
  - Suggested fix: Log only credential id/workspace id, status, and expiry seconds. Remove token material from `_redact()` call sites.
  - Estimated effort: S

- Bug 9: Hash-chain race handling drops writes instead of retrying.
  - File: `app/journal/posting.py:348`, `app/journal/posting.py:383`, `app/journal/posting.py:747`
  - What's wrong: Comments say concurrent posts should retry with a fresh `prev_hash`, but the code catches `IntegrityError` and returns `blocked_idempotent`.
  - Why it matters: Concurrent imports can incorrectly look idempotent and leave legitimate transactions unposted. At external-customer volume, this becomes data loss unless callers detect and retry themselves.
  - Suggested fix: Retry chain advance a bounded number of times on `(entity_id,this_hash)` conflict. Keep duplicate source id handling separate from chain conflict handling.
  - Estimated effort: M

- Bug 10: Sidecar auth for `get_form_1120_data` does not forward the caller token.
  - File: `app/tools/tax_get_form_1120_data.py:39`, `app/tools/tax_get_form_1120_data.py:52`
  - What's wrong: The tool authorizes the MCP caller locally, then calls cpa-export with `ACCTOPS_SERVICE_TOKEN`/`CPA_EXPORT_TOKEN` if present. The comment admits the raw caller bearer is unavailable.
  - Why it matters: The sidecar is not independently validating the actual user. If its own RBAC differs or a service token is overbroad, it can return data outside the caller's scope.
  - Suggested fix: Store the validated raw bearer in the MCP context for internal forwarding, or require the sidecar to trust a signed internal assertion containing workspace/entity/user/scope generated after local auth.
  - Estimated effort: M

## MEDIUM (fix when convenient)
- Bug 11: Discovery metadata is stale and defaults missing tools to admin/write.
  - File: `app/api/discovery.py:52`, `app/api/discovery.py:91`
  - What's wrong: `_TOOL_METADATA` covers only a subset of the 60+ registered MCP tools. Missing tools log warnings and appear as admin/write/800ms.
  - Why it matters: Brigade consumers get inaccurate tool capabilities, noisy logs hide real warnings, and read-only tools may be over-gated.
  - Suggested fix: Generate metadata from decorators or a central tool registry used by both `authorize_workspace()` and discovery. Until then, add every registered tool to `_TOOL_METADATA`.
  - Estimated effort: M

- Bug 12: Tax-status dashboard is N+1 over entities x years x 3 queries.
  - File: `app/api/tax_status.py:293`, `app/api/tax_status.py:315`, `app/api/tax_status.py:333`
  - What's wrong: For every entity and every year, the endpoint runs queue summary, approval summary, and latest specialist-run queries.
  - Why it matters: Fine at 4 entities. Slow at 50 entities x 5 years and noisy under CPA dashboards.
  - Suggested fix: Replace the nested awaits with three grouped queries over all visible entity ids and tax years, then assemble in memory.
  - Estimated effort: M

- Bug 13: Report queries scan and compute account prefixes at runtime.
  - File: `app/api/reports.py:201`, `app/api/reports.py:203`, `app/api/reports.py:212`
  - What's wrong: P&L/cash-flow/trial-balance patterns use `UNION ALL` over journal rows and `split_part()` on debit/credit account strings.
  - Why it matters: At 50K+ journal entries per customer, repeated scans and string prefix extraction become the first dashboard pain point.
  - Suggested fix: Add generated/stored account number columns or a journal-line table, plus indexes on `(entity_id, effective_date, status)` and line account number. Consider materialized monthly balances later.
  - Estimated effort: L

- Bug 14: Frontend still uses floating-point money parsing in several workflow paths.
  - File: `web/app/journal/new/manual-entry-form.tsx:90`, `web/components/queue-row-actions.tsx:84`, `web/components/transactions-table.tsx:95`
  - What's wrong: UI converts dollar strings with `parseFloat()` and `Math.round()`, and aggregates displayed money as JS numbers.
  - Why it matters: Backend cents/Decimals are mostly safe, but client-side approval/manual-entry calculations can round edge cases differently from accounting expectations.
  - Suggested fix: Parse money inputs with a decimal/cents parser that accepts only two decimal places, returns integer cents, and never uses binary floats.
  - Estimated effort: S

## LOW / nice-to-have
- Bug 15: COA UI comments are stale after the proxy flip.
  - File: `web/app/coa/coa-editor.tsx:58`, `web/app/api/coa/[workspaceId]/[entityId]/route.ts:1`
  - What's wrong: The component says `/api/coa` is shadowed by Traefik, but the specific Next.js proxy exists and the new catch-all only covers `/api/workspaces/*`.
  - Why it matters: This kind of stale operational comment sends future debugging in the wrong direction.
  - Suggested fix: Update the comment and decide whether `/api/coa/*` should remain as a compatibility proxy or move the UI to `/api/workspaces/.../coa`.
  - Estimated effort: S

- Bug 16: Mail logs include invite recipient emails and subjects.
  - File: `app/mailer.py:155`, `app/mailer.py:188`, `scripts/drain_email_outbox.py:280`, `scripts/drain_email_outbox.py:287`
  - What's wrong: Logs include `to=` and `subject=`.
  - Why it matters: Not a secret, but it is customer/CPA PII in stdout. External customers raise the bar.
  - Suggested fix: Log outbox id, domain, and status; keep full email/subject only in `email_outbox`.
  - Estimated effort: S

## What's GOOD (don't change)
- The workspace session pattern is strong: `SET LOCAL search_path` plus `app.current_workspace` GUC is set per transaction and auto-resets on commit/rollback (`app/workspace_session.py:85`).
- Tenant RLS and runtime grants are the right shape: append-only tables get SELECT/INSERT only, RLS is enabled, and runtime role is explicitly non-BYPASSRLS (`migrations/tenant/versions/0005_rls_and_runtime_grants.py:159`, `migrations/tenant/versions/0005_rls_and_runtime_grants.py:233`).
- Credential encryption is well designed: no auto-generation, tenant-bound HKDF, random 12-byte nonces, and tenant+credential AAD (`app/security/crypto.py:59`, `app/security/crypto.py:74`, `app/security/crypto.py:98`).
- Brigade JWT verification is strict on shape, alg allowlist, kid refresh, issuer, audience, and time claims (`app/security/brigade_jwt.py:311`, `app/security/brigade_jwt.py:327`, `app/security/brigade_jwt.py:371`).
- Specialist runners mostly use the shared `MAX_OUTPUT_TOKENS = 16384` and `resolve_llm_api_key()`; the coordinator intentionally has small max-token calls.

## Test gaps
- No scoped-membership tests for COA REST or MCP mutation. Add out-of-scope read/create/update/delete tests.
- No PAT tests because PAT auth is not implemented.
- Bulk approval lacks a 201-item rejection test.
- Closed-period tests cover direct posting but not recategorization reversal/restatement or approved held recats.
- Migration tests did not catch public multiple heads.
- eBay token logging/privacy should have a static regression test that rejects token material in log format strings.

## Performance projections
- First pain point: `/api/tax-status` as entity count grows because it runs three queries per entity-year.
- Second pain point: reports over `journal_entry` because account classification is derived from canonical-form strings at query time.
- Third pain point: hash-chain writes under concurrent imports. Without retry, concurrency causes false idempotent refusals rather than smooth throughput.
- At 50K entries per customer, add grouped tax-status queries and journal indexes/materialized monthly summaries before adding more dashboard widgets.

## Recommended next investments (top 3)
1. Close the tenant/entity-scope holes and auth-chain gaps: COA BOLA, PAT/SSO implementation, service-token scoping.
2. Make ledger invariants universal: shared period-open guard, bounded hash-chain retry, and tests around every path that writes journal entries.
3. Stabilize deploy/migration operations: linear public Alembic chain, production `alembic current` checklist, and a read-only drift script for your-host.
