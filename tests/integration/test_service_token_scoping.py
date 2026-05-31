"""Audit 2026-05-31 Bug 7 — service-token scope enforcement at load time.

The old behavior (pre-Bug-7) treated a service token with NO scope fields as
backward-compatible: it received the impersonated user's FULL workspace role,
which made one leaked worker token equivalent to owner/admin across every
workspace that uc_uid could reach. The audit called this a "full-impersonation
footgun."

The fix in ``app/auth.py:_load_service_tokens`` requires at least one of
``workspace_ids`` / ``entity_ids`` / ``role_floor`` in production. The dev
escape hatch ``ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN=true`` re-permits the
legacy shape (with a loud warning).

These tests exercise the loader directly (no DB / no HTTP round-trip needed —
the logic is in the loader) so they run anywhere ``app.auth`` is importable.

Cases:
  1. valid SCOPED token → admitted to the cache
  2. unscoped token in PROD (flag unset/false) → DROPPED from the cache + WARN
  3. unscoped token in DEV (flag true) → admitted + WARN
  4. mix of scoped + unscoped in prod → only the scoped one survives
  5. scope=empty list is a valid declaration (deny-all)
"""
from __future__ import annotations

import json
import logging

import pytest

import app.auth as auth_mod


_VALID_TOKEN = "scoped-token-Aaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # 41 chars
_UNSCOPED_TOKEN = "unscoped-token-Bbbbbbbbbbbbbbbbbbbbbbbb"   # 40 chars
_SECOND_VALID_TOKEN = "scoped-token-Cccccccccccccccccccccccc"  # 38 chars


@pytest.fixture(autouse=True)
def _reset_token_cache_and_env(monkeypatch):
    """Clear the loader cache + per-test env state. Re-cleared on teardown."""
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", None)
    monkeypatch.delenv("ACCTOPS_SERVICE_TOKENS", raising=False)
    monkeypatch.delenv("ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN", raising=False)
    yield
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", None)


def _set_env_tokens(monkeypatch, entries: dict) -> None:
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKENS", json.dumps(entries))


def test_scoped_token_loads_in_production(monkeypatch):
    """A token with role_floor (or any scope field) loads cleanly when the
    dev escape hatch is OFF — this is the production happy path."""
    _set_env_tokens(
        monkeypatch,
        {
            _VALID_TOKEN: {
                "uc_uid": "alice",
                "agent_id": "test-worker",
                "role_floor": "accountant",
            }
        },
    )
    tokens = auth_mod._load_service_tokens()
    assert _VALID_TOKEN in tokens
    assert tokens[_VALID_TOKEN]["uc_uid"] == "alice"
    assert tokens[_VALID_TOKEN]["role_floor"] == "accountant"


def test_unscoped_token_rejected_in_production(monkeypatch, caplog):
    """The footgun case from the audit: a token with NO scope fields is
    rejected at load time when ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN is unset.

    The cache is empty (not a key with empty scopes) — the loader DROPS the
    entry and emits a WARN-level log naming the agent_id."""
    _set_env_tokens(
        monkeypatch,
        {
            _UNSCOPED_TOKEN: {
                "uc_uid": "alice",
                "agent_id": "legacy-worker",
            }
        },
    )
    with caplog.at_level(logging.WARNING, logger="acctops.auth"):
        tokens = auth_mod._load_service_tokens()
    assert tokens == {}, (
        "unscoped token MUST be dropped in production "
        "(set ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN=true for dev)"
    )
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "REJECTING unscoped service token" in msgs
    assert "legacy-worker" in msgs


def test_unscoped_token_allowed_in_dev(monkeypatch, caplog):
    """With ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN=true, the legacy unscoped
    shape loads (with a loud warning so dev operators see the smell)."""
    monkeypatch.setenv("ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN", "true")
    _set_env_tokens(
        monkeypatch,
        {
            _UNSCOPED_TOKEN: {
                "uc_uid": "alice",
                "agent_id": "legacy-worker",
            }
        },
    )
    with caplog.at_level(logging.WARNING, logger="acctops.auth"):
        tokens = auth_mod._load_service_tokens()
    assert _UNSCOPED_TOKEN in tokens, (
        "ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN=true must re-permit the legacy shape"
    )
    ident = tokens[_UNSCOPED_TOKEN]
    assert ident["workspace_ids"] is None
    assert ident["entity_ids"] is None
    assert ident["role_floor"] is None
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "ALLOWING unscoped service token" in msgs


def test_mixed_scoped_and_unscoped_only_keeps_scoped_in_prod(monkeypatch, caplog):
    """In production a mixed map loads only the entries that declare scope."""
    _set_env_tokens(
        monkeypatch,
        {
            _VALID_TOKEN: {
                "uc_uid": "alice",
                "agent_id": "scoped-worker",
                "role_floor": "viewer",
            },
            _UNSCOPED_TOKEN: {
                "uc_uid": "alice",
                "agent_id": "legacy-worker",
            },
        },
    )
    with caplog.at_level(logging.WARNING, logger="acctops.auth"):
        tokens = auth_mod._load_service_tokens()
    assert _VALID_TOKEN in tokens
    assert _UNSCOPED_TOKEN not in tokens, (
        "legacy unscoped token must be dropped while the scoped one survives"
    )


def test_explicit_empty_workspace_list_is_a_valid_declaration(monkeypatch):
    """An explicit empty list for workspace_ids is a deliberate deny-all
    declaration. It counts as "at least one scope field set" so the token
    loads (and resolve_workspace returns 403 for any workspace_id)."""
    _set_env_tokens(
        monkeypatch,
        {
            _VALID_TOKEN: {
                "uc_uid": "alice",
                "agent_id": "deny-all-worker",
                "workspace_ids": [],
            }
        },
    )
    tokens = auth_mod._load_service_tokens()
    assert _VALID_TOKEN in tokens
    assert tokens[_VALID_TOKEN]["workspace_ids"] == frozenset()


def test_short_token_string_is_rejected(monkeypatch):
    """Length check (≥32 chars) is preserved by the new loader (regression
    against accidental weakening when adding scope enforcement)."""
    _set_env_tokens(
        monkeypatch,
        {
            "short-token": {  # only 11 chars — too short
                "uc_uid": "alice",
                "agent_id": "worker",
                "role_floor": "viewer",
            }
        },
    )
    assert auth_mod._load_service_tokens() == {}


def test_unscoped_dev_flag_recognizes_common_truthy_values(monkeypatch):
    """The dev escape-hatch reads ``1``/``true``/``yes``/``on`` as truthy
    (case-insensitive). Anything else is OFF."""
    for v in ("1", "true", "True", "YES", "On"):
        monkeypatch.setenv("ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN", v)
        assert auth_mod._allow_unscoped_service_token() is True, v
    for v in ("0", "false", "no", "", "maybe"):
        monkeypatch.setenv("ACCTOPS_ALLOW_UNSCOPED_SERVICE_TOKEN", v)
        assert auth_mod._allow_unscoped_service_token() is False, v
