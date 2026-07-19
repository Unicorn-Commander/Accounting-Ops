"""Smoke tests for the REAL (flag-gated) e-signature capability.

No live stack (no Postgres / Keycloak / Docuseal). Three layers:

  A. Provider resolution + status vocabulary (pure) — Manual when unconfigured,
     Docuseal when both env vars set, and ManualProvider needs NO network.
  B. The SignatureRequest lifecycle at the SERVICE layer, driven against a tiny
     in-memory FakeSession + a FakeProvider: created->sent on create,
     ->signed on record_signed (proving the filesystem storage fallback),
     ->canceled on cancel, and the terminal-state guards.
  C. A FastAPI route-shape test of the create endpoint: the per-workspace
     feature flag gates creation (403 when disabled, 201 when enabled).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.esign as api_mod
import app.esign.provider as provider_mod
import app.esign.service as service
from app.engagement.policy import EngagementPolicyData
from app.esign.provider import (
    SIGNATURE_STATUSES,
    TERMINAL_STATUSES,
    DocusealProvider,
    ManualProvider,
    coerce_status,
    resolve_provider,
)
from app.models.ledger import ReviewMode
from app.tenancy import WorkspaceContext, resolve_workspace

_WID = uuid.uuid4()
_EID = uuid.uuid4()
_WPID = uuid.uuid4()
_SRID = uuid.uuid4()


# ===========================================================================
# In-memory fakes
# ===========================================================================


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self):  # noqa: ANN201
        return self

    def first(self):  # noqa: ANN201
        return self._rows[0] if self._rows else None

    def all(self):  # noqa: ANN201
        return list(self._rows)


class FakeSession:
    """Records executed statements; returns a single canned row for SELECTs."""

    def __init__(self, row: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._row = row

    async def execute(self, statement, params=None):  # noqa: ANN001, ANN201
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if sql.strip().upper().startswith("SELECT"):
            return _FakeResult([self._row] if self._row else [])
        return _FakeResult([])

    def kinds(self) -> list[str]:
        return [c[0].strip().split()[0].upper() for c in self.calls]


class _FakeProvider:
    name = "fake"
    is_remote = True

    def __init__(
        self,
        *,
        status: str = "sent",
        external_id: str | None = "ext-1",
        signing_url: str | None = "https://docuseal.example/s/abc",
        signed: bytes | None = b"%PDF-signed",
    ) -> None:
        self._status = status
        self._external_id = external_id
        self._signing_url = signing_url
        self._signed = signed
        self.calls: list[tuple] = []

    async def create_request(self, document_bytes, signer_name, signer_email, subject):
        self.calls.append(("create", signer_name, signer_email, subject, len(document_bytes)))
        return {
            "external_id": self._external_id,
            "signing_url": self._signing_url,
            "status": self._status,
        }

    async def get_status(self, external_id):
        self.calls.append(("status", external_id))
        return self._status

    async def fetch_signed(self, external_id):
        self.calls.append(("fetch", external_id))
        return self._signed


class _NoGarage:
    """A GarageS3Client stand-in that is never configured (forces the filesystem
    storage fallback in record_signed)."""

    def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
        pass

    def is_configured(self) -> bool:
        return False


# ===========================================================================
# A. Provider resolution + status vocabulary
# ===========================================================================


class TestProviderResolution:
    def test_manual_when_unconfigured(self):
        provider = resolve_provider({})
        assert isinstance(provider, ManualProvider)
        assert provider.is_remote is False
        assert provider.name == "manual"

    def test_docuseal_when_both_env_set(self):
        provider = resolve_provider(
            {
                "ESIGN_DOCUSEAL_BASE_URL": "https://docuseal.example",
                "ESIGN_DOCUSEAL_API_KEY": "key-123",
            }
        )
        assert isinstance(provider, DocusealProvider)
        assert provider.is_remote is True
        assert provider.name == "docuseal"

    def test_manual_when_partially_configured(self):
        # base URL but no key -> still Manual (never a half-wired remote).
        assert isinstance(
            resolve_provider({"ESIGN_DOCUSEAL_BASE_URL": "https://docuseal.example"}),
            ManualProvider,
        )
        assert isinstance(
            resolve_provider({"ESIGN_DOCUSEAL_API_KEY": "key-123"}), ManualProvider
        )


class TestStatusVocabulary:
    def test_vocab_membership(self):
        assert SIGNATURE_STATUSES == {
            "created", "sent", "out_for_signature", "signed", "declined", "canceled",
        }

    def test_terminal_states(self):
        assert TERMINAL_STATUSES == {"signed", "declined", "canceled"}

    def test_coerce_unknown_falls_back_to_default(self):
        assert coerce_status("not-a-status") == "sent"
        assert coerce_status(None) == "sent"
        assert coerce_status("not-a-status", default="created") == "created"

    def test_coerce_normalizes_case(self):
        assert coerce_status("SIGNED") == "signed"
        assert coerce_status(" Out_For_Signature ") == "out_for_signature"


class TestManualProviderNeedsNoNetwork:
    async def test_manual_create_does_not_touch_httpx(self, monkeypatch):
        # Make any attempt to construct an HTTP client explode — proving Manual
        # performs zero network I/O.
        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("ManualProvider must not use the network")

        monkeypatch.setattr(provider_mod.httpx, "AsyncClient", _boom)

        manual = ManualProvider()
        ack = await manual.create_request(b"%PDF-1.4", "Jane Doe", "jane@example.com", "Sign")
        assert ack["signing_url"] is None
        assert ack["external_id"] is None
        assert ack["status"] == "out_for_signature"
        assert await manual.get_status("anything") in SIGNATURE_STATUSES
        assert await manual.fetch_signed("anything") is None


# ===========================================================================
# B. Service-layer lifecycle (FakeSession + FakeProvider)
# ===========================================================================


class TestRequestSignature:
    async def test_created_then_sent(self):
        session = FakeSession()
        provider = _FakeProvider(status="sent")
        result = await service.request_signature(
            session,
            workspace_id=_WID,
            entity_id=_EID,
            work_product_id=_WPID,
            signer_name="Jane Doe",
            signer_email="jane@example.com",
            pdf_bytes=b"%PDF-1.4 work product",
            subject="Please sign",
            provider=provider,
        )
        assert result["status"] == "sent"
        assert result["provider"] == "fake"
        assert result["external_id"] == "ext-1"
        assert result["signing_url"] == "https://docuseal.example/s/abc"

        # INSERT (status 'created') then UPDATE (status 'sent').
        assert session.kinds()[0] == "INSERT"
        assert "UPDATE" in session.kinds()
        # The provider was actually invoked with the rendered bytes.
        assert provider.calls and provider.calls[0][0] == "create"

    async def test_manual_provider_lands_out_for_signature(self):
        session = FakeSession()
        result = await service.request_signature(
            session,
            workspace_id=_WID,
            entity_id=_EID,
            work_product_id=_WPID,
            signer_name="Jane Doe",
            signer_email=None,
            pdf_bytes=b"%PDF-1.4",
            provider=ManualProvider(),
        )
        assert result["status"] == "out_for_signature"
        assert result["provider"] == "manual"
        assert result["signing_url"] is None


class TestRecordSigned:
    async def test_stores_pdf_to_filesystem_and_marks_signed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ESIGN_SIGNED_DIR", str(tmp_path))
        monkeypatch.setattr(service, "GarageS3Client", _NoGarage)

        session = FakeSession(
            row={"id": str(_SRID), "workspace_id": str(_WID), "status": "sent"}
        )
        result = await service.record_signed(
            session,
            entity_id=_EID,
            work_product_id=_WPID,
            sr_id=_SRID,
            signed_bytes=b"%PDF-signed-bytes",
        )
        assert result is not None
        assert result["status"] == "signed"
        assert result["signed_garage_key"] is None
        assert result["signed_path"] is not None
        stored = Path(result["signed_path"])
        assert stored.exists()
        assert stored.read_bytes() == b"%PDF-signed-bytes"
        assert "UPDATE" in session.kinds()

    async def test_marks_signed_without_bytes(self, monkeypatch):
        # A wet signature captured out-of-band: no stored artifact, status flips.
        monkeypatch.setattr(service, "GarageS3Client", _NoGarage)
        session = FakeSession(
            row={"id": str(_SRID), "workspace_id": str(_WID), "status": "out_for_signature"}
        )
        result = await service.record_signed(
            session,
            entity_id=_EID,
            work_product_id=_WPID,
            sr_id=_SRID,
            signed_bytes=None,
        )
        assert result["status"] == "signed"
        assert result["signed_path"] is None
        assert result["signed_garage_key"] is None

    async def test_missing_request_returns_none(self):
        session = FakeSession(row=None)
        result = await service.record_signed(
            session, entity_id=_EID, work_product_id=_WPID, sr_id=_SRID, signed_bytes=None
        )
        assert result is None

    async def test_cannot_revive_canceled_request(self):
        session = FakeSession(
            row={"id": str(_SRID), "workspace_id": str(_WID), "status": "canceled"}
        )
        with pytest.raises(ValueError):
            await service.record_signed(
                session, entity_id=_EID, work_product_id=_WPID, sr_id=_SRID,
                signed_bytes=None,
            )


class TestCancel:
    async def test_transitions_to_canceled(self):
        session = FakeSession(row={"id": str(_SRID), "status": "sent"})
        result = await service.cancel(
            session, entity_id=_EID, work_product_id=_WPID, sr_id=_SRID
        )
        assert result == {"id": str(_SRID), "status": "canceled"}
        assert "UPDATE" in session.kinds()

    async def test_already_signed_raises(self):
        session = FakeSession(row={"id": str(_SRID), "status": "signed"})
        with pytest.raises(ValueError):
            await service.cancel(
                session, entity_id=_EID, work_product_id=_WPID, sr_id=_SRID
            )

    async def test_missing_returns_none(self):
        session = FakeSession(row=None)
        assert (
            await service.cancel(
                session, entity_id=_EID, work_product_id=_WPID, sr_id=_SRID
            )
            is None
        )


class TestSyncStatus:
    async def test_manual_is_a_noop(self):
        session = FakeSession(
            row={
                "id": str(_SRID), "workspace_id": str(_WID), "provider": "manual",
                "external_id": None, "status": "out_for_signature",
                "signed_garage_key": None, "signed_path": None,
            }
        )
        result = await service.sync_status(
            session, entity_id=_EID, work_product_id=_WPID, sr_id=_SRID
        )
        assert result == {"id": str(_SRID), "status": "out_for_signature"}

    async def test_terminal_request_unchanged(self):
        session = FakeSession(
            row={
                "id": str(_SRID), "workspace_id": str(_WID), "provider": "fake",
                "external_id": "ext-1", "status": "signed",
                "signed_garage_key": None, "signed_path": "/x.pdf",
            }
        )
        result = await service.sync_status(
            session, entity_id=_EID, work_product_id=_WPID, sr_id=_SRID,
            provider=_FakeProvider(status="signed"),
        )
        assert result["status"] == "signed"

    async def test_remote_signed_pulls_and_stores_pdf(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ESIGN_SIGNED_DIR", str(tmp_path))
        monkeypatch.setattr(service, "GarageS3Client", _NoGarage)
        session = FakeSession(
            row={
                "id": str(_SRID), "workspace_id": str(_WID), "provider": "docuseal",
                "external_id": "ext-1", "status": "out_for_signature",
                "signed_garage_key": None, "signed_path": None,
            }
        )
        provider = _FakeProvider(status="signed", signed=b"%PDF-completed")
        result = await service.sync_status(
            session, entity_id=_EID, work_product_id=_WPID, sr_id=_SRID, provider=provider
        )
        assert result["status"] == "signed"
        assert result["signed_path"] is not None
        assert Path(result["signed_path"]).read_bytes() == b"%PDF-completed"
        # The completed document was fetched from the provider.
        assert any(c[0] == "fetch" for c in provider.calls)

    async def test_missing_returns_none(self):
        session = FakeSession(row=None)
        assert (
            await service.sync_status(
                session, entity_id=_EID, work_product_id=_WPID, sr_id=_SRID
            )
            is None
        )


# ===========================================================================
# C. Route-shape: the per-workspace feature-flag gate on creation
# ===========================================================================


def _policy(*, esign: bool) -> EngagementPolicyData:
    return EngagementPolicyData(
        workspace_id=_WID,
        review_mode=ReviewMode.PRO_REQUIRED,
        has_licensed_pro=False,
        prefer_local_inference=False,
        submission_channels={"esign": True} if esign else {},
        rail_kill_switch=False,
    )


def _make_workspace(role: str = "accountant") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WID,
        workspace_slug="acme",
        role=role,
        user_uc_uid="uc-test-user",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )


@pytest.fixture()
def patch_api(monkeypatch):
    """Neutralize workspace_session + let each test choose the policy + render."""
    state: dict = {"policy": _policy(esign=False)}

    async def _fake_get_or_create_policy(session, workspace_id):
        return state["policy"]

    @asynccontextmanager
    async def _fake_workspace_session(session_factory, workspace_id):
        yield object()

    monkeypatch.setattr(api_mod, "get_or_create_policy", _fake_get_or_create_policy)
    monkeypatch.setattr(api_mod, "workspace_session", _fake_workspace_session)

    def _set_policy(policy: EngagementPolicyData) -> None:
        state["policy"] = policy

    return _set_policy


def _client(role: str = "accountant") -> TestClient:
    app = FastAPI()
    app.include_router(api_mod.router)
    app.dependency_overrides[resolve_workspace] = lambda: _make_workspace(role)
    return TestClient(app)


def _post_url() -> str:
    return (
        f"/api/workspaces/{_WID}/entities/{_EID}"
        f"/work-products/{_WPID}/signature-requests"
    )


def test_create_blocked_when_esign_flag_disabled(patch_api):
    patch_api(_policy(esign=False))
    resp = _client("accountant").post(
        _post_url(), json={"signer_name": "Jane Doe", "signer_email": "jane@example.com"}
    )
    assert resp.status_code == 403, resp.text
    assert "esign" in resp.json()["detail"].lower()


def test_create_allowed_when_esign_flag_enabled(patch_api, monkeypatch):
    patch_api(_policy(esign=True))

    async def _fake_fetch_wp(session, entity_id, wp_id):
        return {
            "id": str(_WPID), "kind": "w-4p", "tax_year": 2026,
            "status": "self_approved", "title": "W-4P", "inputs": {},
        }

    def _fake_render(kind, tax_year, status, inputs):
        return b"%PDF-1.4 rendered", "application/pdf"

    async def _fake_request_signature(session, **kwargs):
        return {
            "id": str(_SRID),
            "work_product_id": str(_WPID),
            "provider": "manual",
            "status": "out_for_signature",
            "external_id": None,
            "signing_url": None,
            "signer_name": kwargs["signer_name"],
            "signer_email": kwargs["signer_email"],
        }

    monkeypatch.setattr(api_mod, "_fetch_work_product", _fake_fetch_wp)
    monkeypatch.setattr(api_mod, "_render_work_product", _fake_render)
    monkeypatch.setattr(api_mod, "request_signature", _fake_request_signature)

    resp = _client("accountant").post(
        _post_url(), json={"signer_name": "Jane Doe"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "out_for_signature"
    assert body["provider"] == "manual"
    assert body["id"] == str(_SRID)


def test_create_requires_accountant_role(patch_api):
    patch_api(_policy(esign=True))
    resp = _client("viewer").post(
        _post_url(), json={"signer_name": "Jane Doe"}
    )
    assert resp.status_code == 403, resp.text
