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
#   4. Prompt for a local admin password and store its scrypt hash.
#   5. Generate the credential master key (secrets/accounting_ops_master_key).
#   6. Build + start the self-host stack (postgres, garage, ollama, app, web).
#   7. Run migrations, provision one workspace + entity, grant the admin owner
#      membership, and seed a default chart of accounts.
#
# Usage:  scripts/selfhost_bootstrap.sh          (or: make selfhost)
# Env you may pre-set: AO_COMPANY_NAME, AO_ENTITY_KIND, AO_LOCAL_ADMIN_UC_UID,
#                      AO_LOCAL_ADMIN_EMAIL
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
if [ -z "$(get_env AO_LOCAL_ADMIN_PASSWORD_HASH)" ]; then
  log "Set a password for the local admin ($ADMIN_EMAIL):"
  HASH="$(python3 scripts/gen_local_admin_hash.py)"
  set_env AO_LOCAL_ADMIN_PASSWORD_HASH "$HASH"
  log "stored admin password hash"
else
  log "admin password hash already set — leaving as-is"
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
log "building + starting the stack (this can take a few minutes on first run)..."
"${COMPOSE[@]}" up -d --build

log "waiting for the app to become healthy..."
for _ in $(seq 1 60); do
  status="$("${COMPOSE[@]}" ps -q app | xargs -I{} docker inspect -f '{{.State.Health.Status}}' {} 2>/dev/null || echo starting)"
  [ "$status" = "healthy" ] && break
  sleep 5
done
log "app health: ${status:-unknown}"

# ---------------------------------------------------------------------------
# 7. migrations + provisioning
# ---------------------------------------------------------------------------
log "running public-schema migrations..."
"${COMPOSE[@]}" exec -T app alembic -c alembic-public.ini upgrade head

# Tenant (shared "books") schema — creates entity/journal/etc. Provisioning
# inserts rows into this schema, so it MUST exist first. Run as the OWNER
# (ADMIN_DATABASE_URL) since it creates tables, triggers, grants + RLS policies.
log "running tenant-schema (books) migrations..."
"${COMPOSE[@]}" exec -T app sh -c \
  'DATABASE_URL="$ADMIN_DATABASE_URL" alembic -c alembic-tenant.ini -x tenant=books upgrade head'

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

log "granting owner membership to '$ADMIN_UC_UID'..."
"${COMPOSE[@]}" exec -T app python -m scripts.grant_workspace_admin \
  "$WORKSPACE_ID" "$ADMIN_UC_UID" --role owner

log "seeding default chart of accounts..."
"${COMPOSE[@]}" exec -T app python -m scripts.seed_default_coa \
  "$WORKSPACE_ID" "$ENTITY_SLUG"

# restart web/app so DEFAULT_WORKSPACE_ID/TENANT_ID land in the process env
"${COMPOSE[@]}" up -d app web

WEB_URL="$(get_env PUBLIC_BASE_URL)"; [ -z "$WEB_URL" ] && WEB_URL="http://localhost:3000"
cat <<EOF

============================================================================
 Accounting-Ops (Community Edition) is up.

   Web UI:    $WEB_URL
   Log in:    $ADMIN_EMAIL  (password you just set)
   Workspace: $COMPANY_NAME  ($WORKSPACE_ID)

 Next steps:
   - Pull a vision model for categorization / receipt OCR:
       ${COMPOSE[*]} exec ollama ollama pull qwen2.5vl:7b
   - Import a bank CSV or add a manual journal entry, then open /reports.

 Your books. Your machine. Your rules.
============================================================================
EOF
