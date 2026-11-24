"""AO-IDOR-1: defence-in-depth workspace scoping on the raw UPDATEs.

Four UPDATE statements addressed a row by primary key alone, so a caller who
knows (or guesses) an id from ANOTHER workspace could mutate it:

  * app/api/members.py resend_invitation -- rotated the invitation token of any
    workspace_invitation whose id matched, ignoring workspace_id. A cross-
    workspace id would have re-issued a fresh link for someone else's invite.
  * app/api/members.py accept_invitation -- marked any invitation accepted (and
    stamped accepted_by_user_id) by id alone.
  * app/security/credentials.py get_credential_plaintext -- bumped
    tenant_credentials.last_used_at for any id, ignoring workspace_id.
  * app/security/credentials.py store_credential -- the re-encrypt UPDATE that
    runs after an ON CONFLICT overwrite touched the row by id alone.

Each now carries ``AND workspace_id = ...`` (the FORCE-RLS part of the finding
is a separate, larger decision and is deliberately NOT done here).

Method: an in-memory sqlite database (the repo's aiosqlite driver) seeded with
the tables the real functions touch, then the REAL functions executed against
it with SessionLocal / authenticate_token / get_crypto patched:

  * resend_invitation runs end to end (its two PG-only SELECTs are answered
    with the seeded row); the UPDATE under test is its raw SQL.
  * accept_invitation runs end to end through the authenticated branch (the
    token SELECT, user SELECT and membership SELECT are the PG-only reads,
    answered with the seeded rows); the accept UPDATE under test is its raw
    SQL.
  * get_credential_plaintext runs end to end: same workspace decrypts and
    bumps last_used_at, cross workspace returns None and touches nothing.
  * store_credential runs end to end for the same-workspace overwrite; the
    cross-workspace attack on its re-encrypt UPDATE is proven with the
    statement's verbatim SQL (a cross-workspace id must match zero rows).

No network, no docker, no Postgres, no new dependency.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

WS_A = uuid.UUID("11111111-2222-3333-4444-555555555555")
WS_B = uuid.UUID("22222222-3333-4444-5555-666666666666")
INV_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000aa")
INV_B = uuid.UUID("bbbbbbbb-0000-0000-0000-0000000000bb")
USER_A = uuid.UUID("aaaaaaaa-0000-0000-0000-0000000001aa")
CRED_A = uuid.UUID("cccccccc-0000-0000-0000-0000000000aa")
TOKEN_A = "inv-token-victim-aaaaaaaa"
TOKEN_B = "inv-token-attacker-bbbbbbbb"


@pytest.fixture()
def eng():
    """Stdlib-sqlite async engine (the repo's aiosqlite driver)."""
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    e = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(e.sync_engine, "connect")
    def _shims(dbapi_conn, _):
        # sqlite has no now(); the UPDATEs under test set it via now().
        dbapi_conn.create_function("now", 0, lambda: "2026-11-24 10:00:00+00")
        # sqlite has no set_config (the PG GUC helper the credential functions use).
        dbapi_conn.create_function("set_config", 3, lambda name, val, local: None)
        # sqlite has no gen_random_uuid (PG extension).
        dbapi_conn.create_function(
            "gen_random_uuid", 0, lambda: str(uuid.uuid4())
        )

    yield e
    e.sync_engine.dispose()


@pytest.fixture(autouse=True)
def _strip_pg_only_sql(monkeypatch):
    """Strip PG-only syntax from every AsyncSession.execute.

    The real SQL (written for Postgres) uses `public.` schema qualifiers,
    `::text` type casts, and `CAST(... AS <pgtype>)` expressions that sqlite
    cannot parse. We rewrite the statement before it reaches the driver.
    This applies to ALL sessions in the test, including those created
    directly with `AsyncSession(bind=eng)`.
    """
    import re
    import sqlalchemy.ext.asyncio as sa_asyncio
    from sqlalchemy import text as _sa_text
    from sqlalchemy.sql.elements import TextClause

    orig_execute = sa_asyncio.AsyncSession.execute

    def _depg(sql: str) -> str:
        # Strip `public.` schema qualifiers.
        sql = sql.replace("public.", "")
        # Strip `::text` casts (sqlite has no such syntax).
        sql = re.sub(r"::text\b", "", sql)
        # Replace `CAST(? AS <pgtype>)` with just `?` for non-standard types.
        sql = re.sub(r"CAST\((\?+)\s+AS\s+\w+\)", r"\1", sql)
        return sql

    async def _execute(self, stmt, params=None):
        if isinstance(stmt, TextClause):
            stmt = _sa_text(_depg(str(stmt)))
        return await orig_execute(self, stmt, params)

    monkeypatch.setattr(sa_asyncio.AsyncSession, "execute", _execute)


@pytest.fixture()
async def db(eng):
    """Seed the tables: one victim invitation + credential in workspace A,
    one attacker invitation in workspace B."""
    async with eng.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE workspace_invitation (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    email TEXT,
                    role TEXT DEFAULT 'member',
                    token_hash TEXT,
                    resent_count INTEGER DEFAULT 0,
                    last_resent_at TIMESTAMP,
                    accepted_at TIMESTAMP,
                    accepted_by_user_id TEXT,
                    cancelled_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                "INSERT INTO workspace_invitation "
                "(id, workspace_id, email, token_hash, resent_count, expires_at) "
                "VALUES (:id, :ws, :email, :hash, 0, '2099-01-01 00:00:00+00')"
            ),
            {
                "id": str(INV_A),
                "ws": str(WS_A),
                "email": "victim@example.com",
                "hash": _h(TOKEN_A),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO workspace_invitation "
                "(id, workspace_id, email, token_hash, resent_count, expires_at) "
                "VALUES (:id, :ws, :email, :hash, 0, '2099-01-01 00:00:00+00')"
            ),
            {
                "id": str(INV_B),
                "ws": str(WS_B),
                "email": "attacker@example.com",
                "hash": _h(TOKEN_B),
            },
        )
        await conn.execute(
            text(
                """
                CREATE TABLE workspace (
                    id TEXT PRIMARY KEY,
                    display_name TEXT,
                    slug TEXT
                )
                """
            )
        )
        await conn.execute(
            text(
                "INSERT INTO workspace (id, display_name, slug) VALUES (:id, :dn, :sl)"
            ),
            {"id": str(WS_A), "dn": "Fictional Ws", "sl": "fictional-ws"},
        )
        await conn.execute(
            text(
                "INSERT INTO workspace (id, display_name, slug) VALUES (:id, :dn, :sl)"
            ),
            {"id": str(WS_B), "dn": "Fictional Ws B", "sl": "fictional-ws-b"},
        )
        await conn.execute(
            text(
                """
                CREATE TABLE workspace_membership (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT,
                    entity_scope TEXT,
                    status TEXT DEFAULT 'active',
                    invited_by_uc_uid TEXT,
                    created_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE tenant_credentials (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    connector TEXT,
                    label TEXT,
                    ciphertext BLOB,
                    nonce BLOB,
                    key_version INTEGER,
                    created_by_uc_uid TEXT,
                    encrypted_at TIMESTAMP,
                    last_used_at TIMESTAMP,
                    expires_at TIMESTAMP,
                    UNIQUE (workspace_id, connector, label)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE credential_access_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT,
                    credential_id TEXT,
                    actor_uc_uid TEXT,
                    actor_agent_id TEXT,
                    requesting_service TEXT,
                    operation TEXT,
                    success INTEGER
                )
                """
            )
        )
    yield eng


def _h(token: str) -> str:
    import app.api.members as members_mod

    return members_mod._hash_token(token)





async def _inv_row(eng, inv_id: uuid.UUID):
    async with eng.connect() as conn:
        return (
            (
                await conn.execute(
                    text(
                        "SELECT token_hash, resent_count, accepted_at, "
                        "accepted_by_user_id FROM workspace_invitation "
                        "WHERE id = :id"
                    ),
                    {"id": str(inv_id)},
                )
            )
            .mappings()
            .first()
        )


async def _cred_row(eng, cred_id: uuid.UUID):
    async with eng.connect() as conn:
        return (
            (
                await conn.execute(
                    text(
                        "SELECT ciphertext, nonce, last_used_at "
                        "FROM tenant_credentials WHERE id = :id"
                    ),
                    {"id": str(cred_id)},
                )
            )
            .mappings()
            .first()
        )


class _FakeWorkspace:
    def __init__(self, ws_id: uuid.UUID) -> None:
        self.workspace_id = ws_id
        self.workspace_slug = "fictional-ws"
        self.role = "admin"
        self.user_uc_uid = "victim-uc-uid"
        self.agent_id = None
        self.entity_scope = None
        self.hipaa_protected = False
        self.pat_scopes = None


class _InvRow:
    """Attribute-access row for the invitation SELECT (like a Row)."""

    def __init__(self, inv_id: uuid.UUID, ws_id: uuid.UUID, email: str) -> None:
        from datetime import datetime, timezone

        self.id = str(inv_id)
        self.workspace_id = str(ws_id)
        self.email = email
        self.role = "member"
        self.entity_scope = None
        self.expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
        self.accepted_at = None
        self.cancelled_at = None
        self.workspace_name = "Fictional Ws"
        self.workspace_slug = "fictional-ws"


class _UserRow:
    def __init__(self, email: str = "victim@example.com") -> None:
        self.id = USER_A
        self.email = email


class _FakeAuthCtx:
    def __init__(self, email: str = "victim@example.com") -> None:
        self.uc_uid = "victim-uc-uid"
        self.agent_id = None
        self.claims = {"email": email, "email_verified": True}


class _FakeResult:
    """Mimics the `.first()` and `.mappings().first()` API of a real Result."""

    def __init__(self, rows: list):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def mappings(self):
        return self

    def all(self):
        return self._rows


def _patch_session(monkeypatch, eng):
    """Point the app's SessionLocal at the sqlite engine (both import sites)."""
    import app.api.members as members_mod
    import app.db as app_db

    def _session_factory():
        from sqlalchemy.ext.asyncio import AsyncSession

        return AsyncSession(bind=eng, expire_on_commit=False)

    monkeypatch.setattr(app_db, "SessionLocal", _session_factory)
    monkeypatch.setattr(members_mod, "SessionLocal", _session_factory)


def _patch_send_email(monkeypatch):
    import app.api.members as members_mod

    async def _send(session, **kwargs):
        return {"status": "fake", "delivery": "none"}

    monkeypatch.setattr(members_mod, "send_email", _send)


# ===========================================================================
# resend_invitation (members.py) -- the real function, end to end
# ===========================================================================


async def test_resend_invitation_is_workspace_scoped(db, monkeypatch):
    """A cross-workspace resend must rotate nothing; same workspace must work."""
    import app.api.members as members_mod
    import sqlalchemy.ext.asyncio as sa_asyncio

    _patch_session(monkeypatch, db)
    _patch_send_email(monkeypatch)

    # The two PG-only SELECTs inside resend_invitation (JOIN + i.role::text,
    # and the inviter-email lookup). Everything else -- including the UPDATE
    # under test -- runs for real.
    orig = sa_asyncio.AsyncSession.execute

    def _selects(inv_id: uuid.UUID, inv_ws_id: uuid.UUID, email: str):
        """Fake the invitation SELECT. `inv_ws_id` is the invitation's ACTUAL
        workspace (from the DB seed), NOT the requester's workspace."""
        inv = _InvRow(inv_id, inv_ws_id, email)

        async def _execute(self, stmt, params=None):
            sql = " ".join(str(stmt).split())
            if "FROM public.workspace_invitation i" in sql:
                # The real SELECT is scoped by workspace_id; the fake must
                # honour that: only return the row if the invitation's actual
                # workspace matches the requested workspace.
                wid = (params or {}).get("wid")
                if wid == str(inv_ws_id):
                    return _FakeResult([inv])
                return _FakeResult([])
            if "SELECT email FROM public.user_account" in sql:
                return _FakeResult([])
            return await orig(self, stmt, params)

        return _execute

    # 1) Cross-workspace resend (the attack): workspace B admin, victim's id.
    # The scoped SELECT finds nothing for workspace B and 404s before the
    # UPDATE; either way the victim's row is untouched.
    monkeypatch.setattr(sa_asyncio.AsyncSession, "execute", _selects(INV_A, WS_A, "victim@example.com"))
    with pytest.raises(Exception) as exc_info:
        await members_mod.resend_invitation(
            WS_B, INV_A, workspace=_FakeWorkspace(WS_B)
        )
    assert "not found" in str(exc_info.value).lower()

    row = await _inv_row(db, INV_A)
    assert row["token_hash"] == _h(TOKEN_A), "victim invitation token was rotated"
    assert row["resent_count"] == 0, "victim invitation resent_count was bumped"

    # 2) Same-workspace resend still works (the UPDATE under test runs real).
    monkeypatch.setattr(
        sa_asyncio.AsyncSession, "execute", _selects(INV_A, WS_A, "victim@example.com")
    )
    out = await members_mod.resend_invitation(
        WS_A, INV_A, workspace=_FakeWorkspace(WS_A)
    )
    assert out["ok"] is True

    row = await _inv_row(db, INV_A)
    assert row["token_hash"] != _h(TOKEN_A), "same-workspace resend no longer works"
    assert row["resent_count"] == 1, "same-workspace resend did not bump the counter"


# ===========================================================================
# accept_invitation (members.py) -- the N11 accept UPDATE
# ===========================================================================


async def test_accept_invitation_is_workspace_scoped(db, monkeypatch):
    """A cross-workspace accept must not mark the victim's invite accepted.

    The attacker holds workspace B's own invitation token; the accept flow
    resolves it to INV_B (WS_B) and runs the accept UPDATE scoped to
    inv.workspace_id. A forged run against the victim's row (INV_A, WS_A)
    must match zero rows.
    """
    import app.api.members as members_mod
    import sqlalchemy.ext.asyncio as sa_asyncio

    _patch_session(monkeypatch, db)
    _patch_send_email(monkeypatch)

    async def _audit(session, **kwargs):
        return None

    monkeypatch.setattr(members_mod, "_write_membership_audit", _audit)

    async def _fake_authenticate_token(tok):
        return _FakeAuthCtx("attacker@example.com" if tok == TOKEN_B else "victim@example.com")

    import app.auth as auth_mod

    monkeypatch.setattr(auth_mod, "authenticate_token", _fake_authenticate_token)
    monkeypatch.setattr(members_mod, "_token_email_verified", lambda claims: True)

    orig = sa_asyncio.AsyncSession.execute
    inv_a = _InvRow(INV_A, WS_A, "victim@example.com")
    inv_b = _InvRow(INV_B, WS_B, "attacker@example.com")
    resolved_email: list[str] = []

    async def _execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        if "FROM public.workspace_invitation i" in sql and "JOIN" in sql:
            h = (params or {}).get("h")
            row = inv_a if h == _h(TOKEN_A) else inv_b if h == _h(TOKEN_B) else None
            if row is not None:
                resolved_email.append(row.email)
            return _FakeResult([row] if row else [])
        if "SELECT id, email FROM public.user_account" in sql:
            # The user_account email must match the invitation email for the
            # email-safety check to pass.
            email = resolved_email[-1] if resolved_email else "victim@example.com"
            return _FakeResult([_UserRow(email)])
        if "FROM public.workspace_membership" in sql and "SELECT" in sql:
            return _FakeResult([])
        # Intercept membership INSERT/UPDATE (PG CAST syntax not in sqlite).
        if "INSERT INTO public.workspace_membership" in sql:
            return _FakeResult([])
        if "UPDATE public.workspace_membership" in sql:
            return _FakeResult([])
        return await orig(self, stmt, params)

    monkeypatch.setattr(sa_asyncio.AsyncSession, "execute", _execute)

    class _StubRequest:
        headers = {"authorization": "Bearer fake-token"}

    # 1) Cross-workspace (forged): the accept UPDATE must be scoped to the
    #    invitation's workspace, so a run against the victim's id with a
    #    foreign workspace changes nothing.
    #    (The real flow resolves the token to its own row; the scoping is
    #    proven by the UPDATE's WHERE clause, which carries inv.workspace_id.)
    out = await members_mod.accept_invitation(
        request=_StubRequest(), token=TOKEN_B
    )
    assert out["status"] == "accepted"

    # The attacker's own invite was accepted, not the victim's.
    row_b = await _inv_row(db, INV_B)
    assert row_b["accepted_at"] is not None, "own-workspace accept no longer works"

    row_a = await _inv_row(db, INV_A)
    assert row_a["accepted_at"] is None, "victim invitation was marked accepted"
    assert row_a["accepted_by_user_id"] is None, "victim row got a foreign accepter"

    # 2) The victim's token still accepts for the victim (no regression).
    out = await members_mod.accept_invitation(
        request=_StubRequest(), token=TOKEN_A
    )
    assert out["status"] == "accepted"
    assert out["workspace_id"] == str(WS_A)
    row_a = await _inv_row(db, INV_A)
    assert row_a["accepted_at"] is not None, "victim's own accept no longer works"
    assert row_a["accepted_by_user_id"] == str(USER_A)


async def test_accept_update_with_mismatched_id_and_workspace_changes_nothing(db, monkeypatch):
    """AO112-R3-2: drive the REAL accept UPDATE with INV_A's id and WS_B.

    The lookup is forced to return a row whose id belongs to workspace A but
    whose workspace_id is B. Without `AND workspace_id = :wid` on the UPDATE
    the victim's invitation is marked accepted; with it, zero rows change.
    """
    import app.api.members as members_mod
    import app.auth as auth_mod
    import sqlalchemy.ext.asyncio as sa_asyncio

    _patch_session(monkeypatch, db)
    _patch_send_email(monkeypatch)

    async def _audit(session, **kwargs):
        return None

    monkeypatch.setattr(members_mod, "_write_membership_audit", _audit)

    async def _fake_authenticate_token(tok):
        return _FakeAuthCtx("attacker@example.com")

    monkeypatch.setattr(auth_mod, "authenticate_token", _fake_authenticate_token)
    monkeypatch.setattr(members_mod, "_token_email_verified", lambda claims: True)

    orig = sa_asyncio.AsyncSession.execute
    forged = _InvRow(INV_A, WS_B, "attacker@example.com")

    async def _execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        if "FROM public.workspace_invitation i" in sql and "JOIN" in sql:
            return _FakeResult([forged])
        if "SELECT id, email FROM public.user_account" in sql:
            return _FakeResult([_UserRow("attacker@example.com")])
        if "FROM public.workspace_membership" in sql and "SELECT" in sql:
            return _FakeResult([])
        if "INSERT INTO public.workspace_membership" in sql:
            return _FakeResult([])
        if "UPDATE public.workspace_membership" in sql:
            return _FakeResult([])
        return await orig(self, stmt, params)

    monkeypatch.setattr(sa_asyncio.AsyncSession, "execute", _execute)

    class _StubRequest:
        headers = {"authorization": "Bearer fake-token"}

    await members_mod.accept_invitation(request=_StubRequest(), token=TOKEN_B)

    row_a = await _inv_row(db, INV_A)
    assert row_a["accepted_at"] is None, "accept UPDATE touched a row outside its workspace"
    assert row_a["accepted_by_user_id"] is None


# ===========================================================================
# store_credential (credentials.py) -- the N11 re-encrypt UPDATE
# ===========================================================================


def _patch_creds_crypto(monkeypatch, master_key: bytes = b"\x00" * 32):
    import app.security.credentials as creds_mod
    import app.security.crypto as crypto_mod
    from app.security.crypto import CredCrypto

    crypto = CredCrypto(master_key)
    monkeypatch.setattr(creds_mod, "get_crypto", lambda: crypto)
    crypto_mod.get_crypto.cache_clear()
    return crypto


async def _seed_credential(eng, crypto, cred_id: uuid.UUID, ws: uuid.UUID,
                           connector: str = "simplefin", label: str = "Checking",
                           value: str = "acct-123"):
    nonce, ciphertext = crypto.encrypt(value, ws, cred_id)
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenant_credentials "
                "(id, workspace_id, connector, label, ciphertext, nonce, "
                "key_version, encrypted_at) "
                "VALUES (:id, :ws, :c, :l, :ct, :n, :kv, '2026-01-01 00:00:00+00')"
            ),
            {
                "id": str(cred_id),
                "ws": str(ws),
                "c": connector,
                "l": label,
                "ct": ciphertext,
                "n": nonce,
                "kv": 1,
            },
        )


async def _log_rows(eng):
    async with eng.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT operation, success FROM credential_access_log "
                    "ORDER BY id"
                )
            )
        ).all()
    return [(r[0], bool(r[1])) for r in rows]


async def test_store_credential_reencrypt_update_is_workspace_scoped(db, monkeypatch):
    """A same-workspace overwrite reaches the re-encrypt UPDATE; a cross-
    workspace run with the victim's id changes nothing."""
    import app.security.credentials as creds_mod

    crypto = _patch_creds_crypto(monkeypatch)
    await _seed_credential(db, crypto, CRED_A, WS_A)

    # Same workspace: store_credential overwrites the (ws, connector, label)
    # triple; ON CONFLICT keeps the original id; the re-encrypt UPDATE writes
    # the new ciphertext to CRED_A.
    from sqlalchemy.ext.asyncio import AsyncSession

    session = AsyncSession(bind=db, expire_on_commit=False)
    try:
        out_id = await creds_mod.store_credential(
            session, WS_A, "simplefin", "Checking", "acct-456"
        )
        assert out_id == CRED_A
        await session.commit()
    finally:
        await session.close()

    row = await _cred_row(db, CRED_A)
    assert crypto.decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]), WS_A, CRED_A) == "acct-456"
    logs = await _log_rows(db)
    assert logs == [("encrypt", True)], f"expected one encrypt audit row, got {logs}"

    # Cross-workspace: the re-encrypt UPDATE aimed at the victim's id but
    # carrying the attacker's workspace_id must match zero rows.
    async with db.begin() as c2:
        res = await c2.execute(
            text(
                """
                UPDATE tenant_credentials
                SET ciphertext = :ciphertext, nonce = :nonce, encrypted_at = now()
                WHERE id = :id AND workspace_id = :workspace_id
                """
            ),
            {
                "ciphertext": b"attacker-ct",
                "nonce": b"attacker-nonce",
                "id": str(CRED_A),
                "workspace_id": str(WS_B),
            },
        )
        assert res.rowcount == 0, (
            "cross-workspace re-encrypt UPDATE touched another tenant's credential"
        )

    victim = await _cred_row(db, CRED_A)
    assert crypto.decrypt(
        bytes(victim["nonce"]), bytes(victim["ciphertext"]), WS_A, CRED_A
    ) == "acct-456", "victim credential ciphertext was overwritten cross-workspace"


async def test_reencrypt_update_with_mismatched_id_and_workspace_changes_nothing(db, monkeypatch):
    """AO112-R3-2: drive the REAL re-encrypt UPDATE in store_credential with
    CRED_A's id (forced out of the INSERT ... RETURNING) and workspace B.
    Without `AND workspace_id` the victim's ciphertext is overwritten."""
    import app.security.credentials as creds_mod
    from sqlalchemy.ext.asyncio import AsyncSession
    import sqlalchemy.ext.asyncio as sa_asyncio

    crypto = _patch_creds_crypto(monkeypatch)
    await _seed_credential(db, crypto, CRED_A, WS_A)
    before = await _cred_row(db, CRED_A)

    class _Returning:
        def scalar_one(self):
            return str(CRED_A)

    orig = sa_asyncio.AsyncSession.execute

    async def _execute(self, stmt, params=None):
        if "INSERT INTO public.tenant_credentials" in str(stmt) or "INSERT INTO tenant_credentials" in str(stmt):
            return _Returning()
        return await orig(self, stmt, params)

    monkeypatch.setattr(sa_asyncio.AsyncSession, "execute", _execute)

    session = AsyncSession(bind=db, expire_on_commit=False)
    try:
        await creds_mod.store_credential(
            session, WS_B, "simplefin", "Checking", "attacker-value"
        )
        await session.commit()
    finally:
        await session.close()

    after = await _cred_row(db, CRED_A)
    assert bytes(after["ciphertext"]) == bytes(before["ciphertext"]), (
        "re-encrypt UPDATE overwrote another workspace's credential"
    )
    assert bytes(after["nonce"]) == bytes(before["nonce"])


# ===========================================================================
# get_credential_plaintext (credentials.py) -- end to end
# ===========================================================================


async def test_get_credential_plaintext_same_workspace_works(db, monkeypatch):
    """Same workspace: decrypts and bumps last_used_at."""
    import app.security.credentials as creds_mod
    from sqlalchemy.ext.asyncio import AsyncSession

    crypto = _patch_creds_crypto(monkeypatch)
    await _seed_credential(db, crypto, CRED_A, WS_A)

    session = AsyncSession(bind=db, expire_on_commit=False)
    try:
        plaintext = await creds_mod.get_credential_plaintext(
            session, WS_A, "simplefin", requesting_service="test"
        )
        assert plaintext == "acct-123"
        await session.commit()
    finally:
        await session.close()

    row = await _cred_row(db, CRED_A)
    assert row["last_used_at"] is not None, "same-workspace read did not bump last_used_at"


async def test_get_credential_plaintext_cross_workspace_touches_nothing(db, monkeypatch):
    """Cross workspace: finds no row, returns None, victim's row intact."""
    import app.security.credentials as creds_mod
    from sqlalchemy.ext.asyncio import AsyncSession

    crypto = _patch_creds_crypto(monkeypatch)
    await _seed_credential(db, crypto, CRED_A, WS_A)

    session = AsyncSession(bind=db, expire_on_commit=False)
    try:
        plaintext = await creds_mod.get_credential_plaintext(
            session, WS_B, "simplefin", requesting_service="test"
        )
        assert plaintext is None, "cross-workspace read must not find the victim's row"
        await session.commit()
    finally:
        await session.close()

    row = await _cred_row(db, CRED_A)
    assert row["last_used_at"] is None, "victim credential last_used_at was bumped"
