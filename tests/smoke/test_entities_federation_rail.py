"""The workspace entity list rides the read-only federation rail.

Customer-Ops reads the vendor's legal identity (``legal_name``) from
``GET /api/workspaces/{workspace_id}/entities`` to build the quote letterhead
(docs/QUOTES-SEAM.md). Its caller is a trusted M2M actor with no
``user_account`` row, so the route must resolve the workspace through
``resolve_workspace_for_federation`` (viewer floor, HIPAA refused, members
unchanged) — never through the membership-only ``resolve_workspace``.

These are wiring tests: they pin the dependency on the route so a refactor
cannot silently move the letterhead read back behind the membership path.
"""
from __future__ import annotations

from fastapi.routing import APIRoute

from app.api.workspaces import router
from app.tenancy import resolve_workspace, resolve_workspace_for_federation

ENTITIES_PATH = "/api/workspaces/{workspace_id}/entities"


def _route(path: str) -> APIRoute:
    for r in router.routes:
        if isinstance(r, APIRoute) and r.path == path and "GET" in r.methods:
            return r
    raise AssertionError(f"route {path} not found on the workspaces router")


def _dependency_calls(route: APIRoute) -> list[object]:
    return [dep.call for dep in route.dependant.dependencies]


def test_entities_list_uses_the_federation_resolver():
    calls = _dependency_calls(_route(ENTITIES_PATH))
    assert resolve_workspace_for_federation in calls


def test_entities_list_does_not_use_the_membership_resolver_directly():
    calls = _dependency_calls(_route(ENTITIES_PATH))
    assert resolve_workspace not in calls


def test_single_workspace_detail_stays_on_the_membership_path():
    """Only the entity list opens to trusted M2M callers; the workspace detail
    route (roles, membership) keeps the membership resolver."""
    calls = _dependency_calls(_route("/api/workspaces/{workspace_id}"))
    assert resolve_workspace in calls
    assert resolve_workspace_for_federation not in calls
