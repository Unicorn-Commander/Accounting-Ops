"""Alembic revision ids must fit alembic_version.version_num.

That column is VARCHAR(32). A longer id runs the whole migration and then dies
on the version stamp — `value too long for type character varying(32)` — so the
work rolls back and the revision never applies. Nothing about the failure names
the actual problem, and it only shows up against a real database, never in the
test suite. The existing ids run to 29 characters, so the margin is three.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
VERSION_NUM_MAX = 32
_REVISION = re.compile(r'^revision:\s*str\s*=\s*["\']([^"\']+)["\']', re.M)


def _revision_files() -> list[pathlib.Path]:
    return sorted(
        p
        for d in ("tenant", "public")
        for p in (REPO_ROOT / "migrations" / d / "versions").glob("*.py")
    )


def test_there_are_migrations_to_check():
    assert _revision_files(), "found no migration files — this test would be vacuous"


@pytest.mark.parametrize("path", _revision_files(), ids=lambda p: p.name)
def test_revision_id_fits_the_version_column(path: pathlib.Path):
    m = _REVISION.search(path.read_text())
    if not m:
        pytest.skip(f"{path.name} declares no revision")
    rev = m.group(1)
    assert len(rev) <= VERSION_NUM_MAX, (
        f"revision id {rev!r} is {len(rev)} chars; alembic_version.version_num "
        f"is VARCHAR({VERSION_NUM_MAX}). This migration would execute in full "
        "and then fail on the stamp, rolling everything back."
    )
