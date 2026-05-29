#!/bin/bash
# Run the full tax specialist team against an entity + year.
#
# Designed to be invoked on midboy1 (or any host with docker compose running
# accounting_ops-app-1 + a reachable LLM endpoint).
#
# Usage:
#   ./scripts/run_tax_specialists.sh <workspace-id> <entity-id> <tax-year> [--backend openai|anthropic]
#
# Output:
#   /tmp/<entity-slug>-<year>-{1120,1099,sc1120}-draft.md
#   tax_review_queue rows for each specialist's findings
#
# Env vars consumed:
#   ACCTOPS_SERVICE_TOKEN_FILE  Path to a file containing the service token
#                                (default: /home/muut/.tax-1120-preparer.token)
#   ANTHROPIC_API_KEY            Required for --backend anthropic
#   LLM_ENDPOINT_URL             Override local LLM endpoint (default uses midboy1:8088)
#   LLM_MODEL                    Override local LLM model (default Qwen3.6-35B-A3B-Vision)

set -euo pipefail

WORKSPACE="${1:?usage: $0 <workspace-id> <entity-id> <tax-year> [--backend anthropic|openai]}"
ENTITY="${2:?need entity-id}"
YEAR="${3:?need tax-year}"
BACKEND="${4:-openai}"

TOKEN_FILE="${ACCTOPS_SERVICE_TOKEN_FILE:-/home/muut/.tax-1120-preparer.token}"
if [[ ! -f "$TOKEN_FILE" ]]; then
    echo "ERROR: service token file not found at $TOKEN_FILE" >&2
    exit 2
fi
TOKEN=$(cat "$TOKEN_FILE")

LLM_ENDPOINT="${LLM_ENDPOINT_URL:-http://midboy1:8088/v1}"
LLM_MODEL_NAME="${LLM_MODEL:-Qwen3.6-35B-A3B-Vision}"
LLM_KEY="${LLM_API_KEY:-local-qwen}"

COMMON_ARGS=(
    --workspace-id "$WORKSPACE"
    --entity-id    "$ENTITY"
    --tax-year     "$YEAR"
    --mcp-url      "http://localhost:8000/mcp/"
    --backend      "$BACKEND"
    --llm-endpoint-url "$LLM_ENDPOINT"
    --llm-model    "$LLM_MODEL_NAME"
    --llm-api-key  "$LLM_KEY"
)

ENV_FLAGS=(-e ACCTOPS_SERVICE_TOKEN="$TOKEN")
if [[ "$BACKEND" == "anthropic" ]]; then
    ENV_FLAGS+=(-e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY required for anthropic backend}")
fi

run_specialist() {
    local SPECIALIST="$1"
    local OUTPUT_SLUG="$2"
    local OUTPUT_FILE="/tmp/entity-${ENTITY:0:8}-${YEAR}-${OUTPUT_SLUG}-draft.md"

    echo "[run-team] starting $SPECIALIST for year $YEAR ..."
    docker exec "${ENV_FLAGS[@]}" accounting_ops-app-1 \
        python "/app/agents/tax_specialists/$SPECIALIST/run_specialist.py" \
        "${COMMON_ARGS[@]}" \
        --output "$OUTPUT_FILE" \
        2>&1 | tail -5
    echo "[run-team] $SPECIALIST complete; draft at container:$OUTPUT_FILE"
}

# Order: federal 1120 first (state specialists depend on its journal data),
# then 1099 + SC in parallel (independent on the data side).
run_specialist form_1120_preparer 1120
run_specialist 1099_specialist 1099 &
PID_1099=$!
run_specialist sc_state_specialist sc1120 &
PID_SC=$!

wait $PID_1099 $PID_SC

echo "[run-team] all 3 specialists done for entity ${ENTITY:0:8} year $YEAR"
echo "[run-team] inspect drafts: docker exec accounting_ops-app-1 ls /tmp/entity-${ENTITY:0:8}-${YEAR}-*-draft.md"
echo "[run-team] inspect queue: SELECT … FROM tax_review_queue WHERE entity_id = '$ENTITY' AND tax_year = $YEAR"
