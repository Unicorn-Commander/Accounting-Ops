"""AO-PDF-1: the shared PDF renderer fetches nothing.

WeasyPrint's default URL fetcher follows ``file:`` and ``http(s):`` URLs found
in the document: it reads the file or makes the network request and inlines
the bytes into the PDF (an image via ``<img>`` or a style ``url()``, a file
into the PDF via ``<a rel="attachment">``).  The specialist draft body and
the invoice HTML both come from tenant or AI text, so a draft that references
``file:///`` (env, keys) or an internal host could read that into the PDF the
accountant downloads.

The renderer must refuse every scheme except ``data:``.  These tests drive
``render_pdf`` against a hostile document and assert:

* the fetcher is called for each hostile URL and refuses it (no file read,
  no network call), and
* the PDF bytes do not contain the referenced file's content, and
* ``data:`` URIs (the invoice logo path) still render.

The fetcher is spied at class level on ``SafeUrlFetcher.fetch`` so the test
exercises ``render_pdf``'s own fetcher, not a separately constructed one.
``OpenerDirector.open`` is patched to fail on any network attempt (WeasyPrint
68 calls ``super().open()`` for every fetch).  WeasyPrint is a main dep
(pyproject.toml), so these run in CI; the skip is kept only for a dev venv
without the system Pango libraries.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.pdf.safe_render import data_uri_for_bytes, render_pdf, safe_url_fetcher


def _weasyprint_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


WEASYPRINT_SKIP = pytest.mark.skipif(
    not _weasyprint_available(),
    reason="weasyprint or its Pango system libraries are not installed",
)


def _spy_fetcher():
    """Spy on ``SafeUrlFetcher.fetch`` at class level so ``render_pdf``'s
    own fetcher is the object under test (AO107-R4-3).

    ``render_pdf`` builds a fresh ``SafeUrlFetcher`` on every call, so an
    instance-level spy on a separate fetcher would not see the URLs the
    renderer actually resolves.  Spying the class method records every URL
    the renderer's fetcher sees and delegates to the real implementation.
    """
    import app.pdf.safe_render as safe_render_module

    calls: list[str] = []
    original = safe_render_module.SafeUrlFetcher.fetch

    def spy(self, url, headers=None):
        calls.append(url)
        return original(self, url, headers)

    safe_render_module.SafeUrlFetcher.fetch = spy
    return calls, lambda: setattr(safe_render_module.SafeUrlFetcher, "fetch", original)


def _no_network():
    """Patch ``OpenerDirector.open`` so any network attempt fails the test.

    WeasyPrint 68's ``URLFetcher`` inherits from ``urllib.request
    .OpenerDirector`` and calls ``super().open()`` for every fetch, so
    patching ``urllib.request.urlopen`` alone would not catch a network
    call.  Patching ``OpenerDirector.open`` catches both.
    """
    from urllib.request import OpenerDirector

    def _explode(self, *args, **kwargs):
        raise AssertionError("renderer attempted a network call")

    original = OpenerDirector.open
    OpenerDirector.open = _explode
    return original


def _restore_network(original):
    from urllib.request import OpenerDirector

    OpenerDirector.open = original


def _render(html: str, fetcher=None) -> bytes:
    """Render through ``render_pdf`` itself, not a direct ``HTML`` call.

    AO107-R4-3: the spied tests must exercise ``render_pdf``'s wiring, not a
    separately constructed ``HTML`` object with a hand-made fetcher.
    """
    from app.pdf.safe_render import render_pdf

    return render_pdf(html)


_HOSTILE_DOC = """\
<html><body>
<img src="{file}">
<a rel="attachment" href="{file}">download</a>
<link rel="stylesheet" href="http://127.0.0.1:1/x.css">
<p style="background-image:url({file})">text</p>
<p>SAFE-TEXT-MARKER</p>
</body></html>
"""


# ---------------------------------------------------------------------------
# Fetcher unit checks
# ---------------------------------------------------------------------------

def test_safe_fetcher_refuses_file_scheme(tmp_path):
    """A file: URL must be refused with an exception, never fetched."""
    fetcher = safe_url_fetcher()
    secret = tmp_path / "secret"
    secret.write_text("SECRET-CONTENT-8f3a", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        fetcher.fetch(secret.as_uri())
    assert "blocked" in str(exc.value).lower()
    assert "SECRET-CONTENT-8f3a" not in str(exc.value)


def test_safe_fetcher_refuses_http_scheme():
    fetcher = safe_url_fetcher()
    with pytest.raises(Exception) as exc:
        fetcher.fetch("http://127.0.0.1:1/x.css")
    assert "blocked" in str(exc.value).lower()


def test_safe_fetcher_refuses_https_scheme():
    fetcher = safe_url_fetcher()
    with pytest.raises(Exception):
        fetcher.fetch("https://example.com/x.css")


def test_safe_fetcher_refuses_javascript_scheme():
    fetcher = safe_url_fetcher()
    with pytest.raises(Exception):
        fetcher.fetch("javascript:alert(1)")


def test_safe_fetcher_allows_data_uri():
    uri = data_uri_for_bytes(b"hello", "text/plain")
    assert uri.startswith("data:text/plain;base64,")
    fetcher = safe_url_fetcher()
    response = fetcher.fetch(uri)
    assert response.read() == b"hello"


def test_safe_fetcher_scheme_check_is_exact_not_prefix():
    """The scheme check is an exact data: parse, not a prefix match.

    A prefix check (url.startswith("data")) would allow datafile:, DATABASE.,
    and DATA:.  The fetcher must refuse each of those and only allow a URL
    whose scheme is exactly data:.
    """
    fetcher = safe_url_fetcher()
    for hostile in (
        "datafile:///etc/passwd",
        "DATA:text/plain,hi",
        " data:text/plain,hi",
        "database.css",
        "data",
    ):
        with pytest.raises(Exception):
            fetcher.fetch(hostile)
    # A real data: URI is still allowed.
    uri = data_uri_for_bytes(b"ok", "text/plain")
    assert fetcher.fetch(uri).read() == b"ok"


# ---------------------------------------------------------------------------
# Render checks: the fetcher refuses each hostile URL, nothing leaks
# ---------------------------------------------------------------------------

@WEASYPRINT_SKIP
def test_pdf_refuses_file_image_and_attachment(tmp_path):
    """file: URLs in <img> and <a rel=attachment> are refused, not fetched.

    The renderer's fetcher is spied at class level: ``render_pdf``'s own
    ``SafeUrlFetcher.fetch`` is called for the ``file:`` URLs (the image and
    the attachment) and refuses each.  On main the default fetcher read the
    file instead of refusing, so the refusal assertions fail.
    """
    canary = tmp_path / "canary-hostname"
    canary.write_text("CANARY-HOSTNAME-77e1\n", encoding="utf-8")
    html = _HOSTILE_DOC.format(file=canary.as_uri())

    original_urlopen = _no_network()
    calls, stop_spy = _spy_fetcher()
    try:
        pdf = _render(html)
    finally:
        stop_spy()
        _restore_network(original_urlopen)

    assert pdf[:4] == b"%PDF"
    assert b"CANARY-HOSTNAME-77e1" not in pdf, "file content leaked into the PDF"
    # <img src> and <a rel=attachment href> both resolved against the fetcher;
    # a style url() of the same file is covered by its own test.
    assert calls.count(canary.as_uri()) >= 2, (
        f"fetcher was not called and refused for the file: URLs, calls={calls}"
    )


@WEASYPRINT_SKIP
def test_pdf_refuses_http_stylesheet():
    """An http: stylesheet URL is refused; no network call is made.

    ``OpenerDirector.open`` is patched to fail the test on any network
    attempt.  On main the default fetcher attempted the request and this
    test failed.
    """
    original_urlopen = _no_network()
    calls, stop_spy = _spy_fetcher()
    try:
        pdf = _render(_HOSTILE_DOC.format(file="file:///etc/hostname"))
    finally:
        stop_spy()
        _restore_network(original_urlopen)

    assert pdf[:4] == b"%PDF"
    assert "http://127.0.0.1:1/x.css" in calls, (
        "fetcher was not called and refused for the http: URL"
    )
    assert b"127.0.0.1" not in pdf, "internal host leaked into the PDF"


@WEASYPRINT_SKIP
def test_pdf_refuses_style_url_reference():
    """A url(file:...) inside a style attribute is refused, not fetched."""
    d = Path(tempfile.mkdtemp(prefix="safe-render-"))
    canary = d / "style-canary"
    canary.write_text("CANARY-STYLE-9b2d\n", encoding="utf-8")
    try:
        html = (
            "<html><body>"
            f'<p style="background-image:url(\'{canary.as_uri()}\')">text</p>'
            "<p>SAFE-TEXT-MARKER</p>"
            "</body></html>"
        )
        original_urlopen = _no_network()
        calls, stop_spy = _spy_fetcher()
        try:
            pdf = _render(html)
        finally:
            stop_spy()
            _restore_network(original_urlopen)
    finally:
        canary.unlink()

    assert pdf[:4] == b"%PDF"
    assert b"CANARY-STYLE-9b2d" not in pdf, "style url file content leaked into the PDF"
    assert canary.as_uri() in calls, "fetcher was not called and refused for the style url"


@WEASYPRINT_SKIP
def test_pdf_refuses_css_import():
    """An @import url(http:...) inside a style block is refused, not fetched."""
    original_urlopen = _no_network()
    calls, stop_spy = _spy_fetcher()
    try:
        html = (
            "<html><head>"
            '<style>@import url("http://127.0.0.1:1/x.css");</style>'
            "</head><body><p>SAFE-TEXT-MARKER</p></body></html>"
        )
        pdf = _render(html)
    finally:
        stop_spy()
        _restore_network(original_urlopen)

    assert pdf[:4] == b"%PDF"
    assert "http://127.0.0.1:1/x.css" in calls, (
        "fetcher was not called and refused for the @import URL"
    )


@WEASYPRINT_SKIP
def test_render_pdf_does_not_leak_the_file_into_the_pdf(tmp_path):
    """The shared renderer produces a PDF without the referenced file's bytes.

    A unique random marker is planted in a temp file.  The hostile document
    references it as an image and as an attachment.  The marker must be absent
    from the PDF bytes (raw, hex, and base64, because WeasyPrint compresses
    attachment streams with Flate, so a raw byte scan of a compressed PDF
    would pass even when the file leaked).  The pypdf attachment check below
    makes the test meaningful: the PDF must carry no embedded file.
    """
    import base64
    import secrets

    canary = tmp_path / "canary-safe"
    marker = f"LEAK-{secrets.token_hex(8)}"
    canary.write_text(marker + "\n", encoding="utf-8")
    html = _HOSTILE_DOC.format(file=canary.as_uri())

    original_urlopen = _no_network()
    try:
        pdf = render_pdf(html)
    finally:
        _restore_network(original_urlopen)

    assert pdf[:4] == b"%PDF"
    raw = marker.encode()
    assert raw not in pdf, "file content leaked into the PDF bytes (raw)"
    assert raw.hex() not in pdf.hex(), "file content leaked into the PDF (hex)"
    assert base64.b64encode(raw).decode() not in pdf.decode("latin-1"), (
        "file content leaked into the PDF (base64)"
    )
    assert b"127.0.0.1" not in pdf, "internal host leaked into the PDF"
    # No attachment embedding the file.
    reader = _pdf_reader(pdf)
    if reader is not None:
        assert list(reader.attachments) == [], (
            f"PDF carries an attachment: {list(reader.attachments)!r}"
        )


@WEASYPRINT_SKIP
def test_pdf_data_uri_image_still_renders():
    """data: URIs keep working: the invoice logo path must not break.

    The safe fetcher is spied and must be called for the data: URI (the
    supported way to embed an image) and serve it.
    """
    # A 1x1 transparent PNG.
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8"
        b"\xcf\xc0\xf0\x1f\x00\x05\x05\x02\x00_\xc8\xf2\xf5\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    uri = data_uri_for_bytes(png, "image/png")
    html = f'<html><body><img src="{uri}" alt="logo"><p>logo</p></body></html>'

    calls, stop_spy = _spy_fetcher()
    try:
        pdf = _render(html)
    finally:
        stop_spy()

    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000
    assert calls == [uri], f"fetcher was not called for the data: URI, calls={calls}"


# ---------------------------------------------------------------------------
# AO107-R4-4: hostile data: SVG and protocol-relative / relative URLs
# ---------------------------------------------------------------------------


@WEASYPRINT_SKIP
def test_data_svg_image_does_not_fetch_referenced_urls():
    """A data:image/svg+xml image whose SVG references file:, http: and //host
    renders without fetching any of the referenced URLs.

    The SVG body is inlined via a data: URI, which the fetcher allows.  The
    ``<image>`` elements inside the SVG reference hostile URLs; WeasyPrint
    must not follow them.  The fetcher is spied: it must be called for the
    data: URI and for each hostile URL inside the SVG, and refuse each
    hostile one.
    """
    import base64

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<image href="file:///etc/hostname" />'
        '<image href="http://127.0.0.1:1/x.png" />'
        '<image href="//evil.example/x.png" />'
        "</svg>"
    )
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    html = f'<html><body><img src="{uri}" alt="svg"><p>svg</p></body></html>'

    original_urlopen = _no_network()
    calls, stop_spy = _spy_fetcher()
    try:
        pdf = _render(html)
    finally:
        stop_spy()
        _restore_network(original_urlopen)

    assert pdf[:4] == b"%PDF"
    # The data: URI was fetched (allowed).
    assert uri in calls, f"data: SVG URI was not fetched, calls={calls}"
    # The hostile URLs inside the SVG were refused, not followed.  The
    # ``OpenerDirector.open`` patch above fails the test on any network
    # attempt, and the PDF must not carry the referenced file's content.
    assert b"CANARY" not in pdf, "file: URL content leaked into the PDF"
    assert b"127.0.0.1" not in pdf, "internal host leaked into the PDF"
    assert b"evil.example" not in pdf, "protocol-relative host leaked into the PDF"


@WEASYPRINT_SKIP
def test_protocol_relative_and_relative_urls_are_refused():
    """Protocol-relative (//host) and relative URLs in <img> are refused.

    A protocol-relative URL resolves against the base URL (or the document
    origin) and a relative URL resolves against the base URL; both can point
    at an internal host.  The safe fetcher must refuse them.
    """
    html = (
        "<html><body>"
        '<img src="//evil.example/x.png" alt="proto-rel">'
        '<img src="relative/path.png" alt="rel">'
        "<p>SAFE-TEXT-MARKER</p>"
        "</body></html>"
    )
    original_urlopen = _no_network()
    calls, stop_spy = _spy_fetcher()
    try:
        pdf = _render(html)
    finally:
        stop_spy()
        _restore_network(original_urlopen)

    assert pdf[:4] == b"%PDF"
    assert b"evil.example" not in pdf, "protocol-relative host leaked into the PDF"
    assert b"relative/path.png" not in pdf, "relative path leaked into the PDF"


# ---------------------------------------------------------------------------
# Call-site wiring: both PDF paths must use the shared safe renderer
# ---------------------------------------------------------------------------

@WEASYPRINT_SKIP
def test_invoice_pdf_uses_the_safe_renderer(monkeypatch):
    """render_invoice_pdf routes through the shared safe renderer (AO-PDF-1)."""
    import app.ar.invoice_brand.template as template
    import app.pdf.safe_render as safe_render_module

    captured: dict[str, object] = {}

    def _fake_render_pdf(html: str) -> bytes:
        captured["html"] = html
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(safe_render_module, "render_pdf", _fake_render_pdf)
    out = template.render_invoice_pdf("<html><body>INV</body></html>")
    assert out == b"%PDF-1.4 fake"
    assert captured["html"] == "<html><body>INV</body></html>"


@WEASYPRINT_SKIP
def test_specialist_pdf_uses_the_safe_renderer(monkeypatch):
    """_html_to_pdf routes through the shared safe renderer (AO-PDF-1)."""
    import app.api.specialist_pdf as specialist_pdf
    import app.pdf.safe_render as safe_render_module

    captured: dict[str, object] = {}

    def _fake_render_pdf(html: str) -> bytes:
        captured["html"] = html
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(safe_render_module, "render_pdf", _fake_render_pdf)
    out = specialist_pdf._html_to_pdf("<html><body>DRAFT</body></html>")
    assert out == b"%PDF-1.4 fake"
    assert captured["html"] == "<html><body>DRAFT</body></html>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pdf_reader(pdf: bytes):
    """A pypdf/PyPDF2 reader for the PDF, or None when neither is installed."""
    import io

    for mod in ("pypdf", "PyPDF2"):
        try:
            reader_cls = __import__(mod, fromlist=["PdfReader"]).PdfReader
        except Exception:
            continue
        try:
            return reader_cls(io.BytesIO(pdf))
        except Exception:
            continue
    return None
