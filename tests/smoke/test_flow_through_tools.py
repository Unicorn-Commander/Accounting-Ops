"""Wiring + migration guards for the 1120-S / 1065 derivation tools.

The three registration points (app/tools/__init__.py, mcp_server.py,
app/api/discovery.py) have historically carried uncommitted local drift on
deploy hosts, so a tool can be importable but invisible to callers. These tests
assert all three agree.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

NEW_TOOLS = ("get_form_1120s_data", "get_form_1065_data")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_is_exported_from_app_tools(name: str) -> None:
    import app.tools as tools

    assert hasattr(tools, name), f"{name} not importable from app.tools"
    assert name in tools.__all__, f"{name} missing from app.tools.__all__"


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_is_registered_on_the_mcp_server(name: str) -> None:
    source = (REPO_ROOT / "mcp_server.py").read_text()
    assert f"server.tool()({name})" in source, f"{name} not registered in mcp_server"


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_has_discovery_metadata(name: str) -> None:
    from app.api.discovery import _TOOL_METADATA

    assert name in _TOOL_METADATA, f"{name} missing from discovery metadata"
    meta = _TOOL_METADATA[name]
    assert meta["min_role"] == "viewer", "role floor must match get_form_1120_data"
    assert meta["read_only"] is True


def test_new_tools_match_the_1120_role_floor() -> None:
    """All three business returns are read-only viewer-level tax reads."""
    from app.api.discovery import _TOOL_METADATA

    baseline = _TOOL_METADATA["get_form_1120_data"]
    for name in NEW_TOOLS:
        meta = _TOOL_METADATA[name]
        assert meta["min_role"] == baseline["min_role"]
        assert meta["read_only"] == baseline["read_only"]


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_signature_matches_the_1120_contract(name: str) -> None:
    import app.tools as tools

    sig = inspect.signature(getattr(tools, name))
    assert list(sig.parameters) == ["workspace_id", "entity_id", "tax_year"]


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_docstring_carries_the_draft_warning(name: str) -> None:
    import app.tools as tools

    doc = inspect.getdoc(getattr(tools, name)) or ""
    assert "DRAFT" in doc
    assert "CPA review" in doc


def test_shared_client_forwards_caller_identity_not_just_the_service_token() -> None:
    """Audit 2026-05-31 Bug 10: the sidecar must see the CALLER's identity."""
    source = (REPO_ROOT / "app" / "tools" / "_cpa_export_client.py").read_text()
    assert "X-Internal-Assertion" in source
    assert "sign_assertion" in source
    assert 'authorize_workspace' in source
    assert 'min_role="viewer"' in source


def test_shared_client_stamps_a_draft_notice() -> None:
    source = (REPO_ROOT / "app" / "tools" / "_cpa_export_client.py").read_text()
    assert '_draft_notice' in source


# ---------------------------------------------------------------------------
# Migration 0027
# ---------------------------------------------------------------------------


def _migration_source() -> str:
    path = (
        REPO_ROOT / "migrations" / "tenant" / "versions" / "0027_entity_owner.py"
    )
    return path.read_text()


def test_tenant_tree_has_exactly_one_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations" / "tenant"))
    heads = list(ScriptDirectory.from_config(cfg).get_heads())
    assert heads == ["0027_entity_owner"], f"expected a single head, got {heads}"


def test_0027_chains_from_the_force_rls_migration() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations" / "tenant"))
    rev = ScriptDirectory.from_config(cfg).get_revision("0027_entity_owner")
    assert rev.down_revision == "0026_force_rls_shared"


def test_0027_applies_force_rls_itself() -> None:
    """0026 discovers scoped tables DYNAMICALLY and has already run, so it will
    not retroactively cover a table created afterwards. 0027 must apply the
    identical policy shape itself or entity_owner — which holds owner names and
    ownership percentages — would be the one workspace-scoped table with no
    cross-tenant protection."""
    source = _migration_source()
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "workspace_id = public.current_workspace_id()" in source


def test_0027_creates_all_four_per_command_policies() -> None:
    source = _migration_source()
    for suffix, command in (
        ("sel", "FOR SELECT"),
        ("ins", "FOR INSERT"),
        ("upd", "FOR UPDATE"),
        ("del", "FOR DELETE"),
    ):
        assert f"{{table}}_ws_{suffix}" in source, f"missing {suffix} policy"
        assert command in source


def test_0027_table_is_workspace_scoped() -> None:
    """Without a workspace_id column the RLS predicate cannot apply at all."""
    source = _migration_source()
    assert '"workspace_id", sa.UUID(), nullable=False' in source


def test_0027_revokes_truncate() -> None:
    source = _migration_source()
    assert "REVOKE TRUNCATE" in source


def test_0027_constrains_allocation_basis_to_known_values() -> None:
    """A typo'd basis must not silently unlock a pro-rata partnership split."""
    source = _migration_source()
    assert "ck_entity_owner_allocation_basis" in source
    for value in (
        "per_share_per_day",
        "partnership_agreement_pro_rata",
        "partnership_agreement_special",
    ):
        assert value in source


def test_entity_owner_model_matches_the_migration_columns() -> None:
    from app.models.ledger import EntityOwner

    source = _migration_source()
    for column in EntityOwner.__table__.columns:
        assert f'"{column.name}"' in source, (
            f"model column {column.name} has no counterpart in migration 0027"
        )


def test_entity_owner_model_is_entity_scoped() -> None:
    from app.models.ledger import EntityOwner

    cols = {c.name for c in EntityOwner.__table__.columns}
    assert {"workspace_id", "entity_id"} <= cols


def test_allocation_basis_enum_matches_the_check_constraint() -> None:
    from app.models.ledger import AllocationBasis

    assert {b.value for b in AllocationBasis} == {
        "per_share_per_day",
        "partnership_agreement_pro_rata",
        "partnership_agreement_special",
    }


def test_full_tax_id_is_not_stored_on_the_ownership_table() -> None:
    """A K-1 needs the owner's full SSN/EIN, but that belongs in the encrypted
    document path — not in a table a reporting sidecar reads."""
    from app.models.ledger import EntityOwner

    cols = {c.name for c in EntityOwner.__table__.columns}
    assert "tax_id_last4" in cols
    assert not any(
        c in cols for c in ("tax_id", "ssn", "ein", "tax_id_encrypted")
    )
