"""AO-IDOR-1: defence-in-depth workspace scoping on two raw UPDATEs.

Two UPDATE statements addressed a row by primary key alone, so a caller who
knows (or guesses) an id from ANOTHER workspace could mutate it:

  * app/api/members.py resend_invitation -- rotated the invitation token of any
    workspace_invitation whose id matched, ignoring workspace_id. A cross-
    workspace id would have re-issued a fresh link for someone else's invite.
  * app/security/credentials.py get_credential_plaintext -- bumped
    tenant_credentials.last_used_at for any id, ignoring workspace_id.

Both now carry ``AND workspace_id = :workspace_id`` (the FORCE-RLS part of the
finding is a separate, larger decision and is deliberately NOT done here).

These tests seed an in-memory sqlite database with the two tables, run the
EXACT UPDATE statements from the source, and assert a cross-workspace id
changes nothing while the same-workspace id still works. No network, no docker.
"""
from __future__ import annotations

import pathlib
import re

import pytest
from sqlalchemy import text

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Two distinct workspace ids (fictional).
WS_A = "11111111-2222-3333-4444-555555555555"
WS_B = "22222222-3333-4444-5555-666666666666"


def _invitation_update_sql() -> str:
    """Extract the resend UPDATE from app/api/members.py and return it."""
    src = (REPO_ROOT / "app" / "api" / "members.py").read_text()
    m = re.search(
        r"UPDATE public\.workspace_invitation\s+SET token_hash = :hash.*?WHERE id = :id.*?\"",
        src,
        re.DOTALL,
    )
    assert m, "could not find the resend_invitation UPDATE in members.py"
    return m.group(0).rstrip().rstrip('"')


def _credential_update_sql() -> str:
    """Extract the last_used_at UPDATE from app/security/credentials.py.

    The statement is split across two adjacent string literals in the source
    (one ending in ``now() "`` and the next starting with ``"WHERE``). Grab the
    two lines and pull the SQL text from each quoted fragment.
    """
    lines = (REPO_ROOT / "app" / "security" / "credentials.py").read_text().splitlines()
    frags: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if "UPDATE public.tenant_credentials SET last_used_at" in stripped:
            # This line: ... now() "
            # Pull everything after the opening quote of the SQL literal.
            m = re.search(r'"(UPDATE.*?now\(\) )?"', line)
            if m:
                frags.append(m.group(1).rstrip())
            # The next line(s) continue the SQL until the closing text( ... ),
            j = i + 1
            while j < len(lines) and "workspace_id" not in lines[j]:
                j += 1
            if j < len(lines):
                m2 = re.search(r'"(WHERE.*?)"', lines[j])
                if m2:
                    frags.append(m2.group(1))
            break
    assert frags, "could not extract the last_used_at UPDATE from credentials.py"
    return " ".join(f.strip() for f in frags)


@pytest.fixture()
def engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    eng = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    from sqlalchemy import event

    @event.listens_for(eng.sync_engine, "connect")
    def _fk_on(dbapi_conn, _):
        # sqlite has no now(); register it so the extracted UPDATE runs verbatim.
        dbapi_conn.create_function("now", 0, lambda: "2026-11-24 10:00:00+00")

    yield eng
    eng.sync_engine.dispose()


async def test_resend_invitation_update_is_workspace_scoped(engine):
    """A cross-workspace invitation id must change nothing on resend."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE workspace_invitation (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    email TEXT,
                    token_hash TEXT,
                    resent_count INTEGER DEFAULT 0,
                    last_resent_at TIMESTAMP,
                    accepted_at TIMESTAMP,
                    cancelled_at TIMESTAMP
                )
                """
            )
        )
        # An invitation in workspace A. An attacker from workspace B tries to
        # resend it by id.
        await conn.execute(
            text(
                "INSERT INTO workspace_invitation "
                "(id, workspace_id, email, token_hash, resent_count) "
                "VALUES (:id, :ws, 'victim@example.com', 'oldhash', 0)"
            ),
            {"id": "inv-1", "ws": WS_A},
        )

    sql = _invitation_update_sql()
    # Strip the `public.` schema qualifier (sqlite has no such schema).
    sql = sql.replace("public.", "")

    # 1) Cross-workspace resend (the attack): workspace B's id, victim's row id.
    async with engine.begin() as conn:
        res = await conn.execute(
            text(sql),
            {"id": "inv-1", "hash": "attacker-hash", "wid": WS_B},
        )
        assert res.rowcount == 0, (
            "cross-workspace resend UPDATED another tenant's invitation"
        )

    # The victim's invitation must be untouched.
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT token_hash, resent_count FROM workspace_invitation "
                        "WHERE id = 'inv-1'"
                    )
                )
            )
            .mappings()
            .first()
        )
    assert row["token_hash"] == "oldhash", "victim invitation token was rotated"
    assert row["resent_count"] == 0, "victim invitation resent_count was bumped"

    # 2) Same-workspace resend still works (no regression).
    async with engine.begin() as conn:
        res = await conn.execute(
            text(sql),
            {"id": "inv-1", "hash": "newhash", "wid": WS_A},
        )
        assert res.rowcount == 1, "same-workspace resend no longer works"
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT token_hash, resent_count FROM workspace_invitation "
                        "WHERE id = 'inv-1'"
                    )
                )
            )
            .mappings()
            .first()
        )
    assert row["token_hash"] == "newhash"
    assert row["resent_count"] == 1


async def test_credential_last_used_update_is_workspace_scoped(engine):
    """A cross-workspace credential id must not bump last_used_at."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE tenant_credentials (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    connector TEXT,
                    label TEXT,
                    key_version INTEGER,
                    encrypted_at TIMESTAMP,
                    last_used_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                "INSERT INTO tenant_credentials "
                "(id, workspace_id, connector, label, key_version, encrypted_at, last_used_at) "
                "VALUES (:id, :ws, 'simplefin', 'Checking', 1, '2026-01-01', NULL)"
            ),
            {"id": "cred-1", "ws": WS_A},
        )

    sql = _credential_update_sql().replace("public.", "")

    # 1) Cross-workspace touch (the attack): workspace B tries to bump A's cred.
    async with engine.begin() as conn:
        res = await conn.execute(
            text(sql),
            {"id": "cred-1", "workspace_id": WS_B},
        )
        assert res.rowcount == 0, (
            "cross-workspace last_used_at touch UPDATED another tenant's credential"
        )

    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT last_used_at FROM tenant_credentials WHERE id = 'cred-1'"
                    )
                )
            )
            .mappings()
            .first()
        )
    assert row["last_used_at"] is None, "victim credential last_used_at was bumped"

    # 2) Same-workspace touch still works (no regression).
    async with engine.begin() as conn:
        res = await conn.execute(
            text(sql),
            {"id": "cred-1", "workspace_id": WS_A},
        )
        assert res.rowcount == 1, "same-workspace last_used_at touch no longer works"
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT last_used_at FROM tenant_credentials WHERE id = 'cred-1'"
                    )
                )
            )
            .mappings()
            .first()
        )
    assert row["last_used_at"] is not None, "same-workspace touch did not bump last_used_at"
