"""Unit coverage for app.security.pat — secret format + hashing + helpers.

No DB / no live stack. These exercise the pure-function primitives that the
storage layer + auth chain depend on.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.security.pat import (
    DEFAULT_TTL_DAYS,
    MAX_TTL_DAYS,
    PAT_PREFIX,
    PREFIX_DISPLAY_LEN,
    constant_time_eq,
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
    def test_none_returns_owner_set(self):
        owner = {"accounting:read", "accounting:write"}
        out = validate_scopes(None, owner)
        assert set(out) == owner

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

    def test_empty_list_returns_empty(self):
        """An explicit empty list is a degenerate-but-legal request — the
        token will be useless but it's still consistent with 'no scopes
        granted'."""
        owner = {"accounting:read"}
        out = validate_scopes([], owner)
        assert out == ()


# ---------------------------------------------------------------------------
# resolve_ttl — TTL → expires_at
# ---------------------------------------------------------------------------


class TestResolveTTL:
    def test_none_means_never(self):
        assert resolve_ttl(None) is None

    def test_zero_means_never(self):
        assert resolve_ttl(0) is None

    def test_default_is_90_days(self):
        assert DEFAULT_TTL_DAYS == 90

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
