"""Export unit tests (no database, no network).

Two things are proven here:

  1. **The export contains what it claims.** The manifest's row counts match the
     rows actually written, every discovered table produces both a JSON and a
     CSV member, and the stored files are really in the archive.

  2. **It is tenant-scoped.** A second workspace's rows NEVER appear in the
     first workspace's archive. The fake session below enforces the
     ``workspace_id = :wid`` predicate the way PostgreSQL RLS would, so a code
     change that dropped the predicate — or that read from the wrong workspace —
     fails here rather than in production.

The real database-level proof (that RLS itself is on, and that the GUC is set)
lives in tests/integration/test_data_rights.py, which needs a live stack. This
tier catches the much more likely regression: someone editing the query.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import uuid
import zipfile
from decimal import Decimal

import pytest

from app.config import Settings
from app.data_rights.export import (
    _json_default,
    build_export,
    discover_tenant_tables,
)

WS_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
WS_B = uuid.UUID("22222222-2222-2222-2222-222222222222")

RECEIPT_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
RECEIPT_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")

# Two workspaces' rows in the same physical tables — the real shared-schema
# layout since tenant migration 0026.
DATASET: dict[str, list[dict]] = {
    "entity": [
        {"id": uuid.uuid4(), "workspace_id": WS_A, "name": "Acme Inc", "tax_id_encrypted": b"\x01\x02"},
        {"id": uuid.uuid4(), "workspace_id": WS_B, "name": "SECRET-B-CORP", "tax_id_encrypted": b"\x09"},
    ],
    "journal_entry": [
        {
            "id": uuid.uuid4(),
            "workspace_id": WS_A,
            "amount": Decimal("1234.5600"),
            "payee": "Acme Supplier",
            "posted_at": dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
            "prev_hash": b"\x00" * 32,
            "this_hash": bytes(range(32)),
        },
        {
            "id": uuid.uuid4(),
            "workspace_id": WS_A,
            "amount": Decimal("10.0000"),
            "payee": "Acme Coffee",
            "posted_at": dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc),
            "prev_hash": bytes(range(32)),
            "this_hash": bytes(range(32, 64)),
            "extra_jsonb_col": {"note": "ragged row - column absent from row 1"},
        },
        {
            "id": uuid.uuid4(),
            "workspace_id": WS_B,
            "amount": Decimal("99999.0000"),
            "payee": "SECRET-B-PAYEE",
            "posted_at": dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
            "prev_hash": b"\x00" * 32,
            "this_hash": b"\xff" * 32,
        },
    ],
    "receipt": [
        {
            "id": RECEIPT_A,
            "workspace_id": WS_A,
            "garage_bucket": "bkt-a",
            "object_key": "docs/a/invoice.pdf",
            "mime_type": "application/pdf",
            "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        },
        {
            "id": RECEIPT_B,
            "workspace_id": WS_B,
            "garage_bucket": "bkt-b",
            "object_key": "docs/b/SECRET.pdf",
            "mime_type": "application/pdf",
            "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        },
    ],
    "chart_of_account": [
        {"id": uuid.uuid4(), "workspace_id": WS_A, "code": "1010", "name": "Cash"},
    ],
}

_TABLE_RE = re.compile(r'FROM\s+books\."(?P<t>[a-z_]+)"', re.IGNORECASE)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    """Minimal AsyncSession stand-in that enforces the tenant predicate.

    Deliberately STRICT: if a query arrives without a ``wid`` parameter it
    raises, because a tenant-table read with no workspace binding is exactly the
    bug this test exists to catch.
    """

    def __init__(self, dataset):
        self.dataset = dataset
        self.queries: list[str] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.queries.append(sql)
        params = params or {}

        if "pg_class" in sql:
            return _Result([{"name": t} for t in sorted(self.dataset)])

        match = _TABLE_RE.search(sql)
        if match:
            table = match.group("t")
            if "wid" not in params:
                raise AssertionError(
                    f"tenant read of {table!r} with no workspace binding — "
                    "this would read across tenants"
                )
            wid = uuid.UUID(str(params["wid"]))
            return _Result([r for r in self.dataset.get(table, []) if r["workspace_id"] == wid])

        raise AssertionError(f"unexpected SQL in export path: {sql[:120]}")


class FakeS3:
    def __init__(self, *_a, **_kw):
        self.fetched: list[str] = []

    def is_configured(self):
        return True

    async def get_object(self, *, bucket, object_key, timeout_s=None):
        self.fetched.append(object_key)
        return f"CONTENT-OF:{object_key}".encode()


def _settings():
    return Settings(
        garage_s3_endpoint="http://garage:3900",
        garage_s3_access_key="AK",
        garage_s3_secret_key="SK",
        garage_bucket_prefix="accounting-ops",
    )


async def _build(monkeypatch, workspace_id):
    fake_s3 = FakeS3()
    monkeypatch.setattr("app.data_rights.export.GarageS3Client", lambda *a, **k: fake_s3)
    session = FakeSession(DATASET)
    data, result = await build_export(
        session,
        workspace_id,
        requested_by_uc_uid="owner-1",
        workspace_slug="acme",
        settings=_settings(),
    )
    return data, result, fake_s3


# --------------------------------------------------------------------------- #
async def test_discovery_finds_every_workspace_scoped_table():
    tables = await discover_tenant_tables(FakeSession(DATASET))
    assert tables == sorted(DATASET)


async def test_export_contains_json_and_csv_for_every_table(monkeypatch):
    data, result, _ = await _build(monkeypatch, WS_A)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    for table in DATASET:
        assert f"json/{table}.json" in names
        assert f"csv/{table}.csv" in names
    assert "manifest.json" in names
    assert result.archive_bytes == len(data)


async def test_manifest_row_counts_match_the_json_actually_written(monkeypatch):
    """The 'contains what it claims' property."""
    data, _, _ = await _build(monkeypatch, WS_A)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        for table, claimed in manifest["contents"]["tables"].items():
            actual = len(json.loads(zf.read(f"json/{table}.json")))
            assert actual == claimed, f"{table}: manifest claims {claimed}, archive has {actual}"
    # Sanity: workspace A really does have the rows we seeded.
    assert manifest["contents"]["tables"]["journal_entry"] == 2
    assert manifest["contents"]["tables"]["entity"] == 1
    assert manifest["contents"]["total_rows"] == 5


async def test_cross_tenant_isolation_workspace_b_never_appears(monkeypatch):
    """The load-bearing security test.

    Every byte of workspace A's archive is scanned for workspace B's identifiers
    and its distinctive string data. Nothing may match.
    """
    data, _, fake_s3 = await _build(monkeypatch, WS_A)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())

    for needle in (
        str(WS_B).encode(),
        b"SECRET-B-CORP",
        b"SECRET-B-PAYEE",
        b"SECRET.pdf",
        str(RECEIPT_B).encode(),
        b"99999",
    ):
        assert needle not in blob, f"workspace B data leaked into A's export: {needle!r}"

    # And we never even asked the object store for B's file.
    assert fake_s3.fetched == ["docs/a/invoice.pdf"]


async def test_export_for_b_gets_b_data_only(monkeypatch):
    """Symmetry check — the scoping is by argument, not an accident of ordering."""
    data, result, _ = await _build(monkeypatch, WS_B)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
        entities = json.loads(zf.read("json/entity.json"))
    assert len(entities) == 1
    assert entities[0]["name"] == "SECRET-B-CORP"
    assert b"Acme Supplier" not in blob
    assert result.files_included == 1


async def test_stored_files_are_included_with_their_bytes(monkeypatch):
    data, result, _ = await _build(monkeypatch, WS_A)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        member = f"files/{RECEIPT_A}/invoice.pdf"
        assert member in zf.namelist()
        assert zf.read(member) == b"CONTENT-OF:docs/a/invoice.pdf"
    assert result.files_included == 1
    assert result.files_skipped == []


async def test_unconfigured_object_store_lists_skipped_files_loudly(monkeypatch):
    """Files must never be dropped silently — they are named in the manifest."""

    class Unconfigured(FakeS3):
        def is_configured(self):
            return False

    monkeypatch.setattr("app.data_rights.export.GarageS3Client", lambda *a, **k: Unconfigured())
    session = FakeSession(DATASET)
    data, result = await build_export(
        session, WS_A, requested_by_uc_uid="o", workspace_slug="acme", settings=_settings()
    )
    assert result.files_included == 0
    assert [s.reason for s in result.files_skipped] == ["object_store_not_configured"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    skipped = manifest["contents"]["files_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["object_key"] == "docs/a/invoice.pdf"


async def test_money_precision_and_hash_chain_survive_the_round_trip(monkeypatch):
    """Decimals must not become floats, and BYTEA hashes must be readable.

    The export is worthless as a portability artifact if the customer cannot
    re-verify their own hash chain from it.
    """
    data, _, _ = await _build(monkeypatch, WS_A)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        rows = json.loads(zf.read("json/journal_entry.json"))
    amounts = {r["amount"] for r in rows}
    assert "1234.5600" in amounts  # exact string, no float rounding
    assert all(isinstance(r["amount"], str) for r in rows)
    row = next(r for r in rows if r["payee"] == "Acme Supplier")
    assert row["prev_hash"] == "00" * 32
    assert row["this_hash"] == bytes(range(32)).hex()


async def test_csv_header_is_the_union_of_ragged_rows(monkeypatch):
    """A JSONB-bearing table yields ragged dicts; a header from row 0 alone
    would silently drop a column."""
    data, _, _ = await _build(monkeypatch, WS_A)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        csv_text = zf.read("csv/journal_entry.csv").decode()
    header = csv_text.splitlines()[0]
    assert "extra_jsonb_col" in header
    assert "payee" in header


def test_json_default_handles_every_type_this_schema_produces():
    assert _json_default(Decimal("1.50")) == "1.50"
    assert _json_default(b"\x00\xff") == "00ff"
    assert _json_default(memoryview(b"\x01")) == "01"
    assert _json_default(uuid.UUID(int=1)) == "00000000-0000-0000-0000-000000000001"
    assert _json_default(dt.date(2026, 7, 20)) == "2026-07-20"
    assert _json_default(dt.timedelta(seconds=90)) == 90.0
    assert _json_default({"a"}) == ["a"]


async def test_tenant_read_without_a_workspace_binding_is_a_hard_error():
    """Guards the fake itself: the strictness this suite relies on is real."""
    session = FakeSession(DATASET)
    with pytest.raises(AssertionError, match="no workspace binding"):
        await session.execute('SELECT * FROM books."entity" WHERE 1=1', {})
