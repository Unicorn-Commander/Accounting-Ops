# Changelog

All notable changes to Accounting-Ops Community Edition are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [0.8.0] — Community Edition

The first public open-source release: the **first open-source autonomous AI accountant**.

### Added
- **Open-core Community Edition** — a single-workspace, self-hostable, AGPL-3.0 build that runs
  entirely on your own hardware with your own models.
- **Local single-admin auth** (`AO_AUTH_MODE=local`) — a self-hoster can log in with no external
  identity provider.
- **One-command self-host** — `make selfhost` provisions Postgres, Garage, a bundled Ollama, the
  app, and the web UI; generates secrets; and seeds a workspace + chart of accounts.
- **Bring-your-own-model** — defaults to a bundled Ollama; point `LLM_ENDPOINT_URL` / `LLM_MODEL`
  at any OpenAI-compatible endpoint. The deterministic Tier-1 categorizer runs with no model at all.
- Public documentation: quickstart README, [ARCHITECTURE](docs/ARCHITECTURE.md),
  [PROFESSIONAL-SUBSTITUTE](docs/PROFESSIONAL-SUBSTITUTE.md), DISCLAIMER, CONTRIBUTING (DCO),
  SECURITY, and CODE_OF_CONDUCT.

### Core capabilities in this release
- Append-only, per-entity SHA-256 hash-chained general ledger with `verify_chain`.
- Deterministic-first categorization with an optional model fallback that is always review-gated.
- Materiality gate (auto-post / confirm / hard-block / receipt-required) at posting time.
- Full provenance: every figure traces report line → journal → source transaction → agent decision.
- P&L / Balance Sheet / Cash Flow rendered in-app; CSV / IIF / QBO XML / PDF CPA exports.
- Tax-knowledge Q&A and **draft** tax-form packages (watermarked *do not file*).
- MCP (`/mcp`) and A2A (`/a2a`) surfaces so external agents operate the ledger like the UI does.

### Not enabled in Community Edition (by design)
- No electronic filing (IRS MeF / state portals), no EFTPS payments, no e-signature submission,
  and no execution of trades, transfers, or payments. See the [DISCLAIMER](DISCLAIMER.md).

[0.8.0]: https://semver.org/
