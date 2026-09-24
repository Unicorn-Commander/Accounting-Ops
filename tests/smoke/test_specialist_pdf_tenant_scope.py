"""Tenant and CPA-export failure smoke coverage for specialist drafts."""
from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


WORKSPACE_A = uuid.uuid4()
WORKSPACE_B = uuid.uuid4()
ENTITY_A = uuid.uuid4()
ENTITY_B = uuid.uuid4()


def _make_client(drafts_dir: Path, role: str, workspace_id: uuid.UUID) -> TestClient:
    import app.api.specialist_pdf as specialist_pdf
    from app.tenancy import resolve_workspace

    app = FastAPI()
    app.include_router(specialist_pdf.router)
    context = MagicMock(workspace_id=workspace_id, role=role)

    def resolve():
        return context

    def require_role(_workspace, minimum):
        if role == "viewer" and minimum == "accountant":
            raise HTTPException(status_code=403, detail="Insufficient role")

    specialist_pdf._DRAFTS_DIR = drafts_dir
    app.dependency_overrides[resolve_workspace] = resolve
    specialist_pdf.require_role = require_role
    specialist_pdf.assert_entity_in_scope = lambda *_args: None
    specialist_pdf._get_entity_display_name = AsyncMock(return_value="Tenant B")
    specialist_pdf._html_to_pdf = lambda _html: b"tenant-b-pdf"
    return TestClient(app)


@pytest.fixture()
def tenant_drafts(tmp_path: Path) -> Path:
    import app.api.specialist_pdf as specialist_pdf

    drafts = tmp_path / "drafts"
    path = drafts / str(WORKSPACE_B) / str(ENTITY_B)
    path.mkdir(parents=True)
    (path / specialist_pdf._SLUG_META["form-1120"]["draft_file"].format(year=2024)).write_text(
        "TENANT-B-ONLY-DRAFT", encoding="utf-8"
    )
    return drafts


def test_draft_is_scoped_to_workspace_and_entity(tenant_drafts: Path):
    url = "/api/workspaces/{}/entities/{}/tax-drafts/2024/form-1120.md"
    with _make_client(tenant_drafts, "accountant", WORKSPACE_A) as client:
        response = client.get(url.format(WORKSPACE_A, ENTITY_A))
        assert response.status_code == 404
        assert "TENANT-B-ONLY-DRAFT" not in response.content.decode()

    with _make_client(tenant_drafts, "viewer", WORKSPACE_B) as client:
        response = client.get(url.format(WORKSPACE_B, ENTITY_B))
        assert response.status_code == 403

    with _make_client(tenant_drafts, "accountant", WORKSPACE_B) as client:
        response = client.get(url.format(WORKSPACE_B, ENTITY_B))
        assert response.status_code == 200
        assert response.content == b"TENANT-B-ONLY-DRAFT"


@pytest.mark.parametrize("slug", ["form-1120", "1099-nec", "sc1120", "officer-compensation", "nol-schedule-l", "audit-defense-scdor-ftf", "coordinator-summary"])
@pytest.mark.parametrize("tax_year", [2023, 2024])
def test_draft_path_never_falls_back_to_root(tenant_drafts: Path, slug: str, tax_year: int):
    import app.api.specialist_pdf as specialist_pdf

    root_file = tenant_drafts / specialist_pdf._SLUG_META[slug]["draft_file"].format(year=tax_year)
    root_file.write_text("ROOT-MUST-NOT-RESOLVE", encoding="utf-8")
    with patch.object(specialist_pdf, "_DRAFTS_DIR", tenant_drafts):
        assert specialist_pdf._draft_path(WORKSPACE_A, ENTITY_A, tax_year, slug) is None


def test_handoff_zip_marks_unreachable_sidecars(tenant_drafts: Path):
    import app.api.specialist_pdf as specialist_pdf
    from app.tenancy import resolve_workspace

    app = FastAPI()
    app.include_router(specialist_pdf.router)
    context = MagicMock(workspace_id=WORKSPACE_B, role="accountant")
    app.dependency_overrides[resolve_workspace] = lambda: context

    async def missing(*_args, **_kwargs):
        return None, "CPA export request failed: ConnectError"

    with (
        patch.object(specialist_pdf, "_DRAFTS_DIR", tenant_drafts),
        patch.object(specialist_pdf, "require_role"),
        patch.object(specialist_pdf, "assert_entity_in_scope"),
        patch.object(specialist_pdf, "_get_entity_display_name", AsyncMock(return_value="Tenant B")),
        patch.object(specialist_pdf, "_fetch_cpa_export_json", missing),
        patch.object(specialist_pdf, "_fetch_cpa_export_csv", missing),
        patch.object(specialist_pdf, "_fetch_tax_review_queue_json", AsyncMock(return_value=b"{}")),
        patch.object(specialist_pdf, "_build_cover_letter_pdf", return_value=b"cover"),
        patch.object(specialist_pdf, "get_settings", return_value=SimpleNamespace(cpa_export_token="")),
    ):
        with TestClient(app) as client:
            response = client.get(
                f"/api/workspaces/{WORKSPACE_B}/entities/{ENTITY_B}/tax-drafts/2024/handoff-package.zip"
            )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        names = set(bundle.namelist())
        assert "data/MISSING-form-1120-package.json.txt" in names
        assert "data/MISSING-journal-2024.csv.txt" in names
        readme = bundle.read("README.txt").decode()
        assert "MISSING-form-1120-package.json.txt" in readme
        assert "MISSING-journal-2024.csv.txt" in readme
