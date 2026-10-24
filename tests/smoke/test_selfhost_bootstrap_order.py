"""Static checks on the self-host bootstrap's database steps.

A fresh `make selfhost` must (1) generate the runtime-role password and bake it
into DATABASE_URL, (2) run BOTH migration passes before provisioning, (3) set
the runtime role's password after the migrations create it, and (4) prove that
role can log in, all without ever printing the password.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = (REPO_ROOT / "scripts/selfhost_bootstrap.sh").read_text()


def test_runtime_password_is_generated_and_baked_into_the_urls():
    assert "gen_if_blank APP_DB_PASSWORD" in SOURCE
    assert (
        'set_env DATABASE_URL "postgresql+asyncpg://accounting_ops_app:${_apppw}@'
        in SOURCE
    )
    assert (
        'set_env ADMIN_DATABASE_URL "postgresql+asyncpg://accounting_ops:${_pgpw}@'
        in SOURCE
    )


def test_migrations_then_runtime_password_then_login_probe_then_provisioning():
    public_migration = SOURCE.index("alembic -c alembic-public.ini upgrade head")
    books_schema = SOURCE.index("CREATE SCHEMA IF NOT EXISTS books")
    tenant_pre_data = SOURCE.index(
        'alembic -c alembic-tenant.ini -x tenant=books upgrade "$TENANT_PRE_DATA_REVISION"'
    )
    runtime_password = SOURCE.index("ALTER ROLE accounting_ops_app PASSWORD")
    health_wait = SOURCE.index('log "waiting for the app to become healthy..."')
    login_probe = SOURCE.index('log "checking the runtime database role can log in..."')
    provisioning = SOURCE.index("python -m scripts.provision_workspace")
    tenant_head = SOURCE.index(
        "alembic -c alembic-tenant.ini -x tenant=books upgrade head"
    )
    grant = SOURCE.index("python -m scripts.grant_workspace_admin")

    assert (
        public_migration
        < books_schema
        < tenant_pre_data
        < runtime_password
        < health_wait
        < login_probe
        < provisioning
        < tenant_head
        < grant
    )
    assert 'TENANT_PRE_DATA_REVISION="0029_entity_owner"' in SOURCE
    assert "up -d --build postgres garage ollama app" in SOURCE
    assert "up -d --build app web" in SOURCE


def test_pre_data_revision_matches_the_integration_harness():
    harness = (REPO_ROOT / "scripts/integration_env.py").read_text()
    assert 'TENANT_PRE_DATA_REVISION = "0029_entity_owner"' in harness
    assert (REPO_ROOT / "migrations/tenant/versions/0029_entity_owner.py").exists()


def test_runtime_password_is_passed_by_environment_never_printed():
    assert "APP_DB_PASSWORD=${_runtime_db_password}" in SOURCE
    assert "unset _runtime_db_password _owner_db_password" in SOURCE
    assert 'log "${_runtime_db_password}"' not in SOURCE
    assert 'printf "${_runtime_db_password}' not in SOURCE
    assert 'echo "$_runtime_db_password' not in SOURCE
