"""Migration smoke test for 0008_regulatory_feed.py (public schema)."""
from __future__ import annotations

import pathlib

_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "migrations"
    / "public"
    / "versions"
    / "0008_regulatory_feed.py"
)


def _src() -> str:
    return _PATH.read_text(encoding="utf-8")


def test_migration_file_exists():
    assert _PATH.exists()


def test_revision_chain():
    src = _src()
    assert 'revision: str = "0008_regulatory_feed"' in src
    assert 'down_revision: str | None = "0007_tax_chunking"' in src


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
