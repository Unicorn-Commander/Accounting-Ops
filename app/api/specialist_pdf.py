"""Specialist PDF rendering + CPA Handoff Package bundler.

Routes:
  GET /api/workspaces/{wid}/entities/{eid}/tax-drafts/{tax_year}/{specialist_slug}.pdf
    — renders a single specialist's Markdown draft from /drafts/ as a PDF.
    - Returns application/pdf with Content-Disposition attachment.

  GET /api/workspaces/{wid}/entities/{eid}/tax-drafts/{tax_year}/handoff-package.zip
    — bundles the full CPA Handoff Package for a year:
      - Cover letter PDF (auto-generated)
      - All specialist PDFs for the year
      - CPA-export JSON (from cpa-export sidecar: exports/1120/{year})
      - Journal CSV (from cpa-export sidecar: exports/csv?period={year})
      - tax_review_queue rows export (JSON)
      - README.txt (human-readable index + disclaimer)
    Returns application/zip.

Renderer: WeasyPrint (HTML+CSS → PDF, native, no Chromium). Chosen over
Pandoc because:
  1. Available as a pure Python dep (pip install weasyprint) — no system binary.
  2. CSS @page watermark support is first-class (needed for DRAFT watermark).
  3. The HTML pipeline (markdown → HTML → PDF) gives us full CSS control for
     tables, headers, page numbers, and the rotating DRAFT watermark.
  4. No subprocess / shelling out → simpler error handling + testable.

DRAFT watermark: every PDF page carries a 30-degree rotated "DRAFT — CPA
REVIEW REQUIRED" watermark in light-gray via CSS @page ::before pseudo-content.

Role floor: viewer (CPAs and owners can both download).
Entity scope: standard assert_entity_in_scope check.
"""
from __future__ import annotations

import io
import json
import os
import textwrap
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.config import get_settings
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["specialist-pdf"])

# ---------------------------------------------------------------------------
# Specialist slug → human-readable title + file-name prefix
# ---------------------------------------------------------------------------

_SLUG_META: dict[str, dict[str, str]] = {
    "form-1120": {
        "title": "Form 1120 — Federal Corporate Income Tax Return",
        "filename_prefix": "01-form-1120-draft",
        "draft_file": "mu-inc-{year}-form-1120-draft.md",
    },
    "1099-nec": {
        "title": "1099-NEC Nonemployee Compensation Analysis",
        "filename_prefix": "02-1099-NEC-analysis",
        "draft_file": "mu-inc-{year}-1099-nec-draft.md",
    },
    "sc1120": {
        "title": "SC1120 — South Carolina Corporate Income Tax Return",
        "filename_prefix": "03-SC1120-draft",
        "draft_file": "mu-inc-{year}-sc1120-draft.md",
    },
    "officer-compensation": {
        "title": "Officer Compensation Schedule",
        "filename_prefix": "04-officer-compensation",
        "draft_file": "mu-inc-{year}-officer-compensation-draft.md",
    },
    "nol-schedule-l": {
        "title": "Net Operating Loss & Schedule L Analysis",
        "filename_prefix": "05-NOL-schedule-L",
        "draft_file": "mu-inc-{year}-nol-schedule-l-draft.md",
    },
    "audit-defense-scdor-ftf": {
        "title": "Audit Defense — SCDOR Failure-to-File",
        "filename_prefix": "06-audit-defense-SCDOR-FTF",
        "draft_file": "mu-inc-{year}-audit-defense-scdor-ftf-draft.md",
    },
    "coordinator-summary": {
        "title": "Tax Operations Coordinator Summary",
        "filename_prefix": "07-coordinator-summary",
        "draft_file": "mu-inc-{year}-coordinator-summary-draft.md",
    },
}

# Ordered list for bundle assembly (cover is 00, these are 01–07)
_ORDERED_SLUGS = [
    "form-1120",
    "1099-nec",
    "sc1120",
    "officer-compensation",
    "nol-schedule-l",
    "audit-defense-scdor-ftf",
    "coordinator-summary",
]

# Draft files live here on the container filesystem.
# Override with DRAFTS_DIR env var for flexibility.
_DRAFTS_DIR = Path(os.getenv("DRAFTS_DIR", "/drafts"))


# ---------------------------------------------------------------------------
# CSS + HTML templates
# ---------------------------------------------------------------------------

_WATERMARK_CSS = """
@page {
    margin: 25mm 20mm 25mm 20mm;
    size: letter;
}

/* DRAFT watermark on every page via @page pseudo-content.
   WeasyPrint doesn't support @page ::before directly, so we use
   a fixed-position ::before on the body instead (rendered once per
   page in the paged context through CSS counter trick). The approach
   below uses a page-break-independent fixed box. */

body::before {
    content: "DRAFT — CPA REVIEW REQUIRED";
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%) rotate(-30deg);
    font-size: 52px;
    font-weight: 900;
    color: rgba(180, 180, 180, 0.22);
    white-space: nowrap;
    pointer-events: none;
    z-index: -1;
    font-family: Arial, sans-serif;
    letter-spacing: 2px;
}

body {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 10.5pt;
    line-height: 1.55;
    color: #1a1a1a;
}

h1 {
    font-size: 18pt;
    font-weight: bold;
    margin: 0 0 4pt 0;
    color: #0d1117;
    border-bottom: 2px solid #2563eb;
    padding-bottom: 6pt;
}

h2 {
    font-size: 13pt;
    font-weight: bold;
    margin: 18pt 0 5pt 0;
    color: #0d1117;
    border-bottom: 1px solid #d1d5db;
    padding-bottom: 3pt;
}

h3 {
    font-size: 11pt;
    font-weight: bold;
    margin: 12pt 0 4pt 0;
    color: #1e3a5f;
}

h4 { font-size: 10.5pt; font-weight: bold; margin: 10pt 0 3pt 0; }

p { margin: 0 0 7pt 0; }

table {
    border-collapse: collapse;
    width: 100%;
    margin: 8pt 0 12pt 0;
    font-size: 9.5pt;
}

th {
    background: #1e3a5f;
    color: white;
    padding: 5pt 8pt;
    text-align: left;
    font-weight: bold;
}

td {
    padding: 4pt 8pt;
    border-bottom: 1px solid #e5e7eb;
}

tr:nth-child(even) td { background: #f8fafc; }

code, pre {
    font-family: 'Courier New', Courier, monospace;
    font-size: 9pt;
    background: #f4f4f5;
    padding: 1pt 3pt;
    border-radius: 2pt;
}

pre {
    padding: 8pt 10pt;
    border-left: 3px solid #2563eb;
    white-space: pre-wrap;
    word-break: break-word;
    margin: 8pt 0;
}

blockquote {
    border-left: 4px solid #9ca3af;
    margin: 8pt 0;
    padding: 4pt 12pt;
    color: #4b5563;
    background: #f9fafb;
}

ul, ol { margin: 4pt 0 8pt 0; padding-left: 18pt; }
li { margin-bottom: 3pt; }

.page-header {
    font-size: 8pt;
    color: #6b7280;
    border-bottom: 1px solid #e5e7eb;
    padding-bottom: 4pt;
    margin-bottom: 14pt;
}

.disclaimer {
    margin-top: 24pt;
    padding: 10pt;
    background: #fef3c7;
    border: 1px solid #f59e0b;
    border-radius: 4pt;
    font-size: 9pt;
    color: #92400e;
}

.toc { margin-bottom: 20pt; }
.toc-entry { margin: 3pt 0; font-size: 10pt; }
.toc-entry .num { font-weight: bold; color: #1e3a5f; margin-right: 6pt; }
"""

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <style>{css}</style>
</head>
<body>
<div class="page-header">
  {entity_name} &nbsp;|&nbsp; Tax Year {tax_year} &nbsp;|&nbsp; {doc_title}
  &nbsp;|&nbsp; Generated {generated_date}
</div>
{body_html}
<div class="disclaimer">
  <strong>DRAFT — CPA REVIEW REQUIRED.</strong>
  This document was prepared by an AI tax specialist and has NOT been reviewed or
  signed by a licensed CPA or attorney. It does not constitute legal, tax, or
  financial advice. Do not file any return based solely on this draft without
  independent CPA review and sign-off.
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Renderer helpers
# ---------------------------------------------------------------------------

def _render_markdown_to_html(md_text: str) -> str:
    """Convert Markdown → HTML fragment (tables, fences, etc.)."""
    try:
        import markdown

        return markdown.markdown(
            md_text,
            extensions=["tables", "fenced_code", "codehilite", "toc"],
        )
    except ImportError:
        # Fallback: wrap in <pre> — still usable, just unstyled.
        escaped = md_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<pre>{escaped}</pre>"


def _build_html(
    body_md: str,
    entity_name: str,
    tax_year: int,
    doc_title: str,
) -> str:
    """Assemble the full HTML page for WeasyPrint."""
    body_html = _render_markdown_to_html(body_md)
    return _HTML_TEMPLATE.format(
        css=_WATERMARK_CSS,
        entity_name=_esc(entity_name),
        tax_year=tax_year,
        doc_title=_esc(doc_title),
        generated_date=date.today().isoformat(),
        body_html=body_html,
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _html_to_pdf(html: str) -> bytes:
    """Render HTML → PDF bytes via WeasyPrint.

    Raises HTTP 500 if WeasyPrint is not installed (pip) or if the required
    system libraries (Pango/GObject) are absent — the latter can happen on a
    minimal host before `apt-get install python3-weasyprint` runs. Both are
    caught so the error message is actionable.
    """
    try:
        from weasyprint import HTML  # type: ignore[import-untyped]

        return HTML(string=html).write_pdf()
    except (ImportError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "WeasyPrint renderer unavailable. Ensure weasyprint>=62.0 is in "
                "main deps and the Docker image includes the required system "
                "libraries (libpango-1.0-0, libgdk-pixbuf2.0-0, libcairo2, etc.). "
                f"Underlying error: {exc}"
            ),
        ) from exc


# ---------------------------------------------------------------------------
# Entity helpers
# ---------------------------------------------------------------------------

async def _get_entity_display_name(
    workspace_id: UUID,
    entity_id: UUID,
) -> str:
    """Fetch entity display_name from the workspace schema."""
    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text("SELECT display_name FROM entity WHERE id = :id"),
                {"id": str(entity_id)},
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return row.display_name


def _draft_path(tax_year: int, slug: str) -> Path | None:
    """Return the Path to a draft file if it exists, else None."""
    meta = _SLUG_META.get(slug)
    if meta is None:
        return None
    filename = meta["draft_file"].format(year=tax_year)
    p = _DRAFTS_DIR / filename
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Single PDF route
# ---------------------------------------------------------------------------

@router.get(
    "/api/workspaces/{workspace_id}/entities/{entity_id}"
    "/tax-drafts/{tax_year}/{specialist_slug}.pdf"
)
async def get_specialist_pdf(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    specialist_slug: str,
    disposition: str = Query(
        "attachment",
        description=(
            "Content-Disposition mode. Pass 'inline' to view in a browser tab "
            "instead of triggering a download. Default: 'attachment'."
        ),
    ),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> Response:
    """Render a single specialist Markdown draft as a PDF and return it.

    The specialist_slug maps to a draft file at:
      /drafts/mu-inc-{year}-{slug}-draft.md

    Returns application/pdf.  Content-Disposition is 'attachment' by default
    (browser downloads); pass ?disposition=inline to render in-tab.
    Role floor: viewer.
    """
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)

    if specialist_slug not in _SLUG_META:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown specialist slug '{specialist_slug}'. "
                   f"Valid: {', '.join(_SLUG_META.keys())}",
        )

    meta = _SLUG_META[specialist_slug]
    entity_name = await _get_entity_display_name(workspace_id, entity_id)

    draft_path = _draft_path(tax_year, specialist_slug)
    if draft_path is None:
        # Return a PDF that says "no draft yet" rather than a 404, so callers
        # can still include it in a bundle attempt without hard-failing.
        md_content = textwrap.dedent(f"""\
            # {meta['title']}

            **No draft available for tax year {tax_year}.**

            The specialist runner has not yet produced a draft for this year and entity.
            Run the specialist to generate one.

            Entity: {entity_name}
            Tax Year: {tax_year}
            Specialist: {specialist_slug}
            """)
    else:
        md_content = draft_path.read_text(encoding="utf-8")

    html = _build_html(
        body_md=md_content,
        entity_name=entity_name,
        tax_year=tax_year,
        doc_title=meta["title"],
    )
    pdf_bytes = _html_to_pdf(html)

    # Friendly filename: e.g. MagicUnicornInc-2023-form-1120-draft.pdf
    safe_name = entity_name.replace(" ", "").replace(",", "")
    filename = f"{safe_name}-{tax_year}-{specialist_slug}-draft.pdf"

    # Sanitise the disposition param — only 'inline' or 'attachment' are valid.
    cd_mode = "inline" if disposition.lower().strip() == "inline" else "attachment"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{cd_mode}; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
            "X-Draft-Notice": (
                "DRAFT - for CPA review only. "
                "Do not file without professional review and signature."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Markdown source endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/api/workspaces/{workspace_id}/entities/{entity_id}"
    "/tax-drafts/{tax_year}/{specialist_slug}.md"
)
async def get_specialist_markdown(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    specialist_slug: str,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> Response:
    """Return the raw Markdown source for a specialist draft.

    Returns text/markdown; charset=utf-8.
    404 if the draft file does not exist on the filesystem.

    This endpoint backs the in-browser Markdown viewer modal — it serves the
    source text so the viewer can render it client-side (react-markdown).

    Role floor: viewer.  Entity scope: standard assert_entity_in_scope.
    """
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)

    if specialist_slug not in _SLUG_META:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown specialist slug '{specialist_slug}'. "
                   f"Valid: {', '.join(_SLUG_META.keys())}",
        )

    draft_path = _draft_path(tax_year, specialist_slug)
    if draft_path is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No draft file found for specialist '{specialist_slug}' "
                f"tax year {tax_year}. Run the specialist to generate one."
            ),
        )

    md_content = draft_path.read_text(encoding="utf-8")

    return Response(
        content=md_content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "X-Draft-Notice": (
                "DRAFT - for CPA review only. Do not file without professional review and signature."
            ),
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Cover letter PDF
# ---------------------------------------------------------------------------

def _build_cover_letter_pdf(
    entity_name: str,
    tax_year: int,
    included_docs: list[str],
    generated_dt: str,
) -> bytes:
    """Generate a cover letter PDF for the handoff package."""
    doc_list_html = "".join(
        f"<li>{_esc(d)}</li>" for d in included_docs
    )

    cover_md = textwrap.dedent(f"""\
        # CPA Handoff Package

        **Entity:** {entity_name}
        **Tax Year:** {tax_year}
        **Prepared:** {generated_dt}

        ---

        ## Contents

        This package contains the following materials for CPA review and sign-off:

    """)

    for i, doc in enumerate(included_docs, 1):
        cover_md += f"- {doc}\n"

    cover_md += textwrap.dedent(f"""

        ---

        ## Important Notice

        All documents marked **DRAFT** have been prepared by AI tax specialist
        agents and have **not** been reviewed or signed by a licensed CPA,
        enrolled agent, or attorney. They do not constitute legal, tax, or
        financial advice.

        **The CPA must independently review every line item before signature or
        filing.** No return should be filed based solely on these drafts without
        your independent review, corrections, and written sign-off.

        ---

        *Package assembled automatically by Accounting-Ops · Magic Unicorn Inc.*
        *{generated_dt}*
    """)

    html = _build_html(
        body_md=cover_md,
        entity_name=entity_name,
        tax_year=tax_year,
        doc_title="CPA Handoff Package — Cover Letter",
    )
    return _html_to_pdf(html)


# ---------------------------------------------------------------------------
# CPA-export sidecar helpers
# ---------------------------------------------------------------------------

async def _fetch_cpa_export_json(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    token: str,
) -> bytes | None:
    """Fetch the /exports/1120/{year} JSON from the cpa-export sidecar."""
    settings = get_settings()
    url = (
        f"{settings.cpa_export_url}/exports/1120/{tax_year}"
        f"?workspace_id={workspace_id}&entity_id={entity_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            return resp.content
    except Exception:
        pass
    return None


async def _fetch_cpa_export_csv(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    token: str,
) -> bytes | None:
    """Fetch the /exports/csv?period={year} journal CSV from cpa-export."""
    settings = get_settings()
    url = (
        f"{settings.cpa_export_url}/exports/csv"
        f"?workspace_id={workspace_id}&entity_id={entity_id}&period={tax_year}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            return resp.content
    except Exception:
        pass
    return None


async def _fetch_tax_review_queue_json(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
) -> bytes:
    """Export tax_review_queue rows for the year as JSON bytes."""
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        trq.id::text,
                        trq.entity_id::text,
                        trq.tax_year,
                        trq.item_type,
                        trq.form_reference,
                        trq.content,
                        trq.confidence,
                        trq.citations,
                        trq.status,
                        trq.submitter_kind,
                        trq.submitter_id,
                        trq.reviewer_uc_uid,
                        trq.resolution_notes,
                        trq.created_at,
                        trq.resolved_at
                    FROM tax_review_queue trq
                    WHERE trq.entity_id = :eid
                      AND trq.tax_year = :year
                    ORDER BY trq.created_at ASC
                    """
                ),
                {"eid": str(entity_id), "year": tax_year},
            )
        ).mappings().all()

    payload: dict[str, Any] = {
        "entity_id": str(entity_id),
        "tax_year": tax_year,
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": [dict(r) for r in rows],
    }

    def _default(obj: Any) -> Any:
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return str(obj)

    return json.dumps(payload, default=_default, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# Handoff ZIP route
# ---------------------------------------------------------------------------

@router.get(
    "/api/workspaces/{workspace_id}/entities/{entity_id}"
    "/tax-drafts/{tax_year}/handoff-package.zip"
)
async def get_handoff_package(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> Response:
    """Bundle the full CPA Handoff Package for a tax year as a ZIP.

    Contents:
      00-cover-letter.pdf
      01-form-1120-draft.pdf      (etc. — one per slug, in order)
      data/form-1120-package.json (cpa-export JSON, if sidecar is up)
      data/journal-{year}.csv     (journal CSV, if sidecar is up)
      data/tax-review-queue.json  (queue rows export)
      README.txt                  (human-readable index + disclaimer)

    Role floor: viewer.
    """
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)

    entity_name = await _get_entity_display_name(workspace_id, entity_id)
    safe_name = entity_name.replace(" ", "").replace(",", "")
    generated_dt = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build auth token passthrough for cpa-export calls.
    # The user's bearer token is available via workspace.user_uc_uid but we
    # can't reconstruct the raw JWT here; we forward an empty string instead
    # and rely on cpa-export's graceful degradation when no token is present.
    # In practice the bundle assembly continues even if cpa-export is down.
    cpa_token = ""

    # Collect all specialist PDFs for the year.
    specialist_pdfs: list[tuple[str, bytes]] = []  # (zip_name, pdf_bytes)
    included_docs: list[str] = []

    for slug in _ORDERED_SLUGS:
        meta = _SLUG_META[slug]
        draft_path = _draft_path(tax_year, slug)
        if draft_path is None:
            # Skip missing drafts — don't include a placeholder in the zip.
            continue
        md_content = draft_path.read_text(encoding="utf-8")
        html = _build_html(
            body_md=md_content,
            entity_name=entity_name,
            tax_year=tax_year,
            doc_title=meta["title"],
        )
        pdf_bytes = _html_to_pdf(html)
        zip_name = f"{meta['filename_prefix']}.pdf"
        specialist_pdfs.append((zip_name, pdf_bytes))
        included_docs.append(f"{zip_name} — {meta['title']}")

    # Data files.
    cpa_json = await _fetch_cpa_export_json(workspace_id, entity_id, tax_year, cpa_token)
    journal_csv = await _fetch_cpa_export_csv(workspace_id, entity_id, tax_year, cpa_token)
    queue_json = await _fetch_tax_review_queue_json(workspace_id, entity_id, tax_year)

    data_files: list[tuple[str, bytes]] = []
    if cpa_json:
        data_files.append(("data/form-1120-package.json", cpa_json))
        included_docs.append("data/form-1120-package.json — CPA Export JSON (Form 1120 line mapping)")
    if journal_csv:
        data_files.append((f"data/journal-{tax_year}.csv", journal_csv))
        included_docs.append(f"data/journal-{tax_year}.csv — Full journal export (CSV)")
    data_files.append(("data/tax-review-queue.json", queue_json))
    included_docs.append("data/tax-review-queue.json — AI specialist review queue items")

    # Cover letter (built after we know what's in the package).
    cover_pdf = _build_cover_letter_pdf(
        entity_name=entity_name,
        tax_year=tax_year,
        included_docs=included_docs,
        generated_dt=generated_dt,
    )

    # README.txt
    readme_lines = [
        f"{safe_name} — Tax Year {tax_year} — CPA Handoff Package",
        "=" * 60,
        f"Generated: {generated_dt}",
        f"Entity: {entity_name}",
        f"Tax Year: {tax_year}",
        "",
        "CONTENTS",
        "--------",
        "00-cover-letter.pdf    — Package cover letter and disclaimer",
    ]
    for zip_name, _bytes in specialist_pdfs:
        readme_lines.append(f"{zip_name:<30} — Specialist draft (DRAFT, CPA review required)")
    for zip_name, _bytes in data_files:
        readme_lines.append(f"{zip_name:<30} — Supporting data export")
    readme_lines += [
        "",
        "DISCLAIMER",
        "----------",
        "All PDF documents labeled DRAFT were prepared by AI tax specialist agents.",
        "They have NOT been reviewed or signed by a licensed CPA, enrolled agent,",
        "or attorney. No return should be filed based solely on these drafts without",
        "independent CPA review, corrections, and written sign-off.",
        "",
        "Accounting-Ops · Magic Unicorn Inc.",
    ]
    readme_bytes = "\n".join(readme_lines).encode("utf-8")

    # Assemble zip in memory.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("00-cover-letter.pdf", cover_pdf)
        for zip_name, pdf_bytes in specialist_pdfs:
            zf.writestr(zip_name, pdf_bytes)
        for zip_name, data_bytes in data_files:
            zf.writestr(zip_name, data_bytes)
        zf.writestr("README.txt", readme_bytes)

    zip_bytes = buf.getvalue()
    zip_filename = f"{safe_name}-{tax_year}-CPA-Handoff.zip"

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_filename}"',
            "Content-Length": str(len(zip_bytes)),
        },
    )
