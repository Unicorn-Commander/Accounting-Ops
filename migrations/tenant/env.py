"""Alembic env for per-tenant schemas. Runs against accounting_ops_<tenant_uuid>.

Run: `alembic -c alembic-tenant.ini -x tenant=accounting_ops_<uuid_underscored> upgrade head`

Iterates one tenant per invocation. Use scripts/provision_tenant.py or
scripts/upgrade_all_tenants.py to loop over multiple tenants.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)

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


def do_run_migrations(connection: Connection) -> None:
    schema = _tenant_schema()
    # PG-side: every statement in this Alembic run targets this schema by default.
    connection.execute(text(f'SET search_path = "{schema}", public'))
    # SQLA-side: makes autogen emit unqualified DDL, matching our schema=None models.
    connection.dialect.default_schema_name = schema
    context.configure(
        connection=connection,
        target_metadata=None,
        include_schemas=False,
        version_table="alembic_version",
        version_table_schema=schema,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    import asyncio
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    raise RuntimeError("Offline mode not supported")
run_migrations_online()
