"""Authorization + confirmation gating on the data-rights endpoints (no DB).

``resolve_workspace`` is overridden so these run without a database. That is
sound because every guard under test executes BEFORE the endpoint touches the
database — which is itself a property worth pinning: a 403 or a 400 must never
depend on the database being reachable, and a rejected destructive request must
not have opened a transaction.

If a future edit moves a DB call above ``require_role`` or above the
confirmation check, these tests fail with a connection error rather than
passing quietly. That is the intended tripwire.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.tenancy import WorkspaceContext, resolve_workspace
from tests.route_introspection import methods_for

WS_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SLUG = "acme-books"

BASE = f"/api/workspaces/{WS_ID}/data-rights"

# Every role below owner. 'accountant' matters most: an accountant with access to
# a client's workspace must NOT be able to erase that client's books.
NON_OWNER_ROLES = ["viewer", "client_user", "bookkeeper", "accountant", "cpa", "admin"]


def _ctx(role: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=WS_ID,
        workspace_slug=SLUG,
        role=role,
        user_uc_uid="user-1",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )


@pytest.fixture
def client_as():
    def _factory(role: str) -> TestClient:
        app.dependency_overrides[resolve_workspace] = lambda: _ctx(role)
        return TestClient(app)

    yield _factory
    app.dependency_overrides.pop(resolve_workspace, None)


def _valid_erasure_body(**overrides):
    body = {"confirm": SLUG, "acknowledge_irreversible": True}
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Erasure is owner-only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", NON_OWNER_ROLES)
def test_erasure_is_refused_for_every_non_owner_role(client_as, role):
    resp = client_as(role).post(f"{BASE}/erasure", json=_valid_erasure_body())
    assert resp.status_code == 403, f"role {role!r} was not refused"


@pytest.mark.parametrize("role", NON_OWNER_ROLES)
def test_export_is_refused_for_every_non_owner_role(client_as, role):
    assert client_as(role).post(f"{BASE}/exports").status_code == 403


@pytest.mark.parametrize("role", NON_OWNER_ROLES)
def test_setting_retention_is_refused_for_every_non_owner_role(client_as, role):
    resp = client_as(role).put(f"{BASE}/retention", json={"ledger_retention_days": 30})
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["viewer", "client_user", "bookkeeper"])
def test_reading_retention_is_refused_below_admin(client_as, role):
    assert client_as(role).get(f"{BASE}/retention").status_code == 403


# --------------------------------------------------------------------------- #
# Erasure refuses without explicit confirmation, even for the owner
# --------------------------------------------------------------------------- #
def test_erasure_refuses_without_the_irreversible_acknowledgement(client_as):
    resp = client_as("owner").post(
        f"{BASE}/erasure", json={"confirm": SLUG, "acknowledge_irreversible": False}
    )
    assert resp.status_code == 400
    assert "acknowledge_irreversible" in resp.json()["detail"]
    assert "Nothing was deleted" in resp.json()["detail"]


def test_erasure_refuses_when_acknowledgement_is_omitted_entirely(client_as):
    resp = client_as("owner").post(f"{BASE}/erasure", json={"confirm": SLUG})
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", ["", "   ", "wrong", "ACME-BOOKS", "acme_books", "acme-book"])
def test_erasure_refuses_a_confirmation_that_is_not_the_exact_slug(client_as, bad):
    resp = client_as("owner").post(f"{BASE}/erasure", json=_valid_erasure_body(confirm=bad))
    assert resp.status_code == 400
    assert "Nothing was deleted" in resp.json()["detail"]


def test_erasure_requires_the_confirm_field_to_be_present(client_as):
    resp = client_as("owner").post(
        f"{BASE}/erasure", json={"acknowledge_irreversible": True}
    )
    assert resp.status_code == 422  # pydantic: confirm is required


def test_destructive_erasure_is_post_only():
    """A destructive action must never be triggerable by a link or a prefetch.

    Inspects the route table directly rather than issuing a request, so this
    asserts the routing contract itself: the erasure path accepts GET (read the
    receipt) and POST (perform it), and nothing else. Were the destructive
    handler ever registered under GET, a browser prefetch or a crawled link
    could erase a customer's workspace.
    """
    path = "/api/workspaces/{workspace_id}/data-rights/erasure"
    methods = methods_for(app, path)
    assert methods == {"GET", "POST"}, methods
    # And the mutating verb is POST — never GET, PUT-as-create, or DELETE.
    assert "DELETE" not in methods


# --------------------------------------------------------------------------- #
# Retention validation happens before any DB work
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_days", [-1, 3651, 99999])
def test_out_of_range_retention_is_rejected_without_touching_the_database(client_as, bad_days):
    resp = client_as("owner").put(f"{BASE}/retention", json={"ledger_retention_days": bad_days})
    assert resp.status_code == 422
    assert "ledger_retention_days" in resp.json()["detail"]


def test_non_integer_retention_is_rejected(client_as):
    resp = client_as("owner").put(f"{BASE}/retention", json={"ledger_retention_days": "thirty"})
    assert resp.status_code == 422
