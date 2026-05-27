"""Alembic env for per-tenant (workspace) schemas. Runs against accounting_ops_<workspace_uuid>.

Run: `alembic -c alembic-tenant.ini -x tenant=accounting_ops_<uuid_underscored> upgrade head`

Iterates one workspace per invocation. Use scripts/provision_workspace.py to loop.

We use a SYNC SQLAlchemy engine here (not the async one the app runs on).
Reason: the async-wrapper + run_sync() + alembic begin_transaction() chain has
a subtle interaction where alembic's transaction silently rolls back the
migration on commit. Switching to sync (psycopg2) makes the transaction
boundaries crisp and the migration actually applies. The app's runtime engine
stays async.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.config import get_admin_database_url

config = context.config

# Force psycopg2 (sync) for alembic. The app itself uses asyncpg at runtime.
# Migrations create tables, triggers, grants, and RLS policies — they MUST run
# as the table-OWNING role. Use the admin URL (falls back to DATABASE_URL when
# ADMIN_DATABASE_URL is unset, i.e. single-role/local setups).
_url = get_admin_database_url()
if "+asyncpg" in _url:
    _url = _url.replace("+asyncpg", "+psycopg2")
elif "://" in _url and "+" not in _url.split("://", 1)[0]:
    # plain `postgresql://...` already implies psycopg2; leave as-is
    pass
config.set_main_option("sqlalchemy.url", _url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _tenant_schema() -> str:
    x = context.get_x_argument(as_dictionary=True)
    schema = x.get("tenant")
    if not schema:
        raise RuntimeError(
            "Pass -x tenant=<schema_name>. Example: "
            "alembic -c alembic-tenant.ini -x tenant=accounting_ops_xxxx upgrade head"
        )
    # Defensive: only allow our naming convention to avoid SQL injection through the env arg.
    if not schema.startswith("accounting_ops_") or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in schema
    ):
        raise RuntimeError(f"Invalid tenant schema name: {schema!r}")
    return schema


def run_migrations_online() -> None:
    schema = _tenant_schema()
    connectable = create_engine(
        config.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
        # Set search_path at connection time so every statement in the migration
        # is scoped to this workspace's schema by default.
        connect_args={"options": f'-csearch_path="{schema}",public'},
    )
    # SQLAlchemy 2.0: connect() auto-starts an unmanaged transaction that
    # rolls back on close. We need explicit transaction management so the
    # migration's writes COMMIT. begin() does that.
    with connectable.begin() as connection:
        connection.dialect.default_schema_name = schema
        connection.execute(text(f'SET search_path = "{schema}", public'))
        context.configure(
            connection=connection,
            target_metadata=None,
            include_schemas=False,
            version_table="alembic_version",
            version_table_schema=schema,
            compare_type=True,
            compare_server_default=True,
            # We're already inside connectable.begin(); tell alembic not to
            # start its own (which would be a no-op or a savepoint).
            transactional_ddl=False,
        )
        context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline mode not supported")
run_migrations_online()
