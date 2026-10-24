#!/usr/bin/env bash
# =============================================================================
# Accounting-Ops — Community Edition one-command self-host bootstrap.
#
#   Your books. Your machine. Your rules.
#
# Idempotent. Safe to re-run. It will:
#   1. Create .env from .env.example (if missing).
#   2. Generate strong secrets into .env (Postgres, Garage, NextAuth, local-auth).
#   3. Set AO_EDITION=community + AO_AUTH_MODE=local.
#   4. Take the local admin password (AO_LOCAL_ADMIN_PASSWORD, else a no-echo
#      prompt on a terminal, else generate one and show it once) and store
#      only its scrypt hash.
#   5. Generate the credential master key (secrets/accounting_ops_master_key).
#   6. Build + start the self-host stack (postgres, garage, ollama, app, web).
#   7. Run migrations, provision one workspace + entity, grant the admin owner
#      membership, and seed a default chart of accounts.
#
# Usage:  scripts/selfhost_bootstrap.sh          (or: make selfhost)
# Env you may pre-set: AO_COMPANY_NAME, AO_ENTITY_KIND, AO_LOCAL_ADMIN_UC_UID,
#                      AO_LOCAL_ADMIN_EMAIL, AO_LOCAL_ADMIN_PASSWORD
#                      (the plaintext is never written to .env)
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=".env"
COMPOSE=(docker compose -f docker-compose.selfhost.yml)

COMPANY_NAME="${AO_COMPANY_NAME:-My Company}"
ENTITY_KIND="${AO_ENTITY_KIND:-llc}"
ADMIN_UC_UID="${AO_LOCAL_ADMIN_UC_UID:-admin}"
ADMIN_EMAIL="${AO_LOCAL_ADMIN_EMAIL:-admin@example.com}"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

rand() { openssl rand -hex 32; }

slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//'
}

# get_env KEY -> prints current value from .env (empty if unset/blank)
get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# set_env KEY VALUE -> idempotent upsert (replaces the line or appends)
set_env() {
  local key="$1" val="$2"
  if grep -qE "^$key=" "$ENV_FILE" 2>/dev/null; then
    # Use a temp file to avoid sed -i portability issues (macOS vs GNU).
    grep -vE "^$key=" "$ENV_FILE" > "$ENV_FILE.tmp"
    mv "$ENV_FILE.tmp" "$ENV_FILE"
  fi
  printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
}

# gen_if_blank KEY -> set KEY to a random secret only if currently blank
gen_if_blank() {
  local cur; cur="$(get_env "$1")"
  if [ -z "$cur" ]; then set_env "$1" "$(rand)"; log "generated $1"; fi
}

# ---------------------------------------------------------------------------
# 1. .env
# ---------------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"
  log "created .env from .env.example"
fi

# ---------------------------------------------------------------------------
# 2. secrets
# ---------------------------------------------------------------------------
gen_if_blank POSTGRES_PASSWORD
gen_if_blank APP_DB_PASSWORD
gen_if_blank GARAGE_RPC_SECRET
gen_if_blank GARAGE_ADMIN_TOKEN
gen_if_blank NEXTAUTH_SECRET
gen_if_blank AO_LOCAL_AUTH_SECRET

# Bake the ACTUAL passwords into the DB URLs (no nested ${...} refs). Compose
# does NOT re-interpolate a ${VAR} that appears *inside* a value read from .env
# via env_file, so leaving ${APP_DB_PASSWORD}/${POSTGRES_PASSWORD} in
# DATABASE_URL / ADMIN_DATABASE_URL yields a BLANK password in the container —
# the app then can't authenticate to Postgres, which surfaces downstream as a
# workspace-provisioning transaction error. Expanding them here is robust across
# all Compose versions.
_pgpw="$(get_env POSTGRES_PASSWORD)"
_apppw="$(get_env APP_DB_PASSWORD)"
set_env DATABASE_URL "postgresql+asyncpg://accounting_ops_app:${_apppw}@postgres:5432/accounting_ops"
set_env ADMIN_DATABASE_URL "postgresql+asyncpg://accounting_ops:${_pgpw}@postgres:5432/accounting_ops"

# ---------------------------------------------------------------------------
# 3. edition + auth mode
# ---------------------------------------------------------------------------
set_env AO_EDITION community
set_env AO_AUTH_MODE local
set_env AO_LOCAL_ADMIN_UC_UID "$ADMIN_UC_UID"
set_env AO_LOCAL_ADMIN_EMAIL "$ADMIN_EMAIL"
[ -z "$(get_env PUBLIC_BASE_URL)" ] && set_env PUBLIC_BASE_URL "http://localhost:3000"

# ---------------------------------------------------------------------------
# 4. admin password hash
# ---------------------------------------------------------------------------
ADMIN_PASSWORD_NOTE="password you just set"
if [ -z "$(get_env AO_LOCAL_ADMIN_PASSWORD_HASH)" ]; then
  if [ -n "${AO_LOCAL_ADMIN_PASSWORD:-}" ]; then
    log "using AO_LOCAL_ADMIN_PASSWORD for the local admin ($ADMIN_EMAIL)"
    HASH="$(python3 scripts/gen_local_admin_hash.py)"
    ADMIN_PASSWORD_NOTE="password from AO_LOCAL_ADMIN_PASSWORD"
  elif [ -t 0 ]; then
    log "Set a password for the local admin ($ADMIN_EMAIL):"
    HASH="$(python3 scripts/gen_local_admin_hash.py)"
  else
    # Non-interactive install with no password supplied: generate one and
    # show it once on the installer's own terminal. Only the hash is stored.
    _generated_admin_password="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
    HASH="$(AO_LOCAL_ADMIN_PASSWORD="$_generated_admin_password" python3 scripts/gen_local_admin_hash.py)"
    log "no TTY and no AO_LOCAL_ADMIN_PASSWORD: generated a local admin password."
    printf '\n    Admin password (shown once, save it now): %s\n\n' "$_generated_admin_password"
    unset _generated_admin_password
    ADMIN_PASSWORD_NOTE="generated password shown above"
  fi
  set_env AO_LOCAL_ADMIN_PASSWORD_HASH "$HASH"
  log "stored admin password hash"
else
  log "admin password hash already set — leaving as-is"
  ADMIN_PASSWORD_NOTE="existing admin password"
fi

# ---------------------------------------------------------------------------
# 5. credential master key
# ---------------------------------------------------------------------------
if [ ! -f secrets/accounting_ops_master_key ]; then
  mkdir -p secrets
  python3 -m scripts.bootstrap_master_key --path secrets/accounting_ops_master_key
else
  log "master key already present — leaving as-is"
fi

# ---------------------------------------------------------------------------
# 6. build + up
# ---------------------------------------------------------------------------
log "building + starting the database and app services (this can take a few minutes on first run)..."
# Start the services needed for migrations first; web is built and started at
# the end, once the runtime role can log in and the workspace exists.
"${COMPOSE[@]}" up -d --build postgres garage ollama app

# ---------------------------------------------------------------------------
# 7. migrations + provisioning
# ---------------------------------------------------------------------------
log "running public-schema migrations..."
"${COMPOSE[@]}" exec -T app alembic -c alembic-public.ini upgrade head

# Tenant (shared "books") schema — creates entity/journal/etc. Provisioning
# inserts rows into this schema, so it MUST exist first. No migration issues
# CREATE SCHEMA, so create it here (owner-owned). The tenant migrations run as
# the OWNER (alembic uses ADMIN_DATABASE_URL) since they create tables,
# triggers, grants + RLS policies.
#
# Stop at TENANT_PRE_DATA_REVISION: the next revision (0030) is a data
# migration that rebuilds monthly_account_balance per workspace and refuses to
# run while public.workspace is empty. The schema is complete at this revision;
# the upgrade to head finishes after the workspace is provisioned (below).
# Same order as scripts/integration_env.py. On a re-run (already at head) this
# upgrade is a no-op.
TENANT_PRE_DATA_REVISION="0029_entity_owner"
_owner_db_password="$(get_env POSTGRES_PASSWORD)"
log "creating the shared tenant schema (books)..."
# The command is expanded by the shell inside the postgres container.
# shellcheck disable=SC2016
"${COMPOSE[@]}" exec -T -e "PGPASSWORD=${_owner_db_password}" postgres sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE SCHEMA IF NOT EXISTS books AUTHORIZATION \"$POSTGRES_USER\";"' \
  >/dev/null
log "running tenant-schema (books) migrations -> ${TENANT_PRE_DATA_REVISION}..."
"${COMPOSE[@]}" exec -T app alembic -c alembic-tenant.ini -x tenant=books upgrade "$TENANT_PRE_DATA_REVISION"

# The migrations intentionally create the runtime role without a password.
# Apply the already-generated host secret only after both migration passes have
# created/reasserted that role. The password is passed through the container
# environment and never printed or included in the SQL command text here.
_runtime_db_password="$(get_env APP_DB_PASSWORD)"
_owner_db_password="$(get_env POSTGRES_PASSWORD)"
if [ -z "$_runtime_db_password" ] || [ -z "$_owner_db_password" ]; then
  echo "ERROR: database passwords are missing from .env" >&2
  exit 1
fi
log "configuring the runtime database role..."
# The command is expanded by the shell inside the postgres container.
# shellcheck disable=SC2016
"${COMPOSE[@]}" exec -T \
  -e "PGPASSWORD=${_owner_db_password}" \
  -e "APP_DB_PASSWORD=${_runtime_db_password}" \
  postgres sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER ROLE accounting_ops_app PASSWORD '\''$APP_DB_PASSWORD'\'';"' \
  >/dev/null
unset _runtime_db_password _owner_db_password

log "waiting for the app to become healthy..."
for _ in $(seq 1 60); do
  status="$("${COMPOSE[@]}" ps -q app | xargs -I{} docker inspect -f '{{.State.Health.Status}}' {} 2>/dev/null || echo starting)"
  [ "$status" = "healthy" ] && break
  sleep 5
done
log "app health: ${status:-unknown}"
if [ "${status:-}" != "healthy" ]; then
  echo "ERROR: app did not become healthy; inspect with: ${COMPOSE[*]} logs app" >&2
  exit 1
fi

# /healthz is a liveness probe and does not touch the database, so "healthy"
# alone does not prove the runtime role can log in. Open one fresh connection
# with the app's own DATABASE_URL (the least-privilege role) and fail loudly if
# it is refused. Driver errors are swallowed so no connection detail is echoed.
log "checking the runtime database role can log in..."
# shellcheck disable=SC2016
if ! "${COMPOSE[@]}" exec -T app python -c '
import asyncio, os, sys
import asyncpg

async def probe():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(url, timeout=10)
    try:
        await conn.execute("SELECT 1")
    finally:
        await conn.close()

try:
    asyncio.run(probe())
except Exception:
    sys.exit(1)
' >/dev/null 2>&1; then
  echo "ERROR: the runtime role (accounting_ops_app) cannot log in to Postgres; inspect with: ${COMPOSE[*]} logs postgres" >&2
  exit 1
fi
log "runtime database role login OK"

WORKSPACE_ID="$(get_env DEFAULT_WORKSPACE_ID)"
if [ -z "$WORKSPACE_ID" ]; then
  WORKSPACE_ID="$(python3 -c 'import uuid;print(uuid.uuid4())')"
  set_env DEFAULT_WORKSPACE_ID "$WORKSPACE_ID"
  set_env TENANT_ID "$WORKSPACE_ID"
fi
ENTITY_SLUG="$(slugify "$COMPANY_NAME")"

log "provisioning workspace $WORKSPACE_ID ($COMPANY_NAME / $ENTITY_KIND)..."
"${COMPOSE[@]}" exec -T app python -m scripts.provision_workspace \
  "$WORKSPACE_ID" "$COMPANY_NAME" --entity "$COMPANY_NAME:$ENTITY_KIND"

log "finishing tenant-schema (books) migrations -> head..."
"${COMPOSE[@]}" exec -T app alembic -c alembic-tenant.ini -x tenant=books upgrade head

log "granting owner membership to '$ADMIN_UC_UID'..."
"${COMPOSE[@]}" exec -T app python -m scripts.grant_workspace_admin \
  "$WORKSPACE_ID" "$ADMIN_UC_UID" --role owner

log "seeding default chart of accounts..."
"${COMPOSE[@]}" exec -T app python -m scripts.seed_default_coa \
  "$WORKSPACE_ID" "$ENTITY_SLUG"

# Start/rebuild web and restart app so DEFAULT_WORKSPACE_ID/TENANT_ID land in
# the process environment.
"${COMPOSE[@]}" up -d --build app web

WEB_URL="$(get_env PUBLIC_BASE_URL)"; [ -z "$WEB_URL" ] && WEB_URL="http://localhost:3000"
cat <<EOF

============================================================================
 Accounting-Ops (Community Edition) is up.

   Web UI:    $WEB_URL
   Log in:    $ADMIN_EMAIL  ($ADMIN_PASSWORD_NOTE)
   Workspace: $COMPANY_NAME  ($WORKSPACE_ID)

 Next steps:
   - Pull a vision model for categorization / receipt OCR:
       ${COMPOSE[*]} exec ollama ollama pull qwen2.5vl:7b
   - Import a bank CSV or add a manual journal entry, then open /reports.

 Your books. Your machine. Your rules.
============================================================================
EOF
