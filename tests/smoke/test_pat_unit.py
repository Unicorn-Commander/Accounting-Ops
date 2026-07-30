"""Unit coverage for app.security.pat — secret format + hashing + helpers.

No DB / no live stack. These exercise the pure-function primitives that the
storage layer + auth chain depend on.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.security.pat import (
    DEFAULT_TTL_DAYS,
    MAX_TTL_DAYS,
    PAT_PREFIX,
    PREFIX_DISPLAY_LEN,
    MintRequestError,
    PATRow,
    constant_time_eq,
    count_active_pats,
    find_active_pat_by_token,
    generate_secret,
    hash_secret,
    looks_like_pat,
    resolve_ttl,
    secret_prefix,
    validate_scopes,
)


# ---------------------------------------------------------------------------
# generate_secret — format + entropy
# ---------------------------------------------------------------------------


class TestGenerateSecret:
    def test_starts_with_wire_prefix(self):
        s = generate_secret()
        assert s.startswith(PAT_PREFIX)

    def test_total_length_is_52(self):
        # PAT_PREFIX (9 chars) + 43 url-safe base64 chars from token_urlsafe(32)
        s = generate_secret()
        assert len(s) == len(PAT_PREFIX) + 43 == 52

    def test_charset_is_url_safe(self):
        """token_urlsafe is base64 with - and _ instead of + and /."""
        s = generate_secret()
        body = s[len(PAT_PREFIX) :]
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body), body

    def test_each_call_unique(self):
        """20 calls should produce 20 distinct secrets — collision = broken RNG."""
        seen = {generate_secret() for _ in range(20)}
        assert len(seen) == 20


# ---------------------------------------------------------------------------
# hash_secret — sha256 roundtrip
# ---------------------------------------------------------------------------


class TestHashSecret:
    def test_returns_32_bytes(self):
        h = hash_secret(generate_secret())
        assert isinstance(h, bytes)
        assert len(h) == 32  # sha256 digest size

    def test_matches_hashlib(self):
        s = "aops_pat_example_for_roundtrip_test"
        assert hash_secret(s) == hashlib.sha256(s.encode("utf-8")).digest()

    def test_different_inputs_different_hashes(self):
        a = generate_secret()
        b = generate_secret()
        assert hash_secret(a) != hash_secret(b)

    def test_same_input_same_hash(self):
        s = generate_secret()
        assert hash_secret(s) == hash_secret(s)


# ---------------------------------------------------------------------------
# secret_prefix — display + lookup key
# ---------------------------------------------------------------------------


class TestSecretPrefix:
    def test_returns_first_12_chars(self):
        s = "aops_pat_abcdefghijklmnop"
        assert secret_prefix(s) == "aops_pat_abc"
        assert len(secret_prefix(s)) == PREFIX_DISPLAY_LEN

    def test_always_starts_with_wire_prefix(self):
        s = generate_secret()
        assert secret_prefix(s).startswith(PAT_PREFIX)

    def test_prefix_does_not_leak_full_secret(self):
        s = generate_secret()
        p = secret_prefix(s)
        assert len(p) < len(s)  # the secret cannot be derived from the prefix


# ---------------------------------------------------------------------------
# looks_like_pat — cheap pre-check
# ---------------------------------------------------------------------------


class TestLooksLikePat:
    def test_accepts_fresh_secret(self):
        assert looks_like_pat(generate_secret()) is True

    def test_rejects_jwt(self):
        # Three-part dotted JWT — must NOT look like a PAT.
        assert (
            looks_like_pat(
                "eyJhbGciOiJSUzI1NiIsInR5cCIgOiAiSldUIn0.eyJzdWIiOiJhbGljZSJ9.signature"
            )
            is False
        )

    def test_rejects_service_token(self):
        assert looks_like_pat("h2-ws-match-" + "0" * 28) is False

    def test_rejects_empty(self):
        assert looks_like_pat("") is False

    def test_rejects_non_string(self):
        # The auth chain hands us strings, but be defensive.
        assert looks_like_pat(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# constant_time_eq
# ---------------------------------------------------------------------------


class TestConstantTimeEq:
    def test_equal_bytes(self):
        a = hash_secret("hello")
        assert constant_time_eq(a, hash_secret("hello")) is True

    def test_unequal_bytes(self):
        a = hash_secret("hello")
        b = hash_secret("world")
        assert constant_time_eq(a, b) is False

    def test_different_lengths(self):
        # secrets.compare_digest is constant-time only when lengths match;
        # for our use case both sides are sha256 → 32 bytes always.
        assert constant_time_eq(b"\x00" * 32, b"\x00" * 32) is True
        assert constant_time_eq(b"\x00" * 32, b"\x00" * 31) is False


# ---------------------------------------------------------------------------
# validate_scopes — scope subset enforcement
# ---------------------------------------------------------------------------


class TestValidateScopes:
    def test_none_is_refused(self):
        """Omitting scopes must NOT inherit everything the owner can do.

        Regression for the mint-side privilege default: a request with no
        scope list is a malformed request (MintRequestError -> 400), not an
        implicit grant of the owner's full role set.
        """
        owner = {"accounting:read", "accounting:write", "accounting:admin"}
        with pytest.raises(MintRequestError):
            validate_scopes(None, owner)

    def test_subset_allowed(self):
        owner = {"accounting:read", "accounting:write", "accounting:admin"}
        out = validate_scopes(["accounting:read"], owner)
        assert out == ("accounting:read",)

    def test_full_set_allowed(self):
        owner = {"accounting:read", "accounting:write"}
        out = validate_scopes(["accounting:read", "accounting:write"], owner)
        assert set(out) == owner

    def test_superset_rejected(self):
        owner = {"accounting:read"}
        with pytest.raises(ValueError, match="Cannot grant"):
            validate_scopes(["accounting:read", "accounting:write"], owner)

    def test_unknown_scope_rejected(self):
        owner = {"accounting:read"}
        with pytest.raises(ValueError, match="Cannot grant"):
            validate_scopes(["sudo:everything"], owner)

    def test_returns_sorted_unique(self):
        owner = {"accounting:read", "accounting:write"}
        out = validate_scopes(
            ["accounting:write", "accounting:read", "accounting:write"],
            owner,
        )
        assert out == ("accounting:read", "accounting:write")

    def test_empty_list_is_refused(self):
        """An empty list grants nothing useful and reads like a mistake —
        refuse it rather than mint a dud token."""
        owner = {"accounting:read"}
        with pytest.raises(MintRequestError):
            validate_scopes([], owner)

    def test_whitespace_only_is_refused(self):
        """Blank/whitespace entries are stripped; if nothing survives, refuse
        instead of falling through to a permissive default."""
        owner = {"accounting:read"}
        with pytest.raises(MintRequestError):
            validate_scopes(["", "   ", "\t"], owner)

    def test_whitespace_is_stripped_from_real_scopes(self):
        owner = {"accounting:read", "accounting:write"}
        out = validate_scopes(["  accounting:read  ", ""], owner)
        assert out == ("accounting:read",)

    def test_explicit_full_owner_set_still_allowed(self):
        """Max privilege remains available — it just has to be DELIBERATE.
        The defect was the default, not the capability."""
        owner = {"accounting:read", "accounting:write", "accounting:admin"}
        out = validate_scopes(sorted(owner), owner)
        assert set(out) == owner

    def test_mint_request_error_is_a_value_error(self):
        """Existing `except ValueError` handlers must keep catching it."""
        assert issubclass(MintRequestError, ValueError)


# ---------------------------------------------------------------------------
# resolve_ttl — TTL → expires_at
# ---------------------------------------------------------------------------


class TestResolveTTL:
    def test_none_is_refused(self):
        """A null/omitted TTL must not mint an immortal credential."""
        with pytest.raises(MintRequestError):
            resolve_ttl(None)

    def test_zero_is_refused(self):
        """0 was the 'never expires' sentinel — mint-side it is now a 400."""
        with pytest.raises(MintRequestError):
            resolve_ttl(0)

    def test_never_expiring_cannot_be_minted(self):
        """No TTL input may produce a NULL expires_at. Belt-and-braces over
        the two cases above: whatever resolve_ttl returns, it is a datetime."""
        for ttl in (1, 30, DEFAULT_TTL_DAYS, MAX_TTL_DAYS):
            out = resolve_ttl(ttl)
            assert isinstance(out, datetime), ttl
            assert out is not None, ttl

    def test_default_is_90_days(self):
        assert DEFAULT_TTL_DAYS == 90

    def test_default_ttl_is_within_max(self):
        assert 0 < DEFAULT_TTL_DAYS <= MAX_TTL_DAYS == 365

    def test_positive_returns_future(self):
        before = datetime.now(timezone.utc)
        out = resolve_ttl(30)
        after = datetime.now(timezone.utc)
        assert out is not None
        # The expires_at should be ~30d ahead. Allow generous slack so the
        # test doesn't flake on slow CI.
        expected_lo = before + timedelta(days=30) - timedelta(seconds=5)
        expected_hi = after + timedelta(days=30) + timedelta(seconds=5)
        assert expected_lo <= out <= expected_hi

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            resolve_ttl(-1)

    def test_over_max_rejected(self):
        with pytest.raises(ValueError):
            resolve_ttl(MAX_TTL_DAYS + 1)

    def test_at_max_allowed(self):
        out = resolve_ttl(MAX_TTL_DAYS)
        assert out is not None


# ---------------------------------------------------------------------------
# end-to-end primitive roundtrip (no DB)
# ---------------------------------------------------------------------------


class TestRoundtrip:
    def test_mint_hash_compare_match(self):
        s = generate_secret()
        stored = hash_secret(s)
        presented = hash_secret(s)
        assert constant_time_eq(stored, presented)

    def test_mint_hash_compare_mismatch(self):
        a = generate_secret()
        b = generate_secret()
        stored = hash_secret(a)
        presented = hash_secret(b)
        assert not constant_time_eq(stored, presented)

    def test_prefix_uniquely_identifies_secret_in_practice(self):
        # In practice, 18 bits of entropy beyond the literal prefix means
        # 1000 fresh secrets are extremely unlikely to collide on the
        # full 12-char prefix. (Birthday bound ~512 for 50% collision.)
        prefixes = {secret_prefix(generate_secret()) for _ in range(1000)}
        # At least 990 of the 1000 should be unique — anything tighter risks
        # legitimate-but-rare flakes.
        assert len(prefixes) >= 990, len(prefixes)


# ---------------------------------------------------------------------------
# BACKWARD-COMPATIBILITY GUARD — legacy credentials must keep verifying
#
# The mint path no longer issues never-expiring, full-scope PATs. VERIFICATION
# is deliberately untouched: tokens already in production that carry a NULL
# expires_at and the owner's whole scope set must keep authenticating
# byte-for-byte. If a future change adds a NOT NULL constraint, backfills
# expires_at, or flips NULL to "expired", these tests go red.
# ---------------------------------------------------------------------------


def _legacy_row(raw_secret: str, *, revoked: bool = False):
    """A DB row exactly as a pre-fix mint would have written it: NULL
    expires_at (never expires) + the full owner scope set."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        name="legacy-never-expires",
        secret_hash=hash_secret(raw_secret),
        secret_prefix=secret_prefix(raw_secret),
        scopes=["accounting:read", "accounting:write", "accounting:admin"],
        workspace_scope=None,  # NULL = all workspaces (legacy shape)
        expires_at=None,  # NULL = never expires (legacy shape)
        created_at=datetime.now(timezone.utc) - timedelta(days=400),
        last_used_at=None,
        revoked_at=datetime.now(timezone.utc) if revoked else None,
        owner_uc_uid="legacy-owner-uc-uid",
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Minimal stand-in for AsyncSession.execute() used by the lookup path."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt, _params=None):
        return _FakeResult(self._rows)


class TestLegacyNullExpiryStillVerifies:
    def test_null_expiry_row_is_not_expired(self):
        row = _legacy_row(generate_secret())
        pat = PATRow(
            id=row.id,
            owner_user_id=row.owner_user_id,
            name=row.name,
            secret_prefix=row.secret_prefix,
            scopes=tuple(row.scopes),
            workspace_scope=None,
            expires_at=None,
            created_at=row.created_at,
            last_used_at=None,
            revoked_at=None,
        )
        assert pat.is_expired is False
        assert pat.is_active is True

    async def test_null_expiry_full_scope_pat_still_authenticates(self):
        """The exact credential the old mint path produced — no expiry, every
        scope, no workspace restriction — must still resolve on verification."""
        raw = generate_secret()
        session = _FakeSession([_legacy_row(raw)])

        found = await find_active_pat_by_token(session, raw)  # type: ignore[arg-type]

        assert found is not None, "a live NULL-expiry PAT must still verify"
        pat, owner_user_id, owner_uc_uid = found
        assert pat.expires_at is None
        assert pat.workspace_scope is None
        assert set(pat.scopes) == {
            "accounting:read",
            "accounting:write",
            "accounting:admin",
        }
        assert owner_uc_uid == "legacy-owner-uc-uid"
        assert owner_user_id == pat.owner_user_id

    async def test_revoked_legacy_pat_still_rejected(self):
        """Revocation still beats the NULL-expiry allowance."""
        raw = generate_secret()
        session = _FakeSession([_legacy_row(raw, revoked=True)])
        assert await find_active_pat_by_token(session, raw) is None  # type: ignore[arg-type]

    async def test_finite_expiry_in_the_past_still_rejected(self):
        raw = generate_secret()
        row = _legacy_row(raw)
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session = _FakeSession([row])
        assert await find_active_pat_by_token(session, raw) is None  # type: ignore[arg-type]

    def test_count_query_still_treats_null_expiry_as_live(self):
        """The SQL predicate that decides 'is this token still active' must
        keep the `expires_at IS NULL` arm — dropping it would retroactively
        kill every legacy token."""
        src = inspect.getsource(count_active_pats)
        assert "expires_at IS NULL OR expires_at > now()" in src
