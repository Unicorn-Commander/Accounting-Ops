#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

docker compose config >/dev/null
docker compose up -d postgres garage app web

for service in postgres garage app web; do
  status=""
  for _ in $(seq 1 60); do
    status="$(docker inspect --format='{{json .State.Health.Status}}' "$(docker compose ps -q "$service")" 2>/dev/null | tr -d '"')" || true
    [[ "$status" == "healthy" ]] && break
    sleep 2
  done
  if [[ "$status" != "healthy" ]]; then
    echo "$service did not become healthy; status=$status" >&2
    docker compose logs "$service" >&2
    exit 1
  fi
done

echo "all services healthy"
