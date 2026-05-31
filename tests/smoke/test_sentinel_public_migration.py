"""Migration smoke test for 0009_regulatory_feed.py (public schema).

Audit 2026-05-31 Bug 5: renumbered 0008 -> 0009 to resolve two-heads ambiguity
on ``alembic -c alembic-public.ini heads`` (both rbac_expansion and
regulatory_feed had landed as 0008). RBAC came first chronologically, so
regulatory_feed becomes 0009 with down_revision = "0008_rbac_expansion".
"""
from __future__ import annotations

import pathlib

_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "migrations"
    / "public"
    / "versions"
    / "0009_regulatory_feed.py"
)


def _src() -> str:
    return _PATH.read_text(encoding="utf-8")


def test_migration_file_exists():
    assert _PATH.exists()


def test_revision_chain():
    src = _src()
    assert 'revision: str = "0009_regulatory_feed"' in src
    assert 'down_revision: str | None = "0008_rbac_expansion"' in src


def test_table_and_columns():
    src = _src()
    assert "regulatory_feed_entry" in src
    for col in (
        "source", "external_id", "title", "summary", "url",
        "jurisdiction", "tags", "published_at", "first_seen_at", "classified",
    ):
        assert col in src


def test_unique_constraint():
    src = _src()
    assert "regulatory_feed_uq" in src
    assert '"source", "external_id"' in src


def test_indexes():
    src = _src()
    assert "ix_regulatory_feed_unclassified" in src
    assert "ix_regulatory_feed_source_published" in src


def test_grants():
    src = _src()
    assert "GRANT SELECT, INSERT, UPDATE ON public.regulatory_feed_entry TO accounting_ops_app" in src
    assert "REVOKE DELETE, TRUNCATE ON public.regulatory_feed_entry FROM accounting_ops_app" in src
