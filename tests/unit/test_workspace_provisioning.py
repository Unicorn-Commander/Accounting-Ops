"""Regression coverage for the shared-schema workspace provisioning contract."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from scripts import provision_workspace as provisioner


class _RecordingDatabase:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []


class _RecordingConnection:
    def __init__(self, database: _RecordingDatabase) -> None:
        self.database = database

    async def execute(self, statement, parameters=None):
        self.database.calls.append((str(statement), dict(parameters or {})))


class _BeginContext:
    def __init__(self, database: _RecordingDatabase) -> None:
        self.database = database

    async def __aenter__(self) -> _RecordingConnection:
        return _RecordingConnection(self.database)

    async def __aexit__(self, *exc) -> bool:
        return False


class _RecordingEngine:
    def __init__(self) -> None:
        self.database = _RecordingDatabase()

    def begin(self) -> _BeginContext:
        return _BeginContext(self.database)


def _workspace_inserts(database: _RecordingDatabase) -> list[tuple[str, dict[str, Any]]]:
    return [
        (sql, params)
        for sql, params in database.calls
        if "INSERT INTO public.workspace" in sql
    ]


@pytest.mark.asyncio
async def test_two_workspaces_provision_into_the_shared_schema(monkeypatch: pytest.MonkeyPatch):
    """Distinct registry rows may both point at the one RLS-protected schema."""
    engine = _RecordingEngine()
    monkeypatch.setattr(provisioner, "ensure_workspace_subgraph", None)
    workspace_a = uuid4()
    workspace_b = uuid4()

    result_a = await provisioner.provision_workspace(
        engine, workspace_a, "Alpha Books", slug="alpha-books"
    )
    result_b = await provisioner.provision_workspace(
        engine, workspace_b, "Beta Books", slug="beta-books"
    )

    assert result_a == (provisioner.SHARED_SCHEMA, [])
    assert result_b == (provisioner.SHARED_SCHEMA, [])

    inserts = _workspace_inserts(engine.database)
    assert len(inserts) == 2
    assert {params["id"] for _, params in inserts} == {str(workspace_a), str(workspace_b)}
    assert [params["schema"] for _, params in inserts] == ["books", "books"]
    assert all("CREATE SCHEMA" not in sql for sql, _ in engine.database.calls)


@pytest.mark.parametrize(
    ("display_name", "expected_slug"),
    [
        ("PVDemo", "pvdemo"),
        ("Acme & Co.", "acme-co"),
        ("---", "workspace"),
        ("x" * 120, "x" * 96),
    ],
)
def test_slug_edge_cases_are_not_schema_derivation(display_name: str, expected_slug: str):
    """Slug normalization remains URL-facing; schema selection stays constant."""
    assert provisioner.slugify(display_name) == expected_slug
    assert provisioner.SHARED_SCHEMA == "books"


@pytest.mark.asyncio
async def test_first_workspace_compat_path_keeps_legacy_registry_column(monkeypatch):
    """The first shared-schema row still exposes postgres_schema='books'."""
    engine = _RecordingEngine()
    monkeypatch.setattr(provisioner, "ensure_workspace_subgraph", None)

    schema, entity_ids = await provisioner.provision_workspace(
        engine,
        uuid4(),
        "Existing Books Workspace",
        slug="existing-books-workspace",
    )

    assert schema == "books"
    assert entity_ids == []
    sql, params = _workspace_inserts(engine.database)[0]
    assert "postgres_schema" in sql
    assert params["schema"] == "books"


def _load_public_migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "public"
        / "versions"
        / "0016_drop_workspace_schema_uq.py"
    )
    spec = importlib.util.spec_from_file_location("public_0016", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_migration_drops_the_obsolete_schema_unique_constraint(monkeypatch):
    migration = _load_public_migration()
    calls: list[str] = []

    class _FakeOperations:
        def execute(self, statement: str) -> None:
            calls.append(statement)

    monkeypatch.setattr(migration, "op", _FakeOperations())
    migration.upgrade()

    assert migration.revision == "0016_drop_workspace_schema_uq"
    assert migration.down_revision == "0015_membership_status"
    assert calls == [
        "ALTER TABLE public.workspace "
        "DROP CONSTRAINT IF EXISTS workspace_postgres_schema_key"
    ]
