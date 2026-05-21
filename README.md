# Accounting-Ops

Accounting-Ops is an agent-first, MCP-first financial operations platform for Project-Ops record P-00050. Brigade agents do categorization, reconciliation, compliance monitoring, CPA bundle preparation, and payment proposals; the app is the observation, approval, and chat surface.

Positioning anchor from the design doc: **Your books. Your machine. Your rules.**

## Architecture

```text
External agents
  Claude / ChatGPT / Copilot / Brigade peers
          |
          v
  accounting.magicunicorn.dev
    +-----------------------------+
    | MCP server  | A2A endpoint  |
    | Keycloak uchub OAuth/OBO    |
    +-------------+---------------+
                  |
                  v
    Accounting-Ops FastAPI orchestrator surface
       |              |                 |
       v              v                 v
  Postgres 16     FalkorDB bigboy    Garage S3
  event log       tenant subgraph    receipt blobs
  journal         graph memory       CPA bundles
       |
       v
  lilguy1 DR + centerdeep OTel
```

## Local Development

Prerequisites:

- Python 3.12
- Docker Compose v2
- Node 22 for the web shell
- Keycloak `kcadm.sh` only when provisioning uchub clients

Setup:

```bash
cp .env.example .env
# Fill POSTGRES_PASSWORD, GARAGE_RPC_SECRET, GARAGE_ADMIN_TOKEN, NEXTAUTH_SECRET, and Keycloak secrets.
docker compose up -d postgres garage
alembic upgrade head
python scripts/seed_local.py
docker compose up app web
```

The web shell runs at `http://localhost:3000` in local development. The API health probe is `http://localhost:8000/healthz`.

## Vendor Skills

Anthropic financial-services scaffolding is vendored read-only at:

```text
/Volumes/Studio Storage/Development/Accounting-Ops/vendor/anthropic-financial-services/plugins/vertical-plugins/fund-admin/skills/
```

Phase 0 references those skills as seeds only. Accounting-Ops does not modify vendor files or author new Skills prompts in this repo.

## Design And Tracking

- Design doc: `/Users/aaronstransky/Documents/Accounting-Ops-Design-v1.md`
- Project-Ops record: `P-00050`
- Deployment target: `midboy1`, Traefik route `accounting.magicunicorn.dev`

## Phase 0 Scope

This tree contains the durable platform scaffold: Docker Compose, Postgres/Alembic schema, FastAPI auth/tenancy/OTel shell, MCP and A2A entrypoints, Keycloak setup scripts, OpenAPI contract, Next.js App Router shell, and smoke tests. Phase 1 implements real connectors, Brigade wiring, receipt processing, and tool behavior.
