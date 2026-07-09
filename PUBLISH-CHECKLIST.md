# Accounting-Ops Community Edition — publish checklist

This directory is a **prepared-but-unpublished** open-core Community Edition
snapshot. It is one clean squashed history that now lives in **private** repos
(Forgejo canonical + a GitHub push-mirror, both private) — nothing is public.
The maintainer pushes the button.

## What this snapshot is
- Built from the internal monorepo (`Accounting-Ops`, branch
  `feat/community-edition-prep`) as a fresh single-commit history — **not** the
  340-commit / ~90-branch internal history.
- AGPL-3.0-or-later. Defaults to `AO_EDITION=community` +
  `AO_AUTH_MODE=local` so it boots self-host out of the box.

## Gates passed (re-runnable)
- **gitleaks** (`.gitleaks.toml` in repo): `no leaks found` (exit 0). The config
  allowlists only synthetic test fixtures + one settings-read variable.
- **trufflehog** (`--no-update filesystem . --only-verified`): **0 verified
  secrets** and **0 unverified secrets**. Local temp-cleanup warnings on macOS
  are non-fatal after scan completion.
- **PII gate** (owner name / EIN / CPA / entities / tenant UUIDs / internal
  hosts+IPs+domains): empty across the whole tree.
- **Self-host smoke**: `make selfhost` builds and provisions locally; `make demo`
  seeds both generic sample workspaces; API smoke posts a categorized transaction
  and renders a 2026 P&L.
- No `.env`, `*.pem`, `*.key`, or `secrets/` files present; `.gitignore` covers them.

## What was EXCLUDED (97 files) and why
- **Enterprise-hold code** (the commercial moat): `app/platform/`,
  `app/api/{clients,work_inbox,discovery,test_brigade_jwt}.py`,
  `app/security/{litellm_admin,brigade_client,brigade_jwt}.py`,
  `app/contacts/contact_ops_client.py`, `app/ar/entitlement.py`,
  `web/app/clients/**`, enterprise-bridge migrations `0012/0013`, and their
  8 enterprise smoke tests.
- **Owner real work products**: entire `drafts/` tree (real reconciliations,
  SCDOR cure letter, spouse SSDI memo), `agents/**/worked_examples/` (real MU-Inc
  figures + veteran VA/CRDP/CRSC financials), `agents/_shared/coa_catalogs/`.
- **Internal design/runbook docs**: `agents/_shared/*.md` (mail, permissions,
  brigade, pat-minting, connector runbooks…), internal `docs/*` (MOHSIN_REVIEW_GUIDE,
  ARCHITECTURE, OPERATIONS, PROD-RING-RUNBOOK, STALWART/PHASE/USER-GUIDE/…).
- **Dogfood scripts**: `scripts/extend_coa_mu_inc*.sql`, `ebay_oauth_bootstrap.py`,
  `ingest_bofa_live.py`, `preflight_deploy.py`; internal `docker-compose.yml`
  (self-host uses `docker-compose.selfhost.yml`), `DEPLOY.md`, `CHANGELOG.md`,
  `COMMUNITY_MANIFEST.md`.

## Private prep — DONE (held for the public flip)
- ✅ **Forgejo canonical** (private): `UnicornCommander/accounting-ops-community` — `main` pushed.
- ✅ **GitHub mirror** (private): `Unicorn-Commander/accounting-ops-community` — `main` pushed
  (parity), and a Forgejo **push-mirror** (`sync_on_commit`, 8h) keeps it in sync.
- ✅ **CI committed** (`.github/workflows/ci.yml`): gitleaks + trufflehog + DCO sign-off gate.
- ✅ **Satellites** (private, Forgejo canonical + GitHub mirror each):
  `accounting-ops-{simplefin-mcp,ebay-finances-mcp,reconciler}` — all AGPL-licensed.

## The only steps left (maintainer's call)
1. **Flip public** — make the Forgejo + GitHub `accounting-ops-community` repos public
   (the mirror keeps them in sync). **Launch line:** "the first open-source autonomous AI accountant."
2. Before flipping the **satellites** public, human-eyeball their binary test fixtures for PII
   (`ebay-finances-mcp/**/*_response.json`, any `.eml`/`.pdf` fixtures) and run the scanners on them.
3. Recommended: swap the push-mirror credential (currently a broad personal PAT) for a
   repo-scoped deploy token before/at publish.
4. Optional: add the same CI workflow to the satellites; add an in-app persistent
   "Draft — not tax advice" banner (the not-tax-advice posture already lives in
   README + DISCLAIMER + PROFESSIONAL-SUBSTITUTE).
