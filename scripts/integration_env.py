"""Stand up (or tear down) the integration-test stack and provision it.

This owns the *infrastructure* half of the integration tests: docker compose,
DB migrations, and workspace provisioning. The pytest fixtures own the
test-data half (membership, COA seed, account mapping). Keeping them separate
means CI can cache/parallelize and the tests stay pure black-box.

Subcommands:
  up        generate secrets + .env.integration, `docker compose up -d` the
            minimal stack (postgres + garage + app [+ cpa-export if --with-cpa]),
            wait for health, then run public migrations + provision the test
            workspace + entity.
  env       print the `export VAR=...` lines pytest needs (eval-able).
  down      `docker compose down -v` (removes volumes — full reset).

Everything is idempotent. The compose project is namespaced
(COMPOSE_PROJECT_NAME=acctops_itest) so it never collides with a dev stack.

Usage:
  python scripts/integration_env.py up
  eval "$(python scripts/integration_env.py env)"
  pytest -m integration tests/integration
  python scripts/integration_env.py down
"""
from __future__ import annotations

import argparse
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Keep these in lockstep with tests/conftest.py.
TEST_WORKSPACE_SLUG = "integration-ci"
TEST_WORKSPACE_ID = uuid.uuid5(uuid.NAMESPACE_DNS, f"accounting-ops:{TEST_WORKSPACE_SLUG}")
TEST_ENTITY_NAME = "Integration Test Co"
TEST_ENTITY_KIND = "c_corp"
SCHEMA = f"accounting_ops_{str(TEST_WORKSPACE_ID).replace('-', '_')}"

PROJECT = "acctops_itest"
ENV_FILE = ROOT / ".env.integration"
SECRET_FILE = ROOT / "secrets" / "accounting_ops_master_key"

# Published host ports — MUST match docker-compose.itest.yml. Remapped into a
# high range so the throwaway stack never collides with a standard-port
# Postgres/app on the host or a shared CI runner.
HOST_PG_PORT = 55432
HOST_APP_PORT = 58000
HOST_CPA_PORT = 58093

# Fixed, non-secret-but-local values for the throwaway test stack.
POSTGRES_PASSWORD = "accounting_ops_test_pw"
SERVICE_TOKEN = "itest-" + "0" * 40  # >= 32 chars, deterministic for the test run
SERVICE_UC_UID = "test-service"

CORE_SERVICES = ["postgres", "garage", "app"]


BASE_COMPOSE = "docker-compose.yml"
ITEST_COMPOSE = "docker-compose.itest.yml"  # redefines the external traefik net as managed


def _compose(*args: str, env_file: bool = True, check: bool = True, capture: bool = False):
    cmd = ["docker", "compose", "-p", PROJECT, "-f", BASE_COMPOSE, "-f", ITEST_COMPOSE]
    if env_file:
        cmd += ["--env-file", str(ENV_FILE)]
    cmd += list(args)
    return subprocess.run(
        cmd, cwd=ROOT, check=check, text=True,
        capture_output=capture,
    )


def _write_secrets_and_env(with_cpa: bool) -> None:
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SECRET_FILE.exists():
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
        SECRET_FILE.chmod(0o600)

    service_tokens = (
        '{"%s": {"uc_uid": "%s", "agent_id": "integration-test"}}'
        % (SERVICE_TOKEN, SERVICE_UC_UID)
    )
    lines = [
        f"COMPOSE_PROJECT_NAME={PROJECT}",
        "HEADSCALE_BIND_IP=127.0.0.1",
        "APP_PORT=8000",
        "WEB_PORT=3000",
        "POSTGRES_DB=accounting_ops",
        "POSTGRES_USER=accounting_ops",
        f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
        f"DATABASE_URL=postgresql+asyncpg://accounting_ops:{POSTGRES_PASSWORD}@postgres:5432/accounting_ops",
        f"GARAGE_RPC_SECRET={secrets.token_hex(32)}",
        f"GARAGE_ADMIN_TOKEN={secrets.token_hex(32)}",
        "GARAGE_S3_ENDPOINT=http://garage:3900",
        # Service-token auth for the test caller (see app/auth.py).
        f"ACCTOPS_SERVICE_TOKENS={service_tokens}",
        # cpa-export reads a single shared token.
        f"CPA_EXPORT_SERVICE_TOKEN={SERVICE_TOKEN}",
        "OTEL_EXPORTER_OTLP_ENDPOINT=",
        "TRAEFIK_NETWORK=traefik",
    ]
    if with_cpa:
        lines.append("CPA_EXPORT_PORT=8093")
    ENV_FILE.write_text("\n".join(lines) + "\n")


def _wait_healthy(services: list[str], timeout_s: int = 240) -> None:
    deadline = time.time() + timeout_s
    pending = set(services)
    while pending and time.time() < deadline:
        for svc in list(pending):
            cid = _compose("ps", "-q", svc, capture=True, check=False).stdout.strip()
            if not cid:
                continue
            out = subprocess.run(
                ["docker", "inspect", "--format", "{{json .State.Health.Status}}", cid],
                text=True, capture_output=True, check=False,
            ).stdout.strip().strip('"')
            if out == "healthy":
                pending.discard(svc)
            elif out in ("unhealthy",):
                _compose("logs", svc, check=False)
                raise SystemExit(f"service {svc} reported unhealthy")
        if pending:
            time.sleep(3)
    if pending:
        for svc in pending:
            _compose("logs", svc, check=False)
        raise SystemExit(f"services not healthy within {timeout_s}s: {sorted(pending)}")


def _exec_app(*args: str, check: bool = True):
    return _compose("exec", "-T", "app", *args, check=check)


def cmd_up(with_cpa: bool) -> None:
    _write_secrets_and_env(with_cpa)
    services = list(CORE_SERVICES)
    if with_cpa:
        services.append("cpa-export")
    print(f"[integration] building + starting: {services}", file=sys.stderr)
    # cpa-export lives in the exports profile.
    up_args = ["up", "-d", "--build"]
    if with_cpa:
        up_args = ["--profile", "exports", *up_args]
    _compose(*up_args, *services)

    # app/postgres/garage have healthchecks; cpa-export too.
    _wait_healthy(services)

    print("[integration] running public-schema migrations", file=sys.stderr)
    _exec_app("alembic", "-c", "alembic-public.ini", "upgrade", "head")

    print(f"[integration] provisioning workspace {TEST_WORKSPACE_ID}", file=sys.stderr)
    _exec_app(
        "python", "-m", "scripts.provision_workspace",
        str(TEST_WORKSPACE_ID), TEST_ENTITY_NAME,
        "--slug", TEST_WORKSPACE_SLUG,
        "--entity", f"{TEST_ENTITY_NAME}:{TEST_ENTITY_KIND}",
    )
    print("[integration] stack ready.", file=sys.stderr)
    print("[integration] now run:  eval \"$(python scripts/integration_env.py env"
          + (" --with-cpa" if with_cpa else "") + ")\" && pytest -m integration tests/integration",
          file=sys.stderr)


def cmd_env(with_cpa: bool) -> None:
    dsn = f"postgresql://accounting_ops:{POSTGRES_PASSWORD}@127.0.0.1:{HOST_PG_PORT}/accounting_ops"
    print(f'export ACCTOPS_TEST_BASE_URL="http://127.0.0.1:{HOST_APP_PORT}"')
    print(f'export ACCTOPS_TEST_DSN="{dsn}"')
    print(f'export ACCTOPS_TEST_TOKEN="{SERVICE_TOKEN}"')
    print(f'export ACCTOPS_TEST_UC_UID="{SERVICE_UC_UID}"')
    print(f'export POSTGRES_PASSWORD="{POSTGRES_PASSWORD}"')
    if with_cpa:
        print(f'export CPA_EXPORT_URL="http://127.0.0.1:{HOST_CPA_PORT}"')
        print(f'export CPA_EXPORT_TOKEN="{SERVICE_TOKEN}"')


def cmd_down() -> None:
    _compose("--profile", "exports", "down", "-v", check=False)
    print("[integration] stack + volumes removed.", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("up", "env", "down"):
        p = sub.add_parser(name)
        if name in ("up", "env"):
            p.add_argument(
                "--with-cpa", action="store_true",
                help="include the cpa-export sibling service (exports profile)",
            )
    args = parser.parse_args()
    if args.cmd == "up":
        cmd_up(args.with_cpa)
    elif args.cmd == "env":
        cmd_env(args.with_cpa)
    elif args.cmd == "down":
        cmd_down()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
