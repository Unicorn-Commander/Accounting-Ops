"""AO-PDF-2: specialist markdown escapes raw HTML before the renderer.

The specialist draft body is AI or tenant text.  Python-Markdown passes raw
HTML straight through, so a ``<script>``, ``<img src=file:///...>`` or
``<a rel="attachment" href=file:///...>`` in the body would reach the renderer
as markup (and, before AO-PDF-1, be fetched).  The renderer input must carry
the tag as visible text instead.

``app.pdf.safe_render.markdown_to_html`` escapes raw HTML before Markdown.
These tests assert ``<script>`` and ``<img src=x>`` in the body appear as
text in the HTML handed to the renderer, and that headings, lists, tables and
emphasis still render.  The first two fail on main, where raw HTML passed
through untouched.  Markdown is a main dep (pyproject.toml), so these run in
CI; the skip is kept only for a dev venv without markdown.
"""
from __future__ import annotations

import pytest

from app.api.specialist_pdf import _build_html, _render_markdown_to_html
from app.pdf.safe_render import markdown_to_html


def _markdown_available() -> bool:
    try:
        import markdown  # noqa: F401
    except ImportError:
        return False
    return True


MARKDOWN_SKIP = pytest.mark.skipif(
    not _markdown_available(),
    reason="markdown not installed",
)


@MARKDOWN_SKIP
def test_script_tag_appears_as_text_in_renderer_input():
    """<script> in the body must be text, not markup, in the rendered HTML.

    Drives the specialist route's own markdown helper (the path that feeds the
    renderer).  On main, where raw HTML passed through Python-Markdown
    untouched, the raw <script> tag reached the renderer and this failed.
    """
    html = _render_markdown_to_html("<script>alert('xss')</script>")
    assert "<script>" not in html, "raw <script> tag reached the renderer"
    assert "&lt;script&gt;" in html, "<script> did not appear as visible text"
    assert "alert('xss')" in html, "the script payload text was dropped"
    # The shared helper is the same one the route uses.
    assert html == markdown_to_html("<script>alert('xss')</script>")


@MARKDOWN_SKIP
def test_img_tag_appears_as_text_in_renderer_input():
    """<img src=x> in the body must be text, not markup, in the rendered HTML.

    Drives the specialist route's own markdown helper (the path that feeds the
    renderer).  On main the raw <img> tag reached the renderer and this failed.
    """
    html = _render_markdown_to_html("<img src=x onerror=alert(1)>")
    assert "<img" not in html, "raw <img> tag reached the renderer"
    assert "&lt;img src=x onerror=alert(1)&gt;" in html, (
        "<img> did not appear as visible text"
    )
    # The shared helper is the same one the route uses.
    assert html == markdown_to_html("<img src=x onerror=alert(1)>")


@MARKDOWN_SKIP
def test_attachment_anchor_appears_as_text_in_renderer_input():
    """A <a rel=attachment href=file:..> in the body must be text, not markup."""
    html = markdown_to_html('<a rel="attachment" href="file:///etc/hostname">x</a>')
    assert "<a " not in html and "<a>" not in html, "raw <a> tag reached the renderer"
    assert "file:///etc/hostname" in html, "the attachment URL text was dropped"
    assert "&lt;a" in html, "<a> did not appear as visible text"


@MARKDOWN_SKIP
def test_headings_lists_tables_emphasis_survive():
    """The Markdown constructs the draft relies on still render."""
    md = (
        "# Heading one\n"
        "\n"
        "**bold** and *emphasised* and `inline code`\n"
        "\n"
        "- first\n"
        "- second\n"
        "\n"
        "An ordered list follows.\n"
        "\n"
        "1. one\n"
        "2. two\n"
        "\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "\n"
        "> a quoted note\n"
    )
    html = markdown_to_html(md)
    assert "<h1" in html, "heading lost"
    assert "<strong>bold</strong>" in html, "emphasis lost"
    assert "<em>emphasised</em>" in html, "emphasis lost"
    assert "<code>inline code</code>" in html, "inline code lost"
    assert "<ul>" in html and "<li>first</li>" in html, "list lost"
    assert "<ol>" in html and "<li>one</li>" in html, "ordered list lost"
    assert "<table>" in html and "<td>1</td>" in html, "table lost"
    assert "<blockquote>" in html and "a quoted note" in html, "quote lost"


@MARKDOWN_SKIP
def test_full_build_html_escapes_raw_html():
    """_build_html (the path to the renderer) carries the tag as text."""
    html = _build_html(
        body_md="<script>alert('xss')</script>\n\n# Draft\n",
        entity_name="Test Corp",
        tax_year=2024,
        doc_title="Test Draft",
    )
    assert "<script>" not in html, "raw <script> reached the full page HTML"
    assert "&lt;script&gt;" in html
    assert "<h1" in html, "the markdown heading was lost"
    # The DRAFT watermark and disclaimer are still present.
    assert "CPA REVIEW REQUIRED" in html
    assert "rotate(-30deg)" in html


@MARKDOWN_SKIP
def test_render_markdown_to_html_matches_safe_render():
    """The specialist route's markdown helper is the shared safe one."""
    md = "before\n\n<script>x</script>\n\nafter"
    assert _render_markdown_to_html(md) == markdown_to_html(md)
