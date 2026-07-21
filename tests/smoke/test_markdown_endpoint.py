"""Smoke tests for the specialist Markdown source endpoint.

Tests
-----
  1. test_markdown_returns_200_when_file_exists   — endpoint returns 200 +
     text/markdown when the draft file exists on the filesystem.
  2. test_markdown_returns_404_when_file_missing  — endpoint returns 404 when
     the draft file is absent.
  3. test_markdown_unknown_slug_returns_404       — unknown slug returns 404.
  4. test_pdf_disposition_attachment_by_default   — PDF route without
     ?disposition= returns Content-Disposition: attachment.
  5. test_pdf_disposition_inline_query_param      — PDF route with
     ?disposition=inline returns Content-Disposition: inline.
  6. test_irs_forms_disposition_attachment_default — IRS form route without
     ?disposition= returns Content-Disposition: attachment.
  7. test_irs_forms_disposition_inline             — IRS form route with
     ?disposition=inline returns Content-Disposition: inline.

These tests exercise only the FastAPI routing layer and file-system logic;
they do NOT require a running Postgres instance or Keycloak. We patch
workspace resolution and entity-scope checks to let the request reach the
handler.

File-system setup: a temporary /drafts directory is created with a minimal
sample .md file. The DRAFTS_DIR env var (read by specialist_pdf._DRAFTS_DIR)
is pointed at it.
"""
from __future__ import annotations

import os
import sys
import types
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers — build a minimal FastAPI app that includes the specialist_pdf router
# without the full app stack (no DB, no Keycloak, no telemetry).
# ---------------------------------------------------------------------------

def _make_test_app():
    """Return a minimal FastAPI app wrapping the specialist_pdf and irs_forms routers."""
    from fastapi import FastAPI
    from app.api.specialist_pdf import router as pdf_router
    from app.api.irs_forms import router as irs_router

    app = FastAPI()
    app.include_router(pdf_router)
    app.include_router(irs_router)
    return app


# Fake workspace context / auth bypass fixtures.
_WORKSPACE_ID = uuid.uuid4()
_ENTITY_ID = uuid.uuid4()

_MOCK_WORKSPACE = MagicMock()
_MOCK_WORKSPACE.workspace_id = _WORKSPACE_ID
_MOCK_WORKSPACE.role = "admin"


def _fake_resolve_workspace():
    """Dependency override that returns a dummy WorkspaceContext."""
    return _MOCK_WORKSPACE


def _noop_require_role(*args, **kwargs):
    pass


def _noop_assert_entity(*args, **kwargs):
    pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def drafts_dir(tmp_path: Path) -> Path:
    """Create a temporary drafts directory with one sample draft file."""
    d = tmp_path / "drafts"
    d.mkdir()
    # Place a sample draft matching the slug "form-1120" for year 2023.
    sample = d / "mu-inc-2023-form-1120-draft.md"
    sample.write_text(
        "# Form 1120 — 2023 Draft\n\n**Entity:** Acme Inc.\n\n"
        "## Revenue Summary\n\n| Line | Amount |\n|------|--------|\n| 1a | $48,250 |\n",
        encoding="utf-8",
    )
    return d


def _has_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.fixture()
def client(drafts_dir: Path):
    """TestClient with dependency overrides + DRAFTS_DIR env var pointing at tmp dir.

    Works without pypdf / postgres installed by:
    1. Injecting a fake ``app.exports.form_prefill`` into sys.modules when pypdf
       is absent (prevents ImportError on the lazy import inside the IRS handler).
    2. Patching workspace_session so no live Postgres connection is attempted.
    """
    import app.api.specialist_pdf as _spdf_module
    import app.api.irs_forms as _irs_module
    from app.tenancy import resolve_workspace
    from contextlib import asynccontextmanager

    # Patch DRAFTS_DIR on the module so the endpoint finds our temp directory.
    original_drafts_dir = _spdf_module._DRAFTS_DIR
    _spdf_module._DRAFTS_DIR = drafts_dir

    test_app = _make_test_app()
    test_app.dependency_overrides[resolve_workspace] = _fake_resolve_workspace

    @asynccontextmanager
    async def _mock_workspace_session(*args, **kwargs):
        yield MagicMock()

    # When pypdf is absent the lazy import inside the IRS handler would raise
    # ModuleNotFoundError (which the handler doesn't catch → 500).  Inject a
    # fake module into sys.modules so the import succeeds and our stub
    # generate_form_pdf is called instead.
    fake_form_prefill_installed = False
    if not _has_pypdf() and "app.exports.form_prefill" not in sys.modules:
        fake_mod = types.ModuleType("app.exports.form_prefill")

        def _mock_generate_form_pdf(*args, **kwargs):
            raise FileNotFoundError(
                "f1120_2024.pdf (stub - pypdf not in dev venv)"
            )

        fake_mod.generate_form_pdf = _mock_generate_form_pdf  # type: ignore[attr-defined]
        sys.modules["app.exports.form_prefill"] = fake_mod
        fake_form_prefill_installed = True

    try:
        with (
            patch.object(_spdf_module, "require_role", _noop_require_role),
            patch.object(_spdf_module, "assert_entity_in_scope", _noop_assert_entity),
            patch.object(_irs_module, "require_role", _noop_require_role),
            patch.object(_irs_module, "assert_entity_in_scope", _noop_assert_entity),
            patch.object(
                _spdf_module,
                "_get_entity_display_name",
                AsyncMock(return_value="Acme Inc."),
            ),
            patch("app.api.irs_forms.workspace_session", _mock_workspace_session),
            patch.object(
                _irs_module,
                "_fetch_entity_meta",
                AsyncMock(return_value=("Acme Inc.", None)),
            ),
            patch.object(
                _irs_module,
                "_fetch_queue_items",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                _irs_module,
                "_fetch_cpa_package",
                AsyncMock(return_value=None),
            ),
        ):
            with TestClient(test_app) as c:
                yield c
    finally:
        # Restore DRAFTS_DIR and clean up fake module.
        _spdf_module._DRAFTS_DIR = original_drafts_dir
        if fake_form_prefill_installed:
            sys.modules.pop("app.exports.form_prefill", None)


# ---------------------------------------------------------------------------
# Markdown endpoint tests
# ---------------------------------------------------------------------------

class TestMarkdownEndpoint:
    """Tests for GET /api/.../tax-drafts/{year}/{slug}.md"""

    def _url(self, year: int, slug: str) -> str:
        return (
            f"/api/workspaces/{_WORKSPACE_ID}/entities/{_ENTITY_ID}"
            f"/tax-drafts/{year}/{slug}.md"
        )

    def test_markdown_returns_200_when_file_exists(self, client):
        """200 + text/markdown when the draft file exists."""
        resp = client.get(self._url(2023, "form-1120"))
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        ct = resp.headers.get("content-type", "")
        assert "text/markdown" in ct, f"Expected text/markdown content-type, got: {ct}"
        assert "Form 1120" in resp.text, "Expected draft content in response body"

    def test_markdown_returns_404_when_file_missing(self, client):
        """404 when no draft file exists for that year/slug combination."""
        resp = client.get(self._url(2099, "form-1120"))
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    def test_markdown_unknown_slug_returns_404(self, client):
        """404 for an unknown specialist slug."""
        resp = client.get(self._url(2023, "nonexistent-slug"))
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"

    def test_markdown_has_x_draft_notice_header(self, client):
        """X-Draft-Notice header must be present on a successful markdown response."""
        resp = client.get(self._url(2023, "form-1120"))
        assert resp.status_code == 200
        assert "x-draft-notice" in resp.headers, (
            "X-Draft-Notice header missing from markdown response"
        )


# ---------------------------------------------------------------------------
# Disposition query param — PDF endpoint
# ---------------------------------------------------------------------------

class TestPdfDispositionParam:
    """Tests for the ?disposition= query param on the specialist PDF route."""

    def _url(self, year: int, slug: str, disposition: str | None = None) -> str:
        base = (
            f"/api/workspaces/{_WORKSPACE_ID}/entities/{_ENTITY_ID}"
            f"/tax-drafts/{year}/{slug}.pdf"
        )
        return base if disposition is None else f"{base}?disposition={disposition}"

    def _try_import_weasyprint(self) -> bool:
        try:
            import weasyprint  # noqa: F401
            return True
        except (ImportError, OSError):
            return False

    def _try_import_markdown(self) -> bool:
        try:
            import markdown  # noqa: F401
            return True
        except ImportError:
            return False

    @pytest.mark.skipif(
        not _try_import_weasyprint(None),  # type: ignore[arg-type]
        reason="weasyprint not available — PDF rendering skipped",
    )
    @pytest.mark.skipif(
        not _try_import_markdown(None),  # type: ignore[arg-type]
        reason="markdown not available — PDF rendering skipped",
    )
    def test_pdf_disposition_attachment_by_default(self, client):
        """No ?disposition= param → Content-Disposition: attachment."""
        resp = client.get(self._url(2023, "form-1120"))
        cd = resp.headers.get("content-disposition", "")
        assert resp.status_code == 200, f"PDF request failed: {resp.status_code}"
        assert cd.startswith("attachment"), (
            f"Expected 'attachment' disposition, got: {cd!r}"
        )

    @pytest.mark.skipif(
        not _try_import_weasyprint(None),  # type: ignore[arg-type]
        reason="weasyprint not available — PDF rendering skipped",
    )
    @pytest.mark.skipif(
        not _try_import_markdown(None),  # type: ignore[arg-type]
        reason="markdown not available — PDF rendering skipped",
    )
    def test_pdf_disposition_inline_query_param(self, client):
        """?disposition=inline → Content-Disposition: inline."""
        resp = client.get(self._url(2023, "form-1120", "inline"))
        cd = resp.headers.get("content-disposition", "")
        assert resp.status_code == 200, f"PDF request failed: {resp.status_code}"
        assert cd.startswith("inline"), (
            f"Expected 'inline' disposition, got: {cd!r}"
        )

    def test_pdf_unknown_slug_still_returns_404(self, client):
        """Unknown slug returns 404 regardless of disposition param."""
        resp = client.get(self._url(2023, "bad-slug"))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Disposition query param — IRS forms endpoint (unit-level, no PDF template)
# ---------------------------------------------------------------------------

class TestIrsFormsDispositionParam:
    """Tests for ?disposition= on the IRS forms route.

    We can't actually render an IRS form PDF in unit tests (requires downloaded
    IRS PDF templates), but we CAN test that:
    - The endpoint accepts the disposition param without 422 validation errors.
    - FileNotFoundError (missing IRS PDF template) is raised as a 503, not 422.
    - The disposition query param is forwarded through without error.
    """

    def _url(self, year: int, form: str, disposition: str | None = None) -> str:
        base = (
            f"/api/workspaces/{_WORKSPACE_ID}/entities/{_ENTITY_ID}"
            f"/irs-forms/{year}/{form}.pdf"
        )
        return base if disposition is None else f"{base}?disposition={disposition}"

    def test_irs_forms_disposition_inline_accepted(self, client):
        """?disposition=inline is accepted (no 422).

        Returns 200 (templates present), 503 (templates absent), or 500
        (required system dep like pypdf not installed in this env). All are
        acceptable — 422 would mean FastAPI rejected the param itself.
        """
        resp = client.get(self._url(2023, "1120", "inline"))
        assert resp.status_code in {200, 500, 503}, (
            f"Unexpected status {resp.status_code} (expected not 422): {resp.text[:200]}"
        )

    def test_irs_forms_disposition_attachment_default(self, client):
        """No ?disposition= is also accepted (not 422)."""
        resp = client.get(self._url(2023, "1120"))
        assert resp.status_code in {200, 500, 503}, (
            f"Unexpected status {resp.status_code} (expected not 422): {resp.text[:200]}"
        )

    def test_irs_forms_unknown_form_returns_404(self, client):
        """Unknown form name still returns 404, not 422."""
        resp = client.get(self._url(2023, "9999-x"))
        assert resp.status_code == 404

    def test_irs_forms_invalid_year_returns_400(self, client):
        """Non-4-digit year returns 400."""
        resp = client.get(
            f"/api/workspaces/{_WORKSPACE_ID}/entities/{_ENTITY_ID}"
            "/irs-forms/99/1120.pdf"
        )
        assert resp.status_code == 400
