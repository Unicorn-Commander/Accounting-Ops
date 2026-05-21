"""Seed two generic test tenants for local development.

Idempotent. Uses scripts.provision_tenant which is safe to call multiple times.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import engine  # noqa: E402
from scripts.provision_tenant import provision_tenant  # noqa: E402


TENANTS = [
    ("test-tenant-1", "Test Tenant 1"),
    ("test-tenant-2", "Test Tenant 2"),
]


async def main() -> None:
    for slug, display_name in TENANTS:
        # Deterministic UUID so re-running seed yields the same IDs.
        tenant_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"accounting-ops:{slug}")
        try:
            schema = await provision_tenant(engine, tenant_id, display_name, slug=slug)
            print(f"  {slug:<20} -> {schema}")
        except Exception as exc:
            print(f"  {slug:<20} FAILED: {exc}", file=sys.stderr)
    print("seeded generic local tenants")


if __name__ == "__main__":
    asyncio.run(main())
