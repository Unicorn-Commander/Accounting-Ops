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

import re

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

    Markdown escapes code itself (AO107-R5-1: no pre-escape), so the tag
    inside the fence carries a single &lt; entity, never &amp;lt;.
    """
    html = markdown_to_html("```html\n<div class=x>code</div>\n```")
    assert re.search(r"<pre><code[^>]*>&lt;div class=x&gt;code&lt;/div&gt;", html), (
        "the fenced tag is not shown single-escaped inside <pre><code>"
    )
    assert "&amp;lt;" not in html, "the tag inside the fence is double-escaped"
    assert "<div" not in html


@MARKDOWN_SKIP
def test_tag_inside_code_span_is_escaped_once():
    """A tag inside a code span is escaped once (AO107-R4-2 stays green)."""
    html = markdown_to_html("see `<b>&</b>` here")
    assert "<code>&lt;b&gt;&amp;&lt;/b&gt;</code>" in html
    assert "&amp;lt;" not in html, "the code span is double-escaped"


@MARKDOWN_SKIP
def test_comparison_inequalities_are_text():
    """Lone < and > that are comparison operators are text, not markup."""
    html = markdown_to_html("if a < b and c > d then ok")
    assert "<" not in html or "&lt;" in html
    assert "&lt;" in html and "&gt;" in html
    assert "a" in html and "d" in html


@MARKDOWN_SKIP
def test_entity_text_cannot_forge_a_tag():
    """A body that writes &lt;script&gt; shows the text <script>, never a tag.

    Markdown keeps a valid entity as an entity, so the renderer displays the
    characters; the entity is never decoded into markup.
    """
    html = markdown_to_html("the &lt;script&gt; tag is text")
    assert "<script>" not in html, "a forged entity reopened a raw tag"
    assert "&lt;script&gt;" in html, "the literal entity text was lost"


@MARKDOWN_SKIP
def test_blockquote_survives_full_input_escape():
    """A blockquote line keeps its leading > and a line-start tag is text."""
    html = markdown_to_html("> a quoted note\n\npara")
    assert "<blockquote>" in html, "blockquote lost by the full-input escape"
    assert "a quoted note" in html
    assert "<p>para</p>" in html
    # A line-start tag must still be escaped, not turned into a blockquote.
    html2 = markdown_to_html("<div class=x>\ncontent\n</div>")
    assert "<div" not in html2, "line-start tag reached the renderer"
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


# ---------------------------------------------------------------------------
# AO107-R5-1: Markdown escapes raw HTML itself; no regex code stash
# ---------------------------------------------------------------------------
# On a6e90459 a regex treated \`...\` (backslash-escaped backticks) as a code
# span and skipped the escape, but Markdown does not treat it as code, so the
# tag inside came out live.  Each case below failed there.

_ESCAPED_BACKTICK_REPRODUCERS = [
    "\\`<script>alert(1)</script>\\`",
    "\\`<link rel=stylesheet href=http://127.0.0.1:1/x.css>\\`",
    "\\`<style>@import url(file:///etc/passwd)</style>\\`",
    "\\`<object data=file:///etc/passwd>\\`",
    '\\`<img src="file:///etc/passwd">\\`',
    "x \\`<img src=file:///etc/passwd>\\` y",
]


@MARKDOWN_SKIP
@pytest.mark.parametrize("body", _ESCAPED_BACKTICK_REPRODUCERS)
def test_escaped_backticks_do_not_unescape_a_tag(body):
    """A tag between backslash-escaped backticks is text, not markup."""
    html = markdown_to_html(body)
    for tag in ("<script", "<link", "<style", "<object", "<img"):
        assert tag not in html, f"{tag} reached the renderer from {body!r}"
    assert "&lt;" in html, "the tag was not shown as text"


@MARKDOWN_SKIP
@pytest.mark.parametrize(
    "body",
    [
        "`unbalanced <script>alert(1)</script>",
        "``<img src=file:///etc/passwd>` mismatched",
        "a ` b <object data=file:///x> c",
    ],
)
def test_unbalanced_backticks_do_not_unescape_a_tag(body):
    """Unbalanced or mismatched backticks never let a tag through."""
    html = markdown_to_html(body)
    for tag in ("<script", "<object", "<img"):
        assert tag not in html, f"{tag} reached the renderer from {body!r}"


@MARKDOWN_SKIP
def test_nul_and_c0_controls_are_stripped():
    """NUL and C0 controls are removed before Markdown sees the body.

    A NUL inside a tag name cannot split the tag past the escape, and the
    STX/ETX characters Markdown uses as placeholders cannot be forged.
    """
    html = markdown_to_html("a\x00<scr\x00ipt>x</script>\x02\x03b\x1f")
    assert "\x00" not in html and "\x02" not in html and "\x1f" not in html
    assert "<script" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# AO107-R5-2: a real parse of the generated HTML, tag and attribute allowlist
# ---------------------------------------------------------------------------
# On a6e90459 the post-filter was a regex with a 16-character lookback, so a
# long attribute list hid the <a, an entity-encoded scheme was kept because
# the value carried '&', and every other attribute passed through.


@MARKDOWN_SKIP
def test_long_attachment_anchor_href_is_dropped():
    """<a rel=attachment ...long attrs... href=file:> never keeps its href."""
    from app.pdf.safe_render import _sanitize_html

    hostile = (
        '<a rel="attachment" title="t" class="cccccccccccccccccccccccc" '
        'data-x="yyyyyyyyyyyyyyyyyyyy" href="file:///etc/passwd">x</a>'
    )
    out = _sanitize_html(hostile)
    assert "file:" not in out and "attachment" not in out
    assert out == '<a title="t">x</a>'
    # The same tag in a draft body is text, never markup.
    html = markdown_to_html(hostile)
    assert "<a " not in html and "&lt;a rel" in html


@MARKDOWN_SKIP
@pytest.mark.parametrize(
    "body",
    [
        "[x](jav&#x61;script:alert(1))",
        "[x](&#106;avascript:alert(1))",
        "[x](javascript&#58;alert(1))",
        "[x](file&#x3a;///etc/passwd)",
    ],
)
def test_entity_encoded_scheme_is_dropped(body):
    """The scheme is checked after HTML-unescaping, so entities do not hide it."""
    html = markdown_to_html(body)
    assert "href" not in html, f"an entity-encoded scheme kept a live href: {html}"
    assert ">x</a>" in html


@MARKDOWN_SKIP
def test_entity_encoded_image_scheme_is_dropped():
    """An entity-encoded file: image src is dropped, the alt text stays."""
    html = markdown_to_html("![i](fil&#x65;:///etc/passwd)")
    assert "src" not in html and 'alt="i"' in html


@MARKDOWN_SKIP
@pytest.mark.parametrize(
    "body",
    ["[x](java\tscript:alert(1))", "[x](java\nscript:alert(1))", "[x]( \x0bjavascript:alert(1))"],
)
def test_whitespace_inside_scheme_is_dropped(body):
    """Tab, newline or control characters inside the scheme do not bypass it."""
    html = markdown_to_html(body)
    assert "href" not in html, f"a split javascript: scheme kept a live href: {html}"


@MARKDOWN_SKIP
def test_javascript_autolink_is_not_a_link():
    """<javascript:alert(1)> is not a Markdown autolink and stays text."""
    html = markdown_to_html("<javascript:alert(1)>")
    assert "<a" not in html and "href" not in html
    assert "&lt;javascript:alert(1)&gt;" in html


@MARKDOWN_SKIP
def test_http_autolink_survives():
    """<http://example.com/a> is an allowed autolink."""
    html = markdown_to_html("<http://example.com/a>")
    assert '<a href="http://example.com/a">http://example.com/a</a>' in html


@MARKDOWN_SKIP
def test_disallowed_tags_and_attributes_are_dropped():
    """Only allowlisted tags and a[href|title], img[src|alt] survive the parse."""
    from app.pdf.safe_render import _sanitize_html

    hostile = (
        '<p style="background:url(file:///x)" onclick="x()">t</p>'
        '<img src="data:image/png;base64,AAA=" srcset="file:///a 1x" '
        'onerror="x()" alt="a" style="x">'
        '<video poster="file:///p"></video><object data="file:///o"></object>'
        '<link rel="stylesheet" href="http://127.0.0.1:1/x.css">'
        '<style>@import url(file:///etc/passwd)</style>'
        '<td style="text-align: left;">c</td><!-- c --><span class="k">s</span>'
    )
    out = _sanitize_html(hostile)
    for bad in ("style", "onclick", "onerror", "srcset", "poster", "data=",
                "<video", "<object", "<link", "<span", "<!--", "class"):
        assert bad not in out, f"{bad} survived the allowlist: {out}"
    assert '<img src="data:image/png;base64,AAA=" alt="a">' in out
    assert "<p>t</p>" in out and "<td>c</td>" in out
    # A dropped <style> element's text is escaped text, never a stylesheet.
    assert "@import url(file:///etc/passwd)" in out


@MARKDOWN_SKIP
def test_table_alignment_style_is_dropped():
    """The tables extension's style="text-align" is dropped; the cells stay."""
    html = markdown_to_html("| a | b |\n|:--|--:|\n| 1 | 2 |")
    assert "style" not in html
    assert "<th>a</th>" in html and "<td>2</td>" in html


@MARKDOWN_SKIP
def test_non_image_data_uri_src_is_dropped():
    """img src allows data:image/* only; data:text/html is dropped."""
    html = markdown_to_html("![t](data:text/html;base64,PHNjcmlwdD4=)")
    assert "src" not in html and 'alt="t"' in html


@MARKDOWN_SKIP
def test_link_title_is_kept_and_escaped():
    """a[title] is kept with its value re-escaped."""
    html = markdown_to_html('[t](https://example.com "say \\"hi\\" <b>")')
    assert 'href="https://example.com"' in html
    assert "<b>" not in html
