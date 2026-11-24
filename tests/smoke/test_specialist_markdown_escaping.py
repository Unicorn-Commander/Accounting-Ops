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


@MARKDOWN_SKIP
def test_tag_at_line_start_is_text_not_raw_html_block():
    """A tag at a line start is Python-Markdown's raw-HTML block form.

    It must be escaped to text, not passed through as markup.  This is the
    case a tag-shaped regex would also catch, but the full-input escape is
    what the test pins: the renderer input must carry the tag as visible
    text, and a line-start tag is the most likely shape for a hostile draft.
    """
    html = markdown_to_html("<script>alert('xss')</script>\n\nafter")
    assert "<script>" not in html, "line-start raw-HTML block reached the renderer"
    assert "&lt;script&gt;" in html
    assert "<p>after</p>" in html


@MARKDOWN_SKIP
def test_inline_tag_with_newlines_is_text():
    """A tag broken across lines (raw-HTML block form) is text, not markup."""
    html = markdown_to_html('<a\n  href="file:///etc/passwd"\n  rel="attachment">x</a>')
    assert "<a " not in html and "<a>" not in html, "multiline raw-HTML block reached the renderer"
    assert "file:///etc/passwd" in html, "the attachment URL text was dropped"
    assert "&lt;a" in html


@MARKDOWN_SKIP
def test_tag_inside_fenced_code_is_escaped_not_parsed():
    """A tag inside a fenced code block is escaped once and shown as text.

    The raw-HTML escape skips fenced code (AO107-R4-2): Python-Markdown
    escapes the code content itself, so the tag inside the fence carries a
    single &lt; entity, not the double-escaped &amp;lt; the old
    escape-everything input produced.
    """
    html = markdown_to_html("```html\n<div class=x>code</div>\n```")
    # The codehilite wrapper uses <div class="codehilite">; the inner tag is
    # escaped once by the highlighter and must not be double-escaped.
    assert 'class="codehilite"' in html, "the fenced code block did not render"
    assert "&lt;" in html, "the tag inside the fence was not escaped"
    assert "&amp;lt;" not in html, "the tag inside the fence is double-escaped"
    assert "div" in html, "the tag text was lost"
    assert "code" in html


@MARKDOWN_SKIP
def test_comparison_inequalities_are_text():
    """Lone < and > that are comparison operators are text, not markup."""
    html = markdown_to_html("if a < b and c > d then ok")
    assert "<" not in html or "&lt;" in html
    assert "&lt;" in html and "&gt;" in html
    assert "a" in html and "d" in html


@MARKDOWN_SKIP
def test_escaped_ampersand_is_double_escaped():
    """An & in the body becomes &amp; so it cannot forge an entity.

    A body that writes &lt;script&gt; as literal text must not be re-parsed
    into a raw <script> tag by the renderer.  The escape turns the & into
    &amp;, so the renderer shows the literal text &lt;script&gt; (which,
    after the renderer's own escaping, appears as &amp;lt;script&amp;gt;).
    """
    html = markdown_to_html("the &lt;script&gt; tag is text")
    assert "<script>" not in html, "a forged entity reopened a raw tag"
    assert "&amp;lt;script&amp;gt;" in html, "the literal entity text was lost"


@MARKDOWN_SKIP
def test_blockquote_survives_full_input_escape():
    """A blockquote line keeps its leading > through the escape.

    The full-input escape turns > into &gt;; the blockquote restoration
    un-escapes the leading &gt; of a blockquote line so the quote still
    renders, without reopening any raw-HTML channel.
    """
    html = markdown_to_html("> a quoted note\n\npara")
    assert "<blockquote>" in html, "blockquote lost by the full-input escape"
    assert "a quoted note" in html
    assert "<p>para</p>" in html
    # A line-start tag must still be escaped, not turned into a blockquote.
    html2 = markdown_to_html("<div class=x>\ncontent\n</div>")
    assert "<div" not in html2, "line-start tag reopened by blockquote restore"
    assert "&lt;div" in html2


@MARKDOWN_SKIP
def test_inline_tag_in_table_cell_is_text():
    """A tag inside a table cell is text, not markup."""
    html = markdown_to_html("| a | b |\n|---|---|\n| <img src=x> | 2 |")
    assert "<img" not in html, "raw <img> in a table cell reached the renderer"
    assert "&lt;img" in html
    assert "<td>2</td>" in html, "the table cell text was lost"


# ---------------------------------------------------------------------------
# AO107-R4-1: Markdown link and image URLs are filtered after rendering
# ---------------------------------------------------------------------------


@MARKDOWN_SKIP
def test_inline_javascript_link_href_is_dropped():
    """[js](javascript:alert(1)) must not reach the renderer as a live href.

    Markdown link syntax is not raw HTML, so the input escape cannot filter
    it.  The post-render filter drops the href and keeps the link text.
    Fails on 6ffeb71c, where the raw javascript: href reached the renderer.
    """
    html = markdown_to_html("[js](javascript:alert(1))")
    assert "javascript:" not in html.lower(), "a live javascript: href reached the renderer"
    assert 'href="javascript' not in html, "the unsafe link kept a live href"
    assert ">js</a>" in html, "the link text was dropped"


@MARKDOWN_SKIP
def test_mixed_case_javascript_link_href_is_dropped():
    """[x](JaVaScRiPt:alert(1)) is refused: the scheme match is case-insensitive."""
    html = markdown_to_html("[x](JaVaScRiPt:alert(1))")
    assert "javascript:" not in html.lower(), "a mixed-case javascript: href reached the renderer"
    assert ">x</a>" in html, "the link text was dropped"


@MARKDOWN_SKIP
def test_javascript_with_control_chars_is_dropped():
    """Control characters and whitespace inside a scheme do not bypass the filter."""
    html = markdown_to_html("[js](java\tscript:alert(1))")
    assert "script:alert" not in html.replace("\t", "").lower(), (
        "a control-character javascript: href reached the renderer"
    )


@MARKDOWN_SKIP
def test_reference_style_javascript_link_is_dropped():
    """A reference-style definition [r]: javascript:... cannot make a live href."""
    md = "[r](http://example.com)\n\n[r]: javascript:alert(1)"
    html = markdown_to_html(md)
    assert "javascript:" not in html.lower(), (
        "a reference-style javascript: definition reached the renderer"
    )
    assert "http://example.com" in html, "the safe link target was dropped"


@MARKDOWN_SKIP
def test_file_link_href_is_dropped():
    """[x](file:///etc/passwd) must not reach the renderer as a live href."""
    html = markdown_to_html("[x](file:///etc/passwd)")
    assert "file://" not in html, "a live file: href reached the renderer"
    assert ">x</a>" in html, "the link text was dropped"


@MARKDOWN_SKIP
def test_vbscript_image_src_is_dropped():
    """![v](vbscript:msgbox) must not reach the renderer as a live src."""
    html = markdown_to_html("![v](vbscript:msgbox)")
    assert "vbscript:" not in html.lower(), "a live vbscript: src reached the renderer"
    assert 'alt="v"' in html, "the image alt text was dropped"


@MARKDOWN_SKIP
def test_data_image_src_is_allowed():
    """A data: image URL is the supported embedding path and must survive."""
    html = markdown_to_html("![logo](data:image/png;base64,iVBORw0KGgo=)")
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in html, (
        "the data: image src was dropped"
    )


@MARKDOWN_SKIP
def test_http_image_src_is_dropped():
    """![i](http://example.com/x.png) must not reach the renderer as a live src.

    Images may only come from data: URIs; an http: image would make the
    renderer fetch a URL out of tenant text.
    """
    html = markdown_to_html("![i](http://example.com/x.png)")
    assert "http://example.com/x.png" not in html, (
        "a live http: image src reached the renderer"
    )
    assert 'alt="i"' in html, "the image alt text was dropped"


@MARKDOWN_SKIP
def test_protocol_relative_and_relative_urls_are_dropped():
    """Protocol-relative (//host) and relative links have no allowed scheme."""
    html = markdown_to_html("[rr](//evil.example/x) and [rel](page.html)")
    assert "//evil.example" not in html, "a protocol-relative href reached the renderer"
    assert 'href="page.html"' not in html, "a relative href reached the renderer"
    assert ">rr</a>" in html and ">rel</a>" in html, "the link texts were dropped"


@MARKDOWN_SKIP
def test_http_mailto_and_anchor_links_survive():
    """http, https, mailto and # anchors are the allowed link targets."""
    html = markdown_to_html(
        "[a](http://example.com/x) [b](https://example.com) "
        "[c](mailto:op@example.com) [d](#top)"
    )
    assert 'href="http://example.com/x"' in html, "http link dropped"
    assert 'href="https://example.com"' in html, "https link dropped"
    assert 'href="mailto:op@example.com"' in html, "mailto link dropped"
    assert 'href="#top"' in html, "anchor link dropped"
