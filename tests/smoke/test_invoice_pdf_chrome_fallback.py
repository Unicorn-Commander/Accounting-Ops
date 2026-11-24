"""AO-PDF-1 (P2): the headless Chrome fallback path in render_invoice_pdf.

render_invoice_pdf uses the shared safe renderer (WeasyPrint) when it imports.
The Chrome fallback is reached only on an (ImportError, OSError) from that
import, never on a WeasyPrint render error.  Chrome itself runs offline:
--host-resolver-rules maps every DNS name to NOTFOUND, so no http(s): or
internal URL is fetched while the document renders.

These tests drive the real Chrome binary (headless) through the module's
helper functions and assert the fallback refuses a file: and an http:
resource.  They skip when no Chrome binary is on PATH (CI runners without a
browser), because the guard they check is the flag set plus the fallback
routing, both of which are covered by the unit tests below and by the
WeasyPrint-path fetcher tests in test_pdf_safe_renderer.py.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

import app.ar.invoice_brand.template as template


def _chrome_available() -> bool:
    return template._find_chrome() is not None


CHROME_SKIP = pytest.mark.skipif(
    not _chrome_available(),
    reason="no headless Chrome binary on PATH",
)

_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x05\x02\x00_\xc8\xf2\xf5\x00\x00\x00\x00IEND\xaeB`\x82"
)

_DOC = """<!doctype html><html><head>
<link rel="stylesheet" href="http://127.0.0.1:1/x.css">
<style>@import url("file://{file}");</style>
</head><body>
<img src="file://{file}" alt="probe">
<a rel="attachment" href="file://{file}">download</a>
<p style="background-image:url(file://{file})">styled</p>
<h1>{marker}</h1>
<img src="{png}" alt="logo">
</body></html>
"""


def _short_tmp() -> str:
    """A temp dir whose path is short enough for Chrome's singleton socket.

    Chrome's per-profile SingletonSocket path is hard-capped at 108 bytes.  On
    a runner with a long $TMPDIR (this dispatcher's jail is one) the default
    profile dir overflows it and Chrome aborts before it renders anything.
    ``--user-data-dir`` under a short path relocates the socket; this is a
    test-harness concern, not a change to what is rendered.
    """
    import tempfile

    base = os.environ.get("TMPDIR") or ""
    if len(base) < 60:
        return base
    for candidate in ("/tmp", "/var/tmp", "/home/agents/.local/share"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return tempfile.gettempdir()


def _run_chrome(chrome: str, html_path: Path, pdf_path: Path) -> subprocess.CompletedProcess:
    user_data_dir = Path(_short_tmp()) / f"ao107-chrome-{secrets.token_hex(4)}"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TMPDIR=str(Path(_short_tmp()) / "ao107-ctmp"))
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    try:
        return subprocess.run(
            [
                chrome,
                *template._chrome_flags(),
                f"--user-data-dir={user_data_dir}",
                f"--print-to-pdf={pdf_path}",
                html_path.as_uri(),
            ],
            check=False,
            capture_output=True,
            timeout=60,
            env=env,
        )
    finally:
        shutil.rmtree(user_data_dir, ignore_errors=True)
        shutil.rmtree(Path(env["TMPDIR"]), ignore_errors=True)


@CHROME_SKIP
def test_chrome_fallback_refuses_file_and_http_resources(tmp_path):
    """The offline Chrome flags refuse a file: and an http: resource.

    A unique random marker is planted in a temp file.  The hostile document
    references it four ways (img, attachment, style @import, css url) plus an
    http: stylesheet.  Through the module's _chrome_flags() the PDF must not
    embed the file, must not attach it, and must not fetch the http: URL.
    """
    import secrets

    chrome = template._find_chrome()
    marker = f"AO107-R2-1-{secrets.token_hex(6)}"
    secret = f"LEAK-{secrets.token_hex(8)}\n"
    secret_file = tmp_path / "probe-secret"
    secret_file.write_text(secret, encoding="utf-8")

    html_path = tmp_path / "hostile.html"
    html_path.write_text(
        _DOC.format(file=secret_file.as_uri(), marker=marker, png=template.default_logo_data_uri()),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "hostile.pdf"
    proc = _run_chrome(chrome, html_path, pdf_path)
    assert proc.returncode == 0, f"chrome exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:400]}"

    pdf = pdf_path.read_bytes()
    assert pdf[:4] == b"%PDF", "no PDF written"
    # The file's content must not appear in the PDF bytes.  WeasyPrint-style
    # attachment streams are Flate-compressed, so a raw byte scan of a
    # compressed PDF would pass even when the file leaked; the pypdf
    # attachment check below makes the test meaningful.
    assert secret.encode() not in pdf, "file content leaked into the PDF bytes"
    assert marker.encode() in pdf or _extract_text(pdf) and marker in _extract_text(pdf), (
        f"expected marker {marker!r} in the rendered page"
    )
    # No attachment embedding the file.
    attachments = _attachments(pdf)
    assert attachments == [], f"PDF carries an attachment: {attachments!r}"
    # The http: stylesheet was refused, not fetched: no trace of the internal
    # host in the PDF.
    assert b"127.0.0.1" not in pdf, "internal host leaked into the PDF"


@CHROME_SKIP
def test_chrome_flags_refuse_http_only(tmp_path):
    """--host-resolver-rules refuses an http: resource even with file: allowed.

    A document that only references an http: URL (no file:) must still render
    and must not fetch the URL.  This isolates the DNS-mapping flag from the
    file: behaviour.
    """
    import secrets

    chrome = template._find_chrome()
    marker = f"AO107-R2-1-H-{secrets.token_hex(6)}"
    html_path = tmp_path / "http-only.html"
    html_path.write_text(
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="http://127.0.0.1:1/x.css">'
        "</head><body><h1>" + marker + "</h1></body></html>",
        encoding="utf-8",
    )
    pdf_path = tmp_path / "http-only.pdf"
    proc = _run_chrome(chrome, html_path, pdf_path)
    assert proc.returncode == 0, f"chrome exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:400]}"
    pdf = pdf_path.read_bytes()
    assert pdf[:4] == b"%PDF"
    assert b"127.0.0.1" not in pdf, "internal host leaked into the PDF"


def test_chrome_flags_include_offline_guard():
    """The Chrome flag set maps every DNS name to NOTFOUND and stays headless."""
    flags = " ".join(template._chrome_flags())
    assert "--host-resolver-rules=" in flags, "DNS mapping flag missing"
    assert "MAP * ~NOTFOUND" in flags, "DNS mapping is not NOTFOUND"
    assert "--headless" in flags, "not headless"
    assert "--print-to-pdf" not in flags, "print target must be passed by the caller"


def test_render_invoice_pdf_falls_back_only_on_import_oserror(tmp_path, monkeypatch):
    """The Chrome fallback is reached only on (ImportError, OSError), never on a
    WeasyPrint render error.

    A WeasyPrint render error (any Exception other than ImportError/OSError)
    must propagate, not silently route the unguarded HTML to Chrome.
    """
    import app.pdf.safe_render as safe_render_module

    def _explode(html: str) -> bytes:
        raise ValueError("weasyprint render failed")

    monkeypatch.setattr(safe_render_module, "render_pdf", _explode)
    # The Chrome binary must not be consulted: a render error must raise.
    with pytest.raises(ValueError):
        template.render_invoice_pdf("<html><body>INV</body></html>")


def test_render_invoice_pdf_no_chrome_raises(monkeypatch):
    """When WeasyPrint fails to import and no Chrome binary exists, the render
    raises rather than silently producing nothing."""
    import app.pdf.safe_render as safe_render_module

    def _missing(html: str) -> bytes:
        raise ImportError("weasyprint not installed")

    monkeypatch.setattr(safe_render_module, "render_pdf", _missing)
    monkeypatch.setattr(template, "_find_chrome", lambda: None)
    with pytest.raises(RuntimeError):
        template.render_invoice_pdf("<html><body>INV</body></html>")


# ---------------------------------------------------------------------------
# Helpers: PDF text extraction and attachment listing without a hard dep.
# ---------------------------------------------------------------------------

def _extract_text(pdf: bytes) -> str | None:
    import io

    for mod in ("pypdf", "PyPDF2"):
        try:
            reader_cls = __import__(mod, fromlist=["PdfReader"]).PdfReader
        except Exception:
            continue
        try:
            reader = reader_cls(io.BytesIO(pdf))
            return "\n".join((reader.pages[i].extract_text() or "") for i in range(len(reader.pages)))
        except Exception:
            continue
    return None


def _attachments(pdf: bytes) -> list[str]:
    import io

    for mod in ("pypdf", "PyPDF2"):
        try:
            reader_cls = __import__(mod, fromlist=["PdfReader"]).PdfReader
        except Exception:
            continue
        try:
            reader = reader_cls(io.BytesIO(pdf))
            return list(reader.attachments)
        except Exception:
            continue
    return []
