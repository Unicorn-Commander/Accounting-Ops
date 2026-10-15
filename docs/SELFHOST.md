# Community Edition — self-host quickstart

**Your books. Your machine. Your rules.**

This guide covers the fleet-free Community Edition stack
(`docker-compose.selfhost.yml`): Postgres + Garage + Ollama + FastAPI + Next.js,
with local single-admin auth (no Keycloak) and bring-your-own model.

For the one-command path, see the root [README](../README.md#quickstart).
Operator detail for the optional Sentinel daemon is in
[`agents/_shared/sentinel_runbook.md`](../agents/_shared/sentinel_runbook.md).

---

## Reproducible application images

The backend image installs its runtime dependencies from the committed
`uv.lock` with uv `0.9.30`, including the `tax-agents` extra. Keep
`pyproject.toml` and `uv.lock` together. To intentionally refresh dependencies,
change the constraints in `pyproject.toml`, run `uv lock`, review the resulting
lockfile diff, and verify it with `uv lock --check` before building the image.

---

## One command

```bash
git clone <this-repo> accounting-ops && cd accounting-ops
make selfhost
```

Then open **http://localhost:3000**, sign in with the admin you configured, and
pull a vision model for categorization / receipt OCR:

```bash
docker compose -f docker-compose.selfhost.yml exec ollama ollama pull qwen2.5vl:7b
```

---

## Manual bring-up

```bash
cp .env.example .env
# Fill the app settings below (and compose secrets — see bootstrap script).
python scripts/gen_local_admin_hash.py   # web-only; paste into .env
python -m scripts.bootstrap_master_key --path secrets/accounting_ops_master_key

docker compose -f docker-compose.selfhost.yml up -d --build
# migrate + provision workspace (see README "Manual setup")
```

Compose also needs non-app secrets that the containers wire in (Postgres password,
Garage RPC/admin tokens, NextAuth secret, local admin password hash). Generate
those with `make selfhost` or follow `scripts/selfhost_bootstrap.sh`.

---

## App environment (from `app/config.py`)

Only variables read by `app/config.py` are listed here. Defaults match
Community Edition self-host when the stack hard-sets them in
`docker-compose.selfhost.yml`.

### Edition + local auth

| Env | Default in code | CE self-host |
|---|---|---|
| `AO_EDITION` | `enterprise` | **`community`** — unmounts enterprise routers, hides enterprise nav |
| `AO_AUTH_MODE` | `keycloak` | **`local`** — single admin, no Keycloak |
| `AO_LOCAL_AUTH_SECRET` | _(empty)_ | **required** in local mode (HS256 shared with web) |
| `AO_LOCAL_ADMIN_UC_UID` | `admin` | must match workspace membership `uc_uid` |
| `AO_LOCAL_ADMIN_EMAIL` | _(empty)_ | informational; set to your login email |
| `ACCEPTED_AUDIENCE` | `accounting-ops-app` | must match the local bearer audience |
| `KEYCLOAK_CLIENT_ID` | `accounting-ops-app` | unused when `AO_AUTH_MODE=local` |

### Database + public URL

| Env | Notes |
|---|---|
| `DATABASE_URL` | Runtime Postgres URL (`postgresql+asyncpg://…`) |
| `ADMIN_DATABASE_URL` | Owner/DDL URL for migrations + provisioning; falls back to `DATABASE_URL` |
| `PUBLIC_BASE_URL` | e.g. `http://localhost:3000` |

### Object storage (Garage)

| Env | Notes |
|---|---|
| `GARAGE_S3_ENDPOINT` | default `http://garage:3900` |
| `GARAGE_S3_ACCESS_KEY` / `GARAGE_S3_SECRET_KEY` | blank = uploads return 503 |
| `GARAGE_S3_REGION` | default `garage` |
| `GARAGE_BUCKET_PREFIX` | default `accounting-ops` |
| `DOCUMENT_UPLOAD_MAX_BYTES` | max single upload size |

### Bring-your-own model (categorizer / receipt VLM)

Point these at any OpenAI-compatible endpoint. Self-host defaults target the
bundled Ollama service. **Use a vision-capable model** for receipt OCR.

| Env | CE default (compose) | Purpose |
|---|---|---|
| `LLM_ENDPOINT_URL` | `http://ollama:11434/v1` | OpenAI-compatible base URL |
| `LLM_MODEL` | `qwen2.5vl:7b` | model id (vision for receipts) |
| `LLM_API_KEY` | _(blank)_ | local Ollama needs none |
| `LLM_TIMEOUT_S` | `30` | request timeout |
| `LLM_MAX_TOKENS` | `512` | completion ceiling |

Optional categorizer overrides (inherit `LLM_*` when blank):

| Env | Purpose |
|---|---|
| `CATEGORIZER_LLM_ENDPOINT_URL` | text categorizer endpoint |
| `CATEGORIZER_LLM_MODEL` | text categorizer model |
| `CATEGORIZER_LLM_API_KEY` | categorizer API key |
| `CATEGORIZER_LLM_TIMEOUT_S` | categorizer timeout |
| `CATEGORIZER_FALLBACK_MODEL` | blank = no escalation |
| `CATEGORIZER_FALLBACK_ENDPOINT_URL` | frontier fallback endpoint |
| `CATEGORIZER_FALLBACK_API_KEY` | frontier fallback key |
| `CATEGORIZER_ESCALATE_BELOW_CONFIDENCE` | default `0.60` |
| `OCR_BBOX_SERVICE_URL` | blank = VLM-only (no bbox OCR) |
| `QWEN_LOCAL_URL` / `QWEN_VLM_URL` | legacy local/VLM URLs (default Ollama) |

Examples:

```bash
# Bundled Ollama (default)
LLM_ENDPOINT_URL=http://ollama:11434/v1
LLM_MODEL=qwen2.5vl:7b
LLM_API_KEY=

# Remote OpenAI-compatible gateway
LLM_ENDPOINT_URL=https://my-gateway.example/v1
LLM_MODEL=my-vision-model
LLM_API_KEY=sk-...
```

### Optional graph / observability

| Env | Notes |
|---|---|
| `FALKORDB_HOST` / `FALKORDB_PORT` / `FALKORDB_PASSWORD` | agent-memory projection; no-op if absent |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_SERVICE_NAME` | optional tracing |
| `DEFAULT_WORKSPACE_ID` / `DEFAULT_ENTITY_ID` | single-tenant convenience (`TENANT_ID` aliases workspace) |

### Hosted / enterprise knobs (leave alone on CE)

These exist in `app/config.py` but Community Edition self-host should not need
them. Smart gate and gateway routing stay off; community edition is exempt from
the smart gate regardless of `AO_SMART_GATE_ENABLED`.

| Env | CE guidance |
|---|---|
| `AO_ROUTE_CHAT_THROUGH_GATEWAY` | leave unset / false |
| `LLM_GATEWAY_*` / `LLM_PLATFORM_VIRTUAL_KEY` | leave blank |
| `AO_CHAT_LOCAL_*` | optional local chat tier |
| `NARRATIVE_LLM_*` | frontier narrative path (optional BYO) |
| `OPS_CENTER_*` | enterprise billing portal |
| `AO_SMART_GATE_ENABLED` | inert when `AO_EDITION=community` |
| `CONTACT_OPS_*` / `ACCTOPS_TRUSTED_M2M_CALLERS` | federation / M2M |
| `KEYCLOAK_REALM_URL` / `KEYCLOAK_CLIENT_SECRET` | only if `AO_AUTH_MODE=keycloak` |
| `CPA_EXPORT_URL` | CPA export sidecar (enterprise compose profile) |

---

## Optional compose profiles

| Profile | Service | What it adds |
|---|---|---|
| `embeddings` | Infinity | embedding backend for Tax Knowledge RAG |
| `graph` | FalkorDB | best-effort agent-memory projection |
| `sentinel` | Sentinel | autonomous CPA checks (memos / flags / reminders) |

```bash
# Core stack only (default)
docker compose -f docker-compose.selfhost.yml up -d

# With optional add-ons
docker compose -f docker-compose.selfhost.yml \
  --profile embeddings --profile graph --profile sentinel up -d
```

**Sentinel** is off unless you pass `--profile sentinel`. Global kill switch:
`SENTINEL_DISABLED=true` (read by the daemon process, not `app/config.py`).
Per-workspace pause: set `sentinel_config.autonomy_level='off'` (or use
Settings → Sentinel in the UI). See the [sentinel runbook](../agents/_shared/sentinel_runbook.md).

---

## Verify

```bash
# Resolve the default stack (no optional profiles)
docker compose -f docker-compose.selfhost.yml config --services
# expect: postgres garage ollama app web

# Resolve with Sentinel opted in
docker compose -f docker-compose.selfhost.yml --profile sentinel config --services
# expect: … + sentinel
```

Health endpoints once up: app `http://localhost:8000/healthz`, web
`http://localhost:3000/login`.
