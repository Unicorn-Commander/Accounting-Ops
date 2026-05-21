import asyncio
import uuid

from sqlalchemy import text

from app.db import engine


TENANTS = [
    ("test-tenant-1", "Test Tenant 1"),
    ("test-tenant-2", "Test Tenant 2"),
]


async def main() -> None:
    async with engine.begin() as conn:
        for slug, display_name in TENANTS:
            tenant_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"accounting-ops:{slug}")
            schema = f"accounting_ops_{str(tenant_id).replace('-', '_')}"
            await conn.execute(
                text(
                    """
                    INSERT INTO tenant (id, slug, display_name, postgres_schema, falkordb_subgraph, garage_bucket)
                    VALUES (:id, :slug, :display_name, :schema, :subgraph, :bucket)
                    ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name
                    """
                ),
                {
                    "id": tenant_id,
                    "slug": slug,
                    "display_name": display_name,
                    "schema": schema,
                    "subgraph": f"tenant_{str(tenant_id).replace('-', '_')}",
                    "bucket": f"accounting-ops-{tenant_id}",
                },
            )
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    print("seeded generic local tenants")


if __name__ == "__main__":
    asyncio.run(main())
