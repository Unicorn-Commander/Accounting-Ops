# Accounting-Ops Community Edition — publish checklist

This directory is a **prepared-but-unpublished** open-core Community Edition
snapshot. It is one clean squashed commit with **no git remote** — nothing has
been pushed anywhere. The maintainer pushes the button.

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

## Remaining steps for the publish click (maintainer's call)
1. Create a new **private** GitHub/Forgejo repo; push this `main`.
2. Wire the two scanners as CI (config already present) + a DCO check.
3. Flip the repo public when ready. **Launch line:** "the first open-source
   autonomous AI accountant."
4. Satellites: create public remotes for the 3 local-only MCP repos
   (simplefin-mcp, ebay-finances-mcp, reconciler) — all 6 already carry AGPL LICENSE.
5. Keep the Forgejo repo canonical and configure the private GitHub repo as a
   push mirror; flip both public only when the maintainer gives the go.
