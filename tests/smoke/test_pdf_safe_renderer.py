"""AO-PDF-1: the shared PDF renderer fetches nothing.

WeasyPrint's default URL fetcher follows ``file:`` and ``http(s):`` URLs found
in the document: it reads the file or makes the network request and inlines
the bytes into the PDF (an image via ``<img>`` or a style ``url()``, a file
into the PDF via ``<a rel="attachment">``).  The specialist draft body and
the invoice HTML both come from tenant or AI text, so a draft that references
``file:///`` (env, keys) or an internal host could read that into the PDF the
accountant downloads.

The renderer must refuse every scheme except ``data:``.  These tests drive the
renderer's fetcher against a hostile document and assert:

* the fetcher is called for each hostile URL and refuses it (no file read,
  no network call), and
* the PDF bytes do not contain the referenced file's content, and
* ``data:`` URIs (the invoice logo path) still render.

On main, where the call sites rendered with WeasyPrint's default fetcher, the
fetcher reads the file and attempts the network call, so the refusal and
network assertions fail.  WeasyPrint is a main dep (pyproject.toml), so these
run in CI; the skip is kept only for a dev venv without the system Pango
libraries.
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
    """Wrap the safe fetcher so every URL it sees is recorded.

    This is the same fetcher ``render_pdf`` installs, so "the fetcher was
    called and refused" is asserted against the object the renderer uses.
    """
    fetcher = safe_url_fetcher()
    calls: list[str] = []
    original = fetcher.fetch

    def spy(url, headers=None):
        calls.append(url)
        return original(url, headers)

    fetcher.fetch = spy
    return fetcher, calls


def _no_network():
    """Patch urllib so any network attempt fails the test."""
    import urllib.request

    def _explode(*args, **kwargs):
        raise AssertionError("renderer attempted a network call")

    original = urllib.request.urlopen
    urllib.request.urlopen = _explode
    return original


def _restore_network(original):
    import urllib.request

    urllib.request.urlopen = original


def _render(html: str, fetcher) -> bytes:
    from weasyprint import HTML

    return HTML(string=html, url_fetcher=fetcher).write_pdf()


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

    The renderer's fetcher is spied: it must be called for the ``file:`` URLs
    (the image and the attachment) and refuse each.  On main the default
    fetcher read the file instead of refusing, so the refusal assertions fail.
    """
    canary = tmp_path / "canary-hostname"
    canary.write_text("CANARY-HOSTNAME-77e1\n", encoding="utf-8")
    html = _HOSTILE_DOC.format(file=canary.as_uri())

    original_urlopen = _no_network()
    try:
        fetcher, calls = _spy_fetcher()
        pdf = _render(html, fetcher)
    finally:
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

    urllib is patched to fail the test on any network attempt.  On main the
    default fetcher attempted the request and this test failed.
    """
    original_urlopen = _no_network()
    try:
        fetcher, calls = _spy_fetcher()
        pdf = _render(_HOSTILE_DOC.format(file="file:///etc/hostname"), fetcher)
    finally:
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
        try:
            fetcher, calls = _spy_fetcher()
            pdf = _render(html, fetcher)
        finally:
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
    try:
        fetcher, calls = _spy_fetcher()
        html = (
            "<html><head>"
            '<style>@import url("http://127.0.0.1:1/x.css");</style>'
            "</head><body><p>SAFE-TEXT-MARKER</p></body></html>"
        )
        pdf = _render(html, fetcher)
    finally:
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

    fetcher, calls = _spy_fetcher()
    pdf = _render(html, fetcher)

    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000
    assert calls == [uri], f"fetcher was not called for the data: URI, calls={calls}"


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
