"""Shared pytest fixtures + config for the Accounting-Ops test suite.

Two test tiers live under tests/:

  tests/smoke/        - import-only + compose-config checks (no running stack)
  tests/integration/  - exercise a LIVE stack stood up via docker compose
                        (postgres + garage + app [+ cpa-export]).

The integration tier talks to the app over HTTP and to Postgres directly
(for deterministic seeding + tamper injection). Everything is driven by a
handful of env vars so the same tests run locally and in CI unchanged:

  ACCTOPS_TEST_BASE_URL   app base URL          (default http://127.0.0.1:8000)
  ACCTOPS_TEST_DSN        asyncpg DSN to PG     (default postgresql://accounting_ops:...@127.0.0.1:5432/accounting_ops)
  ACCTOPS_TEST_TOKEN      service bearer token  (must be in the app's ACCTOPS_SERVICE_TOKENS)
  ACCTOPS_TEST_UC_UID     uc_uid the token maps to (default test-service)
  CPA_EXPORT_URL          cpa-export base URL   (optional; if unset the CPA step
                          asserts the export-source data instead of the PDF/CSV service)
  CPA_EXPORT_TOKEN        cpa-export bearer     (default: same as ACCTOPS_TEST_TOKEN)

If the stack is not reachable, the integration fixtures SKIP (they never
error). This keeps `pytest` green on a laptop without docker while the CI
runner — which does stand the stack up — executes them for real.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import pytest

# --------------------------------------------------------------------------
# Deterministic identifiers for the integration test workspace + entity.
# uuid5 keeps them stable across runs so re-runs are idempotent (the seed +
# provision paths are themselves idempotent).
# --------------------------------------------------------------------------
TEST_WORKSPACE_SLUG = "integration-ci"
TEST_WORKSPACE_ID = uuid.uuid5(uuid.NAMESPACE_DNS, f"accounting-ops:{TEST_WORKSPACE_SLUG}")
TEST_ENTITY_NAME = "Integration Test Co"
TEST_ENTITY_KIND = "c_corp"
TEST_ENTITY_ID = uuid.uuid5(
    uuid.NAMESPACE_DNS, f"{TEST_WORKSPACE_ID}:entity:integration-test-co"
)

# A SECOND fully-separate workspace (its own Postgres schema) used by the
# RLS-backstop cross-tenant proof in tests/integration/test_db_rls_roles.py.
# Provisioned by scripts/integration_env.py — keep in lockstep.
TEST_WORKSPACE_B_SLUG = "integration-ci-b"
TEST_WORKSPACE_B_ID = uuid.uuid5(uuid.NAMESPACE_DNS, f"accounting-ops:{TEST_WORKSPACE_B_SLUG}")
TEST_WORKSPACE_B_ENTITY_ID = uuid.uuid5(
    uuid.NAMESPACE_DNS, f"{TEST_WORKSPACE_B_ID}:entity:integration-test-co-b"
)

# The external bank account the SimpleFIN-style transactions arrive on. We
# link COA 1010 (Cash · Primary Checking) to it AND register an
# account_entity_mapping row so the journal-posting path resolves accounts
# and marks the txn entity_mapped=True.
TEST_SOURCE = "simplefin"
TEST_EXTERNAL_ACCOUNT_ID = "ACC-integration-checking-0001"


def env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


@dataclass(frozen=True)
class StackConfig:
    base_url: str
    dsn: str  # OWNER role DSN — seeding + tamper-injection (DISABLE TRIGGER)
    token: str
    uc_uid: str
    cpa_export_url: str | None
    cpa_export_token: str
    # Least-privilege RUNTIME role DSN. Used by the RLS-backstop +
    # role-can't-bypass tests to connect AS the restricted role. None if the
    # split-role stack isn't configured (those tests then SKIP).
    app_dsn: str | None = None


def _stack_config() -> StackConfig:
    # Defaults match the remapped host ports in docker-compose.itest.yml
    # (app 58000, postgres 55432) so a direct `pytest` run still hits the
    # throwaway stack even without `eval "$(... env)"`.
    base_url = env("ACCTOPS_TEST_BASE_URL", "http://127.0.0.1:58000").rstrip("/")
    pw = env("POSTGRES_PASSWORD", "accounting_ops_test_pw")
    dsn = env(
        "ACCTOPS_TEST_DSN",
        f"postgresql://accounting_ops:{pw}@127.0.0.1:55432/accounting_ops",
    )
    token = env("ACCTOPS_TEST_TOKEN", "")
    uc_uid = env("ACCTOPS_TEST_UC_UID", "test-service")
    cpa_export_url = env("CPA_EXPORT_URL")
    if cpa_export_url:
        cpa_export_url = cpa_export_url.rstrip("/")
    cpa_export_token = env("CPA_EXPORT_TOKEN") or token
    app_dsn = env("ACCTOPS_TEST_APP_DSN")
    return StackConfig(
        base_url=base_url,
        dsn=dsn,
        token=token,
        uc_uid=uc_uid,
        cpa_export_url=cpa_export_url,
        cpa_export_token=cpa_export_token,
        app_dsn=app_dsn,
    )


@pytest.fixture(scope="session")
def stack() -> StackConfig:
    return _stack_config()
