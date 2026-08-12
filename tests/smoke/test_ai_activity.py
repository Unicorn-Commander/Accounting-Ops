"""Unit-ish coverage for the AI Activity reversibility classifier and
label/target helpers.  These don't need a database — they exercise the
pure-Python helpers in ``app.api.ai_activity``.

The endpoint surface itself is covered by integration tests (which need
a workspace + audit rows seeded). These smoke tests just guarantee the
in-process classification logic stays sane.
"""
from __future__ import annotations

import datetime as dt

from app.api.ai_activity import (
    _ACTION_LABELS,
    _REVERSIBILITY,
    _artifact_link,
    _auto_applied,
    _target_from,
    is_reversible,
    label_for,
    reversibility_for,
)
from tests.route_introspection import paths_containing


def test_reversibility_table_known_tools():
    """Every classification key is one of our four reverse_kinds (or None)."""
    valid_kinds = {
        None,
        "memo_status_reject",
        "journal_post_contra",
        "categorization_pending",
    }
    for tool, (kind, window) in _REVERSIBILITY.items():
        assert kind in valid_kinds, f"{tool!r} has invalid reverse_kind {kind!r}"
        assert isinstance(window, int)
        assert window >= 0


def test_reversibility_lookup_defaults_to_challenge_only():
    """Unknown tool -> (None, 0) — challenge-only."""
    kind, window = reversibility_for("totally_made_up_tool")
    assert kind is None
    assert window == 0


def test_draft_memo_is_always_reversible():
    """draft_memo carries an effectively-unbounded reversal window."""
    long_ago = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=180)
    assert is_reversible("draft_memo", long_ago) is True


def test_categorize_is_reversible_within_24h_only():
    """Categorize is reversible inside the 24-hour grace window."""
    fresh = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=1)
    stale = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=25)
    assert is_reversible("categorize", fresh) is True
    assert is_reversible("categorize", stale) is False


def test_approval_decision_is_challenge_only():
    """approval_decision is never one-click reversible."""
    fresh = dt.datetime.now(tz=dt.timezone.utc)
    assert is_reversible("approval_decision", fresh) is False


def test_label_for_friendly_names_known_tools():
    assert label_for("draft_memo") == "Drafted memo"
    assert label_for("approval_decision") == "Decided approval"
    # Unknown — falls back to a humanized version of the raw name.
    assert label_for("some_new_tool_xyz") == "Some new tool xyz"


def test_action_labels_align_with_reversibility_table():
    """Every tool we know how to reverse should also have a friendly label
    so the timeline UI doesn't show the raw MCP name."""
    for tool in _REVERSIBILITY:
        assert tool in _ACTION_LABELS, (
            f"Tool {tool!r} is in the reversibility table but has no entry "
            f"in _ACTION_LABELS — the UI will show the raw tool name."
        )


def test_target_from_extracts_known_ids():
    req = {"tax_year": 2024}
    resp = {"memo_id": "abc-123", "form_reference": "1120-line-26"}
    t = _target_from("draft_memo", req, resp, "ent-xyz")
    assert t["entity_id"] == "ent-xyz"
    assert t["tax_year"] == 2024
    assert t["memo_id"] == "abc-123"
    assert t["form_reference"] == "1120-line-26"


def test_artifact_link_for_memo():
    target = {"memo_id": "deadbeef-1111-2222-3333-444455556666", "tax_year": 2024}
    link = _artifact_link("draft_memo", target)
    assert link is not None
    assert link["kind"] == "tax_review_queue_item"
    assert "deadbeef" in link["label"]


def test_artifact_link_for_unknown_tool():
    link = _artifact_link("some_new_tool", {"entity_id": "ent"})
    assert link is None


def test_auto_applied_for_approval_decision():
    """approval_decision is always human-decided, never auto-applied."""
    assert _auto_applied("approval_decision", {"applied": True}) is False


def test_auto_applied_for_explicit_unapplied_response():
    """Tools that surface ``applied=False`` (i.e. held for human approval)
    are not auto-applied."""
    assert _auto_applied("categorize", {"applied": False}) is False
    assert _auto_applied("categorize", {"applied": True}) is True
    # Missing response_payload defaults to auto-applied (the conservative
    # answer is: if there's no signal saying a human gated this, treat it
    # as auto so the UI surfaces it for review).
    assert _auto_applied("categorize", None) is True


def test_ai_activity_router_registers_all_four_endpoints():
    """The four routes the timeline UI uses must all be wired."""
    from app.api.ai_activity import router

    paths = {r.path for r in router.routes if hasattr(r, "path")}
    expected = {
        "/api/workspaces/{workspace_id}/ai-activity",
        "/api/workspaces/{workspace_id}/ai-activity/stream",
        "/api/workspaces/{workspace_id}/ai-activity/{audit_id}/undo",
        "/api/workspaces/{workspace_id}/ai-activity/{audit_id}/challenge",
    }
    assert expected <= paths


def test_main_app_wires_ai_activity_router():
    """Sanity: the FastAPI app exposes ai-activity routes."""
    from app.main import app

    paths = paths_containing(app, "ai-activity")
    assert len(paths) >= 4, f"expected >=4 ai-activity routes, got {paths!r}"
