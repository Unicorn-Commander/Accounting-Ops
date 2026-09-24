"""Pin the cpa-export outbound-bearer contract.

2026-08-11 production incident: legacy1 (.ai prod) had ONLY
``CPA_EXPORT_SERVICE_TOKEN`` set — the name the cpa-export SIDECAR reads —
while the app resolved its outbound bearer from ``ACCTOPS_SERVICE_TOKEN`` or
``CPA_EXPORT_TOKEN``. Every app -> cpa-export request therefore went out with
no Authorization header, the sidecar answered 401 "Missing bearer token", and
the whole 1120 / 1120-S / 1065 export path was dead in production while every
healthcheck stayed green.

Two guards:
 1. the resolver accepts all three names, in precedence order;
 2. no cpa-export call site may resolve the bearer inline again (the
    single-source-of-truth guard — an inline os.getenv chain is how the
    mismatch survived across three call sites in the first place).
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.tools._cpa_export_client import (
    _CPA_EXPORT_BEARER_ENV_VARS,
    resolve_cpa_export_bearer,
)

_ALL_NAMES = ("ACCTOPS_SERVICE_TOKEN", "CPA_EXPORT_TOKEN", "CPA_EXPORT_SERVICE_TOKEN")


@pytest.fixture(autouse=True)
def _clear_bearer_env(monkeypatch):
    for name in _ALL_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("name", _ALL_NAMES)
def test_each_accepted_name_resolves(monkeypatch, name):
    """Any one of the three names alone is enough to authenticate."""
    monkeypatch.setenv(name, f"token-from-{name}")
    assert resolve_cpa_export_bearer() == f"token-from-{name}"


def test_the_sidecars_own_variable_name_is_accepted(monkeypatch):
    """The exact production failure: ONLY the sidecar's name is set."""
    monkeypatch.setenv("CPA_EXPORT_SERVICE_TOKEN", "sidecar-name-only")
    assert resolve_cpa_export_bearer() == "sidecar-name-only"


def test_precedence_order_is_stable(monkeypatch):
    for name in _ALL_NAMES:
        monkeypatch.setenv(name, f"v-{name}")
    assert resolve_cpa_export_bearer() == "v-ACCTOPS_SERVICE_TOKEN"
    monkeypatch.delenv("ACCTOPS_SERVICE_TOKEN")
    assert resolve_cpa_export_bearer() == "v-CPA_EXPORT_TOKEN"
    monkeypatch.delenv("CPA_EXPORT_TOKEN")
    assert resolve_cpa_export_bearer() == "v-CPA_EXPORT_SERVICE_TOKEN"


def test_unset_returns_none_and_never_invents_a_credential():
    assert resolve_cpa_export_bearer() is None


def test_empty_values_are_skipped(monkeypatch):
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKEN", "")
    monkeypatch.setenv("CPA_EXPORT_TOKEN", "real")
    assert resolve_cpa_export_bearer() == "real"


def test_declared_names_match_the_tested_set():
    assert tuple(_CPA_EXPORT_BEARER_ENV_VARS) == _ALL_NAMES


# Files that name a CPA_EXPORT_* env var for something OTHER than an outbound
# cpa-export request, and are therefore not required to use the resolver.
# Both narrative paths use the value as the bearer for the /mcp endpoint (the
# narrative runner's own MCP session) — reusing the cpa-export name there is
# pre-existing and confusing, but it is NOT the sidecar handshake, so routing
# it through resolve_cpa_export_bearer() would be wrong, not merely redundant.
_INLINE_ALLOWLIST = frozenset({
    "app/tools/tax_narrative.py",
    "app/api/tax_narrative.py",
})


def test_no_cpa_export_call_site_resolves_the_bearer_inline():
    """Single source of truth: only _cpa_export_client.py may name these vars.

    A new call site that re-implements ``os.getenv("ACCTOPS_SERVICE_TOKEN") or
    ...`` reintroduces exactly the drift that took prod's export path down.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    resolver = repo_root / "app" / "tools" / "_cpa_export_client.py"
    offenders: list[str] = []

    for path in sorted((repo_root / "app").rglob("*.py")):
        if path == resolver:
            continue
        rel = path.relative_to(repo_root).as_posix()
        if rel == "app/config.py":
            continue
        if rel in _INLINE_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"os\.(getenv|environ\.get)\(\s*[\"']CPA_EXPORT_(SERVICE_)?TOKEN[\"']", text):
            offenders.append(rel)

    assert not offenders, (
        "these files resolve the cpa-export bearer inline instead of calling "
        f"resolve_cpa_export_bearer(): {offenders}"
    )


def test_allowlisted_files_still_exist_and_are_still_mcp_only():
    """Keep the allowlist honest: if a narrative file ever starts talking to the
    cpa-export sidecar, the exemption must be re-examined rather than inherited.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    for rel in _INLINE_ALLOWLIST:
        path = repo_root / rel
        assert path.exists(), f"allowlisted file {rel} no longer exists — prune the allowlist"
        text = path.read_text(encoding="utf-8")
        assert "cpa_export_url" not in text, (
            f"{rel} now calls the cpa-export sidecar; it must use "
            "resolve_cpa_export_bearer() and leave the allowlist"
        )
