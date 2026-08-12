"""Connector discovery must not run as a role RLS filters to nothing.

The eBay and Stripe sync workers look ACROSS workspaces to find which ones have
a credential. That query ran on app.db.SessionLocal — the runtime role — and
public.tenant_credentials carries RLS (enabled, not forced) with a
workspace_isolation policy keyed on app.current_workspace, a GUC a
cross-workspace query has no single value for. So the query matched zero rows
against a database that plainly held the credential, and both workers sat
"healthy" doing nothing: eBay's connector_state was empty and its last
successful sync never happened.

accounting_ops_admin owns that table and it is not FORCE RLS, so the owner is
exempt. These tests pin the two properties that keep the fix honest: discovery
uses the admin URL, and per-workspace work does NOT (it must stay isolated).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

SCRIPTS = ("scripts/ebay_sync.py", "scripts/stripe_sync.py")
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _source(name: str) -> str:
    return (ROOT / name).read_text()


@pytest.mark.parametrize("script", SCRIPTS)
def test_discovery_uses_an_admin_bound_session(script: str):
    src = _source(script)
    tree = ast.parse(src)
    discovery = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("_workspaces_with_")
    ]
    assert discovery, f"{script}: no _workspaces_with_* discovery function"
    body = ast.get_source_segment(src, discovery[0]) or ""
    assert "_admin_sessionmaker()" in body, (
        f"{script}: cross-workspace discovery must not use the runtime role — "
        "RLS on public.tenant_credentials filters it to zero rows"
    )
    assert "SessionLocal() as session" not in body, (
        f"{script}: discovery still opens a runtime-role session"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_admin_url_falls_back_and_never_silently_defaults(script: str):
    src = _source(script)
    assert 'os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")' in src, script
    # A missing URL must stop the worker, not hand back an unusable engine.
    assert "SystemExit(" in src, f"{script}: missing-URL case must fail loudly"


@pytest.mark.parametrize("script", SCRIPTS)
def test_per_workspace_work_still_uses_the_runtime_role(script: str):
    """The privilege escalation is scoped to discovery ONLY.

    Per-workspace reads and writes must keep going through workspace_session on
    the runtime role — that is what actually enforces tenant isolation. If this
    fails, the fix has widened far past its purpose.
    """
    src = _source(script)
    assert "workspace_session(SessionLocal" in src, (
        f"{script}: per-workspace work must stay on the runtime role"
    )
    assert src.count("_admin_sessionmaker()()") == 1, (
        f"{script}: the admin session is for discovery only, found "
        f"{src.count('_admin_sessionmaker()()')} uses"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_empty_discovery_warns_on_every_cycle(script: str):
    """Silence is what hid this for months."""
    src = _source(script)
    tree = ast.parse(src)
    loop_fn = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_sync_all_workspaces"
    ]
    assert loop_fn, f"{script}: no _sync_all_workspaces"
    body = ast.get_source_segment(src, loop_fn[0]) or ""
    assert "no_workspaces_with_credential" in body, (
        f"{script}: an empty discovery must warn from the per-cycle path, not "
        "only from the one-shot CLI path"
    )
    assert "_log.warning" in body, f"{script}: log it at WARNING, not INFO"
