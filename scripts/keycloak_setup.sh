#!/usr/bin/env bash
set -euo pipefail

KEYCLOAK_BASE_URL="${KEYCLOAK_BASE_URL:-https://auth.unicorncommander.ai}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-uchub}"
KEYCLOAK_ADMIN_USER="${KEYCLOAK_ADMIN_USER:-admin}"

if [[ -z "${KEYCLOAK_ADMIN_PASSWORD:-}" ]]; then
  read -r -s -p "Keycloak admin password: " KEYCLOAK_ADMIN_PASSWORD
  echo
fi

require_kcadm() {
  command -v kcadm.sh >/dev/null 2>&1 || {
    echo "kcadm.sh is required on PATH" >&2
    exit 1
  }
}

login() {
  kcadm.sh config credentials \
    --server "$KEYCLOAK_BASE_URL" \
    --realm master \
    --user "$KEYCLOAK_ADMIN_USER" \
    --password "$KEYCLOAK_ADMIN_PASSWORD"
}

client_exists() {
  local client_id="$1"
  kcadm.sh get clients -r "$KEYCLOAK_REALM" -q clientId="$client_id" --fields id --format csv noquotes | grep -q .
}

ensure_client() {
  local client_id="$1"
  local name="$2"
  if client_exists "$client_id"; then
    echo "client exists: $client_id"
    return
  fi
  kcadm.sh create clients -r "$KEYCLOAK_REALM" \
    -s clientId="$client_id" \
    -s name="$name" \
    -s enabled=true \
    -s publicClient=false \
    -s standardFlowEnabled=true \
    -s serviceAccountsEnabled=true \
    -s directAccessGrantsEnabled=false \
    -s 'redirectUris=["https://accounting.magicunicorn.dev/*","http://localhost:3000/*"]' \
    -s 'webOrigins=["https://accounting.magicunicorn.dev","http://localhost:3000"]'
  echo "created client: $client_id"
}

provision_tenant() {
  local tenant_id="$1"
  local display_name="$2"
  local role_name="accounting-ops-${tenant_id}"
  local group_path="/Accounting-Ops/${tenant_id}"

  kcadm.sh get roles/"$role_name" -r "$KEYCLOAK_REALM" >/dev/null 2>&1 \
    || kcadm.sh create roles -r "$KEYCLOAK_REALM" -s name="$role_name" -s "description=Accounting-Ops tenant role for ${display_name}"

  if ! kcadm.sh get groups -r "$KEYCLOAK_REALM" -q search="$tenant_id" --fields id --format csv noquotes | grep -q .; then
    kcadm.sh create groups -r "$KEYCLOAK_REALM" -s name="$tenant_id" -s "attributes.display_name=[\"${display_name}\"]"
  fi
  echo "tenant provisioned: $tenant_id ($display_name) role=$role_name group=$group_path"
}

main() {
  require_kcadm
  login
  ensure_client "accounting-ops-app" "Accounting-Ops Web App"
  ensure_client "accounting-ops-mcp-server" "Accounting-Ops MCP Server"
  ensure_client "accounting-ops-a2a-endpoint" "Accounting-Ops A2A Endpoint"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
