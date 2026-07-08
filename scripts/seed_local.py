"""Seed two generic test workspaces with sample entities.

Idempotent. Uses scripts.provision_workspace which is safe to call multiple
times. No PII — generic placeholder names only.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.provision_workspace import provision_workspace  # noqa: E402


WORKSPACES = [
    {
        "slug": "test-workspace-1",
        "name": "Test Workspace 1",
        "entities": [
            ("Test Operating Co", "c_corp"),
            ("Test Founder Personal", "personal"),
        ],
    },
    {
        "slug": "test-workspace-2",
        "name": "Test Workspace 2",
        "entities": [
            ("Test Law Firm PLLC", "llc"),
            ("Test IOLTA Trust", "trust"),
        ],
    },
]


async def main() -> None:
    failed = False
    for ws in WORKSPACES:
        workspace_id = uuid.uuid5(
            uuid.NAMESPACE_DNS, f"accounting-ops:{ws['slug']}"
        )
        try:
            schema, entity_ids = await provision_workspace(
                None,  # use the admin (owner) engine
                workspace_id,
                ws["name"],
                slug=ws["slug"],
                entities=ws["entities"],
            )
            print(f"  {ws['slug']:<20} -> {schema}")
            for eid, (display, kind) in zip(entity_ids, ws["entities"]):
                print(f"    entity: {display:<24} ({kind}) -> {eid}")
        except Exception as exc:
            failed = True
            print(f"  {ws['slug']:<20} FAILED: {exc}", file=sys.stderr)
    if failed:
        raise SystemExit(1)
    print("seeded generic local workspaces")


if __name__ == "__main__":
    asyncio.run(main())
