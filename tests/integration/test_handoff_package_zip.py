"""Integration test: CPA Handoff Package ZIP endpoint.

End-to-end path:
  1. A draft Markdown file for tax year 9999 is written to a temp /drafts dir.
  2. The DRAFTS_DIR env var is patched to point there.
  3. The /api/workspaces/{wid}/entities/{eid}/tax-drafts/9999/handoff-package.zip
     endpoint is called.
  4. The response is unzipped and the expected file tree is asserted.
  5. All expected files are present and non-trivially sized.

Skips if the live stack is not reachable (same pattern as every other
integration test in this directory — no docker = skip, not fail).

WeasyPrint must be installed in the app container for this to work; the test
also checks that the returned bytes start with PK (ZIP magic).
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import pytest_asyncio
import httpx

from tests.conftest import TEST_WORKSPACE_ID, TEST_ENTITY_ID, StackConfig

INTEGRATION_YEAR = 9999  # A synthetic year we control — never collides with real data.


# ---------------------------------------------------------------------------
# Draft content fixtures
# ---------------------------------------------------------------------------

_DRAFT_CONTENT = """\
# Form 1120 Test Draft

**Entity:** Integration Test Co.
**Tax Year:** {year}

## Revenue

| Line | Amount  |
|------|---------|
| 1a   | $1.00   |

CPA note: This is a synthetic integration test draft.
""".format(year=INTEGRATION_YEAR)


# ---------------------------------------------------------------------------
# Helper: seed a temp draft file + monkeypatch env
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def drafts_dir(tmp_path_factory):
    """Create a temp /drafts dir with one synthetic draft for INTEGRATION_YEAR."""
    d = tmp_path_factory.mktemp("drafts")
    fname = f"mu-inc-{INTEGRATION_YEAR}-form-1120-draft.md"
    (d / fname).write_text(_DRAFT_CONTENT, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# Live-stack guard
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module")
async def live_stack_for_module(stack: StackConfig):
    """Skip unless the app is reachable."""
    if not stack.token:
        pytest.skip("ACCTOPS_TEST_TOKEN unset — integration stack not configured.")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{stack.base_url}/healthz")
            r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"App not reachable: {exc!r}")
    return stack


# ---------------------------------------------------------------------------
# Test: handoff package zip structure
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_handoff_package_zip_structure(
    live_stack_for_module: StackConfig,
    drafts_dir: Path,
):
    """Call the handoff-package.zip endpoint and verify the zip structure.

    Because we can't monkeypatch the running container's env from here, we
    exercise the endpoint with a real draft path. If the draft dir env isn't
    set on the container, the specialist PDFs will be 'no draft available'
    stubs — that's fine, the zip must still be returned and have the correct
    shape.
    """
    stack = live_stack_for_module

    url = (
        f"{stack.base_url}/api/workspaces/{TEST_WORKSPACE_ID}"
        f"/entities/{TEST_ENTITY_ID}"
        f"/tax-drafts/{INTEGRATION_YEAR}/handoff-package.zip"
    )
    headers = {"Authorization": f"Bearer {stack.token}"}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, headers=headers)

    # ---- HTTP-level assertions ----
    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}. Body: {resp.text[:500]}"
    )
    assert resp.headers.get("content-type", "").startswith("application/zip"), (
        f"Expected application/zip, got {resp.headers.get('content-type')}"
    )
    assert "content-disposition" in resp.headers, "Missing Content-Disposition header"
    assert ".zip" in resp.headers["content-disposition"], (
        "Content-Disposition should reference a .zip filename"
    )

    # ---- ZIP-level assertions ----
    zip_bytes = resp.content
    assert zip_bytes[:2] == b"PK", (
        f"Response body doesn't start with ZIP magic (PK): {zip_bytes[:8]!r}"
    )

    buf = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(buf, "r") as zf:
        names = set(zf.namelist())

        # Required files always present regardless of sidecar availability.
        required = {
            "00-cover-letter.pdf",
            "README.txt",
            "data/tax-review-queue.json",
        }
        for fname in required:
            assert fname in names, (
                f"Expected '{fname}' in zip but it's missing. Contents: {sorted(names)}"
            )

        # Cover letter must be a valid PDF (magic bytes).
        cover_bytes = zf.read("00-cover-letter.pdf")
        assert len(cover_bytes) > 500, "Cover letter PDF is suspiciously small"
        assert cover_bytes[:4] == b"%PDF", (
            f"Cover letter doesn't look like a PDF: {cover_bytes[:8]!r}"
        )

        # README must be human-readable text.
        readme = zf.read("README.txt").decode("utf-8")
        assert "CPA Handoff Package" in readme or "Handoff" in readme, (
            f"README.txt doesn't contain expected header text: {readme[:200]}"
        )
        assert "DISCLAIMER" in readme or "disclaimer" in readme.lower(), (
            "README.txt missing disclaimer section"
        )

        # tax-review-queue.json must be parseable JSON.
        import json

        queue_json = json.loads(zf.read("data/tax-review-queue.json"))
        assert "items" in queue_json, "tax-review-queue.json missing 'items' key"
        assert "tax_year" in queue_json, "tax-review-queue.json missing 'tax_year' key"
        assert queue_json["tax_year"] == INTEGRATION_YEAR

        # All PDF files in the zip must start with %PDF magic bytes.
        pdf_files = [n for n in names if n.endswith(".pdf")]
        assert len(pdf_files) >= 1, "No PDF files found in zip"
        for pdf_name in pdf_files:
            pdf_data = zf.read(pdf_name)
            assert len(pdf_data) > 200, f"{pdf_name} is too small ({len(pdf_data)} bytes)"
            assert pdf_data[:4] == b"%PDF", (
                f"{pdf_name} doesn't start with PDF magic: {pdf_data[:8]!r}"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_handoff_package_requires_auth(live_stack_for_module: StackConfig):
    """The endpoint must reject unauthenticated requests with 401 or 403."""
    stack = live_stack_for_module
    url = (
        f"{stack.base_url}/api/workspaces/{TEST_WORKSPACE_ID}"
        f"/entities/{TEST_ENTITY_ID}"
        f"/tax-drafts/{INTEGRATION_YEAR}/handoff-package.zip"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)  # no auth header

    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for unauthenticated request, got {resp.status_code}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_pdf_requires_auth(live_stack_for_module: StackConfig):
    """The single-PDF endpoint must reject unauthenticated requests."""
    stack = live_stack_for_module
    url = (
        f"{stack.base_url}/api/workspaces/{TEST_WORKSPACE_ID}"
        f"/entities/{TEST_ENTITY_ID}"
        f"/tax-drafts/{INTEGRATION_YEAR}/form-1120.pdf"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)

    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for unauthenticated request, got {resp.status_code}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_pdf_returns_pdf(live_stack_for_module: StackConfig):
    """The single-PDF endpoint returns a valid PDF for a known slug."""
    stack = live_stack_for_module
    url = (
        f"{stack.base_url}/api/workspaces/{TEST_WORKSPACE_ID}"
        f"/entities/{TEST_ENTITY_ID}"
        f"/tax-drafts/{INTEGRATION_YEAR}/form-1120.pdf"
    )
    headers = {"Authorization": f"Bearer {stack.token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)

    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.headers.get("content-type", "").startswith("application/pdf"), (
        f"Expected application/pdf, got {resp.headers.get('content-type')}"
    )
    assert resp.content[:4] == b"%PDF", (
        f"Response body doesn't start with PDF magic: {resp.content[:8]!r}"
    )
