"""Pure unit tests for app.api.members helpers — no DB / no network."""
from __future__ import annotations

import secrets

import pytest
from fastapi import HTTPException

from app.api.members import (
    DEFAULT_INVITE_TTL_DAYS,
    ELEVATED_ROLES,
    INVITEABLE_ROLES,
    MAX_INVITE_TTL_DAYS,
    TOKEN_BYTES,
    _hash_token,
    _normalize_email,
)


def test_token_bytes_meet_spec():
    # Brief mandated 32+ bytes of entropy.
    assert TOKEN_BYTES >= 32


def test_hash_token_is_deterministic_and_sha256():
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    h = _hash_token(raw)
    # SHA-256 hex = 64 chars
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
    # Stable
    assert _hash_token(raw) == h
    # Different input -> different hash
    assert _hash_token(raw + "x") != h


def test_normalize_email_lowercases_and_validates():
    assert _normalize_email("FOO@Bar.com") == "foo@bar.com"
    assert _normalize_email("  user@example.org  ") == "user@example.org"


@pytest.mark.parametrize(
    "bad",
    ["", "not-an-email", "missing-domain@", "@missing-local", "spaces in@email.com"],
)
def test_normalize_email_rejects_invalid(bad: str):
    with pytest.raises(HTTPException):
        _normalize_email(bad)


def test_inviteable_roles_excludes_platform_admin():
    assert "platform_admin" not in INVITEABLE_ROLES


def test_elevated_roles_match_expected():
    assert ELEVATED_ROLES == frozenset({"admin", "owner", "client_owner"})


def test_default_ttl_within_max():
    assert 1 <= DEFAULT_INVITE_TTL_DAYS <= MAX_INVITE_TTL_DAYS
    # Brief mandates 7d default
    assert DEFAULT_INVITE_TTL_DAYS == 7
