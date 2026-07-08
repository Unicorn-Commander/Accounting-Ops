"""Regression unit tests for audit finding C1 — bank-data cross-workspace leak.

Before the fix, cash_position / transactions / account_mappings resolved the
SimpleFIN access URL with a per-uc_uid env-var fallback that *also* fell back to
a hard-coded SIMPLEFIN_ACCESS_URL_OWNER. A workspace with no stored credential
would therefore silently inherit whatever bank connection sat in the process
environment — one customer's bank data leaking into another's workspace.

These tests prove the fallback is GONE: with the env vars set AND the encrypted
store returning nothing, each module's _simplefin_access_url MUST return None
(the "not configured" signal) — it must never consult os.environ.

Pure unit tests: get_credential_plaintext is monkeypatched so no DB is needed.
"""
from __future__ import annotations

import importlib

import pytest

# The three modules that used to carry the env fallback.
_MODULES = [
    "app.api.cash_position",
    "app.api.transactions",
    "app.api.account_mappings",
]


class _FakeSession:
    """Minimal async-context-manager stand-in for SessionLocal()."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin(self):
        return self


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name", _MODULES)
async def test_no_env_fallback_when_store_empty(module_name, monkeypatch):
    mod = importlib.import_module(module_name)

    # Encrypted store has nothing for this workspace.
    async def _no_credential(*args, **kwargs):
        return None

    monkeypatch.setattr(mod, "get_credential_plaintext", _no_credential)
    monkeypatch.setattr(mod, "SessionLocal", lambda: _FakeSession())

    # Plant exactly the env vars the old fallback would have read.
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL_OWNER", "https://leak:owner@bridge/accounts")
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL_TEST-SERVICE", "https://leak:ts@bridge/accounts")
    monkeypatch.setenv("SIMPLEFIN_ACCESS_URL_VICTIM", "https://leak:victim@bridge/accounts")

    class _Ws:
        workspace_id = __import__("uuid").uuid4()
        user_uc_uid = "victim"
        agent_id = None

    result = await mod._simplefin_access_url(_Ws())
    assert result is None, (
        f"{module_name}._simplefin_access_url leaked an env-var credential "
        f"({result!r}); the C1 fallback must be removed."
    )


def test_modules_do_not_reference_simplefin_env_vars():
    """Belt-and-suspenders: the env-var names must not appear in module source.

    Catches a re-introduced os.getenv(...) fallback even if a future refactor
    changes the function shape so the behavioural test above stops covering it.
    """
    import inspect

    for module_name in _MODULES:
        mod = importlib.import_module(module_name)
        src = inspect.getsource(mod)
        # The function body must not call os.getenv on a SIMPLEFIN_* key.
        assert "os.getenv(" not in src, (
            f"{module_name} still calls os.getenv — C1 fallback may be back"
        )
