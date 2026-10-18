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
  run       up, run pytest in the app container, and always tear down.

Everything is idempotent. The compose project is namespaced per invocation:
AO_ITEST_PROJECT wins when set; otherwise CI run IDs or a short local suffix
are used. The selected name is written to .env.integration so separate `env`,
`test`, and `down` invocations use the same project.

Usage:
  python scripts/integration_env.py up
  python scripts/integration_env.py test -- -m integration tests/integration
  python scripts/integration_env.py down
  python scripts/integration_env.py run -- -m integration tests/integration
  python scripts/integration_env.py run --keep -- -m integration tests/integration
"""
from __future__ import annotations

import argparse
import os
import re
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
# Shared-schema model (tenant/0025+0026): every workspace's rows live in the ONE
# fixed `books` schema, isolated by FORCE RLS on workspace_id — there is no
# per-workspace schema any more. Mirrors app.workspace_session.SHARED_SCHEMA.
SHARED_SCHEMA = "books"

# The last SCHEMA-ONLY tenant revision. The next one, 0030_mab_include_corrections,
# is a DATA migration: it rebuilds monthly_account_balance per workspace and
# RAISES if public.workspace is empty ("refusing to stamp a migration that would
# silently rebuild nothing"). A fresh throwaway stack has no workspaces when the
# tenant schema is first built, so we migrate to this revision, provision the
# workspaces (making public.workspace non-empty), THEN finish the upgrade to head
# so 0030 has data to rebuild. It adds no tables/columns, so provisioning against
# this revision has the full schema it needs.
TENANT_PRE_DATA_REVISION = "0029_entity_owner"

# A SECOND, fully separate workspace (its own schema) — the cross-tenant target
# for the RLS-backstop proof. Provisioned the same way as workspace A.
TEST_WORKSPACE_B_SLUG = "integration-ci-b"
TEST_WORKSPACE_B_ID = uuid.uuid5(uuid.NAMESPACE_DNS, f"accounting-ops:{TEST_WORKSPACE_B_SLUG}")
TEST_WORKSPACE_B_ENTITY_NAME = "Integration Test Co B"

ENV_FILE = ROOT / ".env.integration"
SECRET_FILE = ROOT / "secrets" / "accounting_ops_master_key"

PROJECT_ENV = "AO_ITEST_PROJECT"
PROJECT_PREFIX = "acctops_itest"
PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CI_RUN_ID_ENV_VARS = ("GITHUB_RUN_ID", "FORGEJO_RUN_ID")

# Fixed, non-secret-but-local values for the throwaway test stack.
POSTGRES_PASSWORD = "accounting_ops_test_pw"
SERVICE_TOKEN = "itest-" + "0" * 40  # >= 32 chars, deterministic for the test run
SERVICE_UC_UID = "test-service"

# The least-privilege runtime role + a LOCAL (throwaway-stack-only) password.
# This is NOT a production secret — it exists only for the ephemeral CI/dev
# stack so the app can authenticate as the restricted role and the RLS /
# role-permission tests can connect as it directly. Production sets the
# password out-of-band (DEPLOY.md), never in the repo.
RUNTIME_DB_ROLE = "accounting_ops_app"
RUNTIME_DB_PASSWORD = "accounting_ops_app_test_pw"
OWNER_DB_ROLE = "accounting_ops"

CORE_SERVICES = ["postgres", "garage", "app"]


BASE_COMPOSE = "docker-compose.yml"
ITEST_COMPOSE = "docker-compose.itest.yml"  # redefines the external traefik net as managed


def _read_env_value(name: str) -> str | None:
    """Read one generated env value without exposing the rest of the file."""
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        return None
    prefix = f"{name}="
    for line in lines:
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value or None
    return None


def _validate_project_name(value: str) -> str:
    if not PROJECT_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"{PROJECT_ENV} must contain only lowercase letters, digits, '-' or '_', "
            f"starting with a letter or digit; got {value!r}"
        )
    return value


def _new_project_name() -> str:
    for env_name in CI_RUN_ID_ENV_VARS:
        run_id = os.environ.get(env_name, "").strip()
        if run_id:
            suffix = re.sub(r"[^a-z0-9_-]+", "-", run_id.lower()).strip("-_")
            if suffix:
                return _validate_project_name(f"{PROJECT_PREFIX}_{suffix}")
    return _validate_project_name(f"{PROJECT_PREFIX}_{os.getpid()}_{secrets.token_hex(2)}")


def resolve_project_name(*, for_up: bool = False) -> str:
    """Resolve the project name, retaining it across separate CLI invocations.

    A fresh local `up` gets a new suffix. Later commands reuse the project
    recorded in .env.integration. CI supplies AO_ITEST_PROJECT from the
    workflow run ID, so every concurrent job has an independent namespace.
    """
    configured = os.environ.get(PROJECT_ENV, "").strip()
    if configured:
        return _validate_project_name(configured)

    if not for_up:
        persisted = _read_env_value(PROJECT_ENV)
        if persisted:
            return _validate_project_name(persisted)

    return _new_project_name()


def _compose(*args: str, env_file: bool = True, check: bool = True, capture: bool = False):
    cmd = [
        "docker", "compose", "-p", resolve_project_name(),
        "-f", BASE_COMPOSE, "-f", ITEST_COMPOSE,
    ]
    if env_file:
        cmd += ["--env-file", str(ENV_FILE)]
    cmd += list(args)
    return subprocess.run(
        cmd, cwd=ROOT, check=check, text=True,
        capture_output=capture,
    )


def _write_secrets_and_env(
    with_cpa: bool,
    project: str | None = None,
    *,
    include_master_key: bool = True,
) -> None:
    project = project or resolve_project_name(for_up=True)
    dummy_key: bytes | None = None
    if include_master_key:
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not SECRET_FILE.exists():
            SECRET_FILE.write_bytes(secrets.token_bytes(32))
            SECRET_FILE.chmod(0o600)
        dummy_key = SECRET_FILE.read_bytes()
        if len(dummy_key) != 32:
            raise ValueError(f"{SECRET_FILE} must contain exactly 32 bytes for itest")

    # H2 / audit 2026-05-31 Bug 7: the app REJECTS a service token that declares
    # NONE of workspace_ids / entity_ids / role_floor (unless the dev escape
    # hatch ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN is set) — see app/auth.py
    # _load_service_tokens. Declare role_floor="owner": a real scope field that
    # satisfies the loader WITHOUT constraining the token's reach. The
    # impersonated test-service user is provisioned as workspace OWNER, and role
    # cap is min(membership_role, role_floor); since nothing in the suite ever
    # assigns test-service a role above owner, the cap is a no-op and the token's
    # effective role always equals its membership role. Crucially we do NOT set
    # workspace_ids, so cross-tenant denials still fire at the MEMBERSHIP layer
    # (workspace B has no membership row) — preserving the "no active membership"
    # / "permission" error the isolation tests assert.
    service_tokens = (
        '{"%s": {"uc_uid": "%s", "agent_id": "integration-test", '
        '"role_floor": "owner"}}'
        % (SERVICE_TOKEN, SERVICE_UC_UID)
    )
    lines = [
        f"AO_ITEST_PROJECT={project}",
        f"COMPOSE_PROJECT_NAME={project}",
        "HEADSCALE_BIND_IP=127.0.0.1",
        "APP_PORT=8000",
        "WEB_PORT=3000",
        "POSTGRES_DB=accounting_ops",
        "POSTGRES_USER=accounting_ops",
        f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
        # Runtime: the app/web/cpa-export connect as the LEAST-PRIVILEGE role.
        f"DATABASE_URL=postgresql+asyncpg://{RUNTIME_DB_ROLE}:{RUNTIME_DB_PASSWORD}@postgres:5432/accounting_ops",
        # Admin: migrations + provisioning (run via `docker compose exec app`)
        # use the table-OWNING role.
        f"ADMIN_DATABASE_URL=postgresql+asyncpg://{OWNER_DB_ROLE}:{POSTGRES_PASSWORD}@postgres:5432/accounting_ops",
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
    if dummy_key is not None:
        lines.append(f"AO_ITEST_MASTER_KEY_HEX={dummy_key.hex()}")
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


def _psql_owner(sql: str, check: bool = True):
    """Run SQL as the OWNER (= POSTGRES_USER superuser) inside the pg container."""
    return _compose(
        "exec", "-T",
        "-e", f"PGPASSWORD={POSTGRES_PASSWORD}",
        "postgres",
        "psql", "-v", "ON_ERROR_STOP=1",
        "-U", OWNER_DB_ROLE, "-d", "accounting_ops",
        "-c", sql,
        check=check,
    )


def _bootstrap_runtime_role() -> None:
    """Create the least-privilege runtime role WITH a (local, throwaway) password.

    The migrations create the role too (idempotent), but with NO password — a
    no-password LOGIN role can't authenticate. For the ephemeral test stack we
    set a local password here so the app (and the role-permission tests) can
    connect. In production the password is set out-of-band; see DEPLOY.md.

    Runs before migrations so the migration DO-block finds the role already
    present and only re-asserts the safe attributes (it never clears a password).
    """
    print(f"[integration] bootstrapping runtime role {RUNTIME_DB_ROLE}", file=sys.stderr)
    _psql_owner(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_DB_ROLE}') THEN
            CREATE ROLE {RUNTIME_DB_ROLE} LOGIN PASSWORD '{RUNTIME_DB_PASSWORD}'
              NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
          ELSE
            ALTER ROLE {RUNTIME_DB_ROLE} LOGIN PASSWORD '{RUNTIME_DB_PASSWORD}'
              NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
          END IF;
        END
        $$;
        """
    )


def cmd_up(with_cpa: bool) -> None:
    project = resolve_project_name(for_up=True)
    _write_secrets_and_env(with_cpa, project, include_master_key=False)
    services = list(CORE_SERVICES)
    if with_cpa:
        services.append("cpa-export")
    print(f"[integration] project {project}; building + starting: {services}", file=sys.stderr)
    # Build before creating the dummy runtime secret. This guarantees the
    # generated file is never a build input, even if the context rules regress.
    build_args = ["build"]
    if with_cpa:
        build_args = ["--profile", "exports", *build_args]
    _compose(*build_args, *services)

    # The app/sidecar receive this key through the runtime-only named volume.
    _write_secrets_and_env(with_cpa, project)

    # cpa-export lives in the exports profile.
    up_args = ["up", "-d"]
    if with_cpa:
        up_args = ["--profile", "exports", *up_args]
    _compose(*up_args, *services)

    # app/postgres/garage have healthchecks; cpa-export too.
    _wait_healthy(services)

    # Create the least-privilege runtime role + local password BEFORE migrations
    # so the migrations' idempotent role-create finds it and the app can connect.
    _bootstrap_runtime_role()

    print("[integration] running public-schema migrations", file=sys.stderr)
    _exec_app("alembic", "-c", "alembic-public.ini", "upgrade", "head")

    # --- One-time tenant bootstrap (shared-schema model) -------------------
    # provision_workspace is rows-only: it inserts the workspace registry row +
    # seed entities and assumes the fixed `books` schema and its tables ALREADY
    # exist. The tenant migrations create those tables but do NOT create the
    # schema itself (migrations/tenant/env.py only sets search_path), and no
    # migration issues CREATE SCHEMA. On a throwaway stack nothing else creates
    # `books`, so without this step provisioning fails with
    # `relation "entity" does not exist`. Create the schema (owner-owned) then
    # run the tenant migrations into it — the deploy-time equivalent of
    # `CREATE SCHEMA books` + `alembic -c alembic-tenant.ini -x tenant=books
    # upgrade head` (tenant/0005 also grants the runtime role USAGE + the
    # least-privilege table grants, so the app can connect afterwards).
    #
    # We stop ONE revision short of head (at TENANT_PRE_DATA_REVISION): the head
    # migration 0030 is a data-backfill that refuses to run against an empty
    # public.workspace. The schema is fully built here; 0030 is finished after
    # the workspaces are provisioned (below), so it has data to rebuild.
    print("[integration] creating shared tenant schema `books`", file=sys.stderr)
    _psql_owner("CREATE SCHEMA IF NOT EXISTS books AUTHORIZATION accounting_ops")
    print(
        f"[integration] running tenant-schema (books) migrations -> {TENANT_PRE_DATA_REVISION}",
        file=sys.stderr,
    )
    _exec_app(
        "alembic", "-c", "alembic-tenant.ini", "-x", f"tenant={SHARED_SCHEMA}",
        "upgrade", TENANT_PRE_DATA_REVISION,
    )

    print(f"[integration] provisioning workspace {TEST_WORKSPACE_ID}", file=sys.stderr)
    _exec_app(
        "python", "-m", "scripts.provision_workspace",
        str(TEST_WORKSPACE_ID), TEST_ENTITY_NAME,
        "--slug", TEST_WORKSPACE_SLUG,
        "--entity", f"{TEST_ENTITY_NAME}:{TEST_ENTITY_KIND}",
    )

    print(f"[integration] provisioning workspace B {TEST_WORKSPACE_B_ID}", file=sys.stderr)
    _exec_app(
        "python", "-m", "scripts.provision_workspace",
        str(TEST_WORKSPACE_B_ID), TEST_WORKSPACE_B_ENTITY_NAME,
        "--slug", TEST_WORKSPACE_B_SLUG,
        "--entity", f"{TEST_WORKSPACE_B_ENTITY_NAME}:{TEST_ENTITY_KIND}",
    )

    # Finish the tenant upgrade now that public.workspace is non-empty: the head
    # migration 0030 rebuilds monthly_account_balance per workspace and verifies
    # zero drift (both freshly-provisioned workspaces have empty journals, so the
    # rebuild is a clean no-op that stamps correctly instead of raising).
    print("[integration] finishing tenant-schema (books) migrations -> head", file=sys.stderr)
    _exec_app(
        "alembic", "-c", "alembic-tenant.ini", "-x", f"tenant={SHARED_SCHEMA}",
        "upgrade", "head",
    )

    print("[integration] stack ready.", file=sys.stderr)
    print(
        "[integration] now run: python scripts/integration_env.py test -- "
        "-m integration tests/integration",
        file=sys.stderr,
    )


def cmd_env(with_cpa: bool) -> None:
    """Print service-DNS settings for a process on the compose network."""
    print(f'export AO_ITEST_PROJECT="{resolve_project_name()}"')
    # Owner DSN: used by the harness for seeding + tamper-injection (the tamper
    # test must DISABLE TRIGGER, an owner-only op — it models an attacker with
    # raw owner access).
    dsn = f"postgresql://{OWNER_DB_ROLE}:{POSTGRES_PASSWORD}@postgres:5432/accounting_ops"
    # Runtime-role DSN: used by the RLS + role-permission tests to prove the
    # least-privilege role canNOT mutate the ledger / disable triggers, and that
    # RLS blocks cross-tenant reads under it.
    app_dsn = f"postgresql://{RUNTIME_DB_ROLE}:{RUNTIME_DB_PASSWORD}@postgres:5432/accounting_ops"
    print('export ACCTOPS_TEST_BASE_URL="http://app:8000"')
    print(f'export ACCTOPS_TEST_DSN="{dsn}"')
    print(f'export ACCTOPS_TEST_APP_DSN="{app_dsn}"')
    print(f'export ACCTOPS_TEST_TOKEN="{SERVICE_TOKEN}"')
    print(f'export ACCTOPS_TEST_UC_UID="{SERVICE_UC_UID}"')
    print(f'export POSTGRES_PASSWORD="{POSTGRES_PASSWORD}"')
    if with_cpa:
        print('export CPA_EXPORT_URL="http://cpa-export:8093"')
        print(f'export CPA_EXPORT_TOKEN="{SERVICE_TOKEN}"')


def cmd_down() -> None:
    _compose("--profile", "exports", "down", "-v", check=False)
    print("[integration] stack + volumes removed.", file=sys.stderr)


def _collect_failure_logs(with_cpa: bool) -> None:
    services = list(CORE_SERVICES)
    if with_cpa:
        services.append("cpa-export")
    try:
        _compose("logs", *services, check=False)
    except Exception:
        # Logging must never prevent the finally block from tearing the stack
        # down, and the original test/startup error is more useful than a
        # secondary logging failure.
        print("[integration] unable to collect failure logs", file=sys.stderr)


def cmd_test(pytest_args: list[str], with_cpa: bool = False) -> int:
    """Run pytest INSIDE the `app` container instead of out here.

    The test process runs inside the `app` container, where `127.0.0.1:8000`
    reaches the app and Compose DNS resolves `postgres` and `cpa-export`.
    No host port or dind NAT path is involved.

    Needs `.[dev]` inside the container (the image only ships the
    `[tax-agents]` runtime extra); installed here rather than baked into
    the image so production's Dockerfile/image stay untouched.
    """
    print("[integration] installing dev extras into the app container", file=sys.stderr)
    _compose("exec", "-T", "app", "python", "-m", "pip", "install", "--no-cache-dir", "-e", ".[dev]")

    in_container_env = {
        "ACCTOPS_TEST_BASE_URL": "http://127.0.0.1:8000",
        "ACCTOPS_TEST_DSN": f"postgresql://{OWNER_DB_ROLE}:{POSTGRES_PASSWORD}@postgres:5432/accounting_ops",
        "ACCTOPS_TEST_APP_DSN": f"postgresql://{RUNTIME_DB_ROLE}:{RUNTIME_DB_PASSWORD}@postgres:5432/accounting_ops",
        "ACCTOPS_TEST_TOKEN": SERVICE_TOKEN,
        "ACCTOPS_TEST_UC_UID": SERVICE_UC_UID,
        "POSTGRES_PASSWORD": POSTGRES_PASSWORD,
    }
    if with_cpa:
        in_container_env["CPA_EXPORT_URL"] = "http://cpa-export:8093"
        in_container_env["CPA_EXPORT_TOKEN"] = SERVICE_TOKEN
    exec_args = ["exec", "-T"]
    for key, value in in_container_env.items():
        exec_args += ["-e", f"{key}={value}"]
    exec_args += ["app", "pytest", *pytest_args]

    print(f"[integration] running in-container: pytest {' '.join(pytest_args)}", file=sys.stderr)
    result = _compose(*exec_args, check=False)

    # pytest writes its junitxml inside the container (WORKDIR /app); copy it
    # out so the caller (this script's own process, or CI reading the same
    # relative path) can read it regardless of the exit code below.
    for arg in pytest_args:
        if arg.startswith("--junitxml="):
            xml_path = arg.split("=", 1)[1]
            _compose("cp", f"app:/app/{xml_path}", xml_path, check=False)
            break

    return result.returncode


def cmd_run(with_cpa: bool, keep: bool, pytest_args: list[str]) -> int:
    """Run the full integration lifecycle and tear down unless `--keep`."""
    try:
        cmd_up(with_cpa)
        result = cmd_test(pytest_args, with_cpa)
        if result:
            _collect_failure_logs(with_cpa)
        return result
    except BaseException:
        _collect_failure_logs(with_cpa)
        raise
    finally:
        if keep:
            print("[integration] --keep set; stack left running for debugging.", file=sys.stderr)
        else:
            cmd_down()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("up", "env", "down", "test", "run"):
        p = sub.add_parser(name)
        if name in ("up", "env", "test", "run"):
            p.add_argument(
                "--with-cpa", action="store_true",
                help="include the cpa-export sibling service (exports profile)",
            )
        if name == "run":
            p.add_argument(
                "--keep", action="store_true",
                help="leave the stack running after tests for debugging",
            )
        if name in ("test", "run"):
            p.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.cmd == "up":
        cmd_up(args.with_cpa)
    elif args.cmd == "env":
        cmd_env(args.with_cpa)
    elif args.cmd == "down":
        cmd_down()
    elif args.cmd == "test":
        pytest_args = args.pytest_args
        if pytest_args and pytest_args[0] == "--":
            pytest_args = pytest_args[1:]
        return cmd_test(pytest_args, args.with_cpa)
    elif args.cmd == "run":
        pytest_args = args.pytest_args
        if pytest_args and pytest_args[0] == "--":
            pytest_args = pytest_args[1:]
        return cmd_run(args.with_cpa, args.keep, pytest_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
