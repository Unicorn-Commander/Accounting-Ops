"""AO107-R3-2: one renderer, fail closed, hostile document leaks nothing.

The invoice PDF has exactly one renderer, the shared safe renderer
(AO-PDF-1).  When WeasyPrint is unavailable the render raises
``PdfRenderUnavailableError`` (the caller answers 503) and no second
renderer (no subprocess) is ever started.

The hostile document test drives ``render_invoice_pdf`` end to end:
iframe, embed, object, CSS url(), @import, img and a rel=attachment
link, all pointing at a temp file with a unique random marker and at an
http://127.0.0.1:<port> listener started in the test.  The marker must
be absent from the pypdf-EXTRACTED text (a raw byte scan misses
compressed streams) and the listener must receive zero requests.
"""
from __future__ import annotations

import builtins
import contextlib
import secrets
import socket
import threading
import time

import pytest

from app.ar.invoice_brand.template import (
    PdfRenderUnavailableError,
    render_invoice_pdf,
)


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


def _blocked_weasyprint_import(exc: Exception):
    """Route the weasyprint import through ``exc`` for the duration of a call."""
    original_import = builtins.__import__

    def _import(name, *args, **kwargs):
        if name == "weasyprint" or name.startswith("weasyprint."):
            raise exc
        return original_import(name, *args, **kwargs)

    return _import


def _subprocess_spy():
    """Patch subprocess so any start fails the test; record whether it was asked."""
    import subprocess

    calls: dict[str, bool] = {"run": False, "popen": False}

    def _run_refused(*args, **kwargs):
        calls["run"] = True
        raise AssertionError("a subprocess was started; the renderer must fail closed")

    def _popen_refused(*args, **kwargs):
        calls["popen"] = True
        raise AssertionError("a subprocess was started; the renderer must fail closed")

    original_run = subprocess.run
    original_popen = subprocess.Popen
    subprocess.run = _run_refused
    subprocess.Popen = _popen_refused
    return calls, original_run, original_popen


def _restore_subprocess(original_run, original_popen) -> None:
    import subprocess

    subprocess.run = original_run
    subprocess.Popen = original_popen


@contextlib.contextmanager
def _force_weasyprint_unavailable(exc: Exception):
    """Remove safe_render from sys.modules so re-import re-runs the weasyprint import.

    With the weasyprint import blocked, ``import app.pdf.safe_render``
    raises (ImportError or OSError) and ``render_invoice_pdf`` must fail
    closed.  Yields, then restores the original module object in
    ``sys.modules`` so subsequent tests see the working renderer.
    """
    import sys

    original_import = builtins.__import__
    monkeypatch_import = _blocked_weasyprint_import(exc)
    saved_module = sys.modules.pop("app.pdf.safe_render", None)
    saved_weasy = sys.modules.pop("weasyprint", None)
    builtins.__import__ = monkeypatch_import
    try:
        # The template module keeps its ``from app.pdf.safe_render import ...``
        # inside render_invoice_pdf, so the import happens at call time and
        # re-runs the module-level weasyprint import with the block in place.
        yield
    finally:
        builtins.__import__ = original_import
        if saved_module is not None:
            sys.modules["app.pdf.safe_render"] = saved_module
        else:
            sys.modules.pop("app.pdf.safe_render", None)
        if saved_weasy is not None:
            sys.modules["weasyprint"] = saved_weasy
        else:
            sys.modules.pop("weasyprint", None)


def test_render_invoice_pdf_fails_closed_when_weasyprint_cannot_import():
    """WeasyPrint unavailable (ImportError forced) -> the clear error.

    No subprocess is started: a second renderer is not a fallback, it is
    the leak.  subprocess.run/Popen are patched to fail if consulted.
    """
    calls, original_run, original_popen = _subprocess_spy()
    try:
        with _force_weasyprint_unavailable(ImportError("weasyprint not installed (forced by the test)")):
            with pytest.raises(PdfRenderUnavailableError) as exc:
                render_invoice_pdf("<html><body>INV</body></html>")
    finally:
        _restore_subprocess(original_run, original_popen)
    assert "unavailable" in str(exc.value).lower()
    assert not calls["run"], "subprocess.run was called"
    assert not calls["popen"], "subprocess.Popen was called"


def test_render_invoice_pdf_fails_closed_on_system_library_error():
    """An OSError from the WeasyPrint import (missing Pango) -> the clear error."""
    calls, original_run, original_popen = _subprocess_spy()
    try:
        with _force_weasyprint_unavailable(OSError("cannot load library libpango (forced by the test)")):
            with pytest.raises(PdfRenderUnavailableError) as exc:
                render_invoice_pdf("<html><body>INV</body></html>")
    finally:
        _restore_subprocess(original_run, original_popen)
    assert "unavailable" in str(exc.value).lower()
    assert not calls["run"], "subprocess.run was called"
    assert not calls["popen"], "subprocess.Popen was called"


@WEASYPRINT_SKIP
def test_hostile_invoice_pdf_leaks_nothing_and_makes_no_network_call(tmp_path):
    """The hostile document through render_invoice_pdf: nothing leaks, nothing fetched.

    iframe, embed, object, CSS url(), @import, img and a rel=attachment
    link point at (a) a temp file holding a unique random marker and (b)
    an http://127.0.0.1:<port> listener started in the test.  The marker
    must be absent from the pypdf-EXTRACTED text (not the raw bytes:
    compressed streams hide a raw-byte scan) and the listener must
    receive ZERO requests.
    """
    marker = f"AO107-R3-2-{secrets.token_hex(8)}"
    secret_file = tmp_path / "probe-secret"
    secret_file.write_text(marker + "\n", encoding="utf-8")
    file_uri = secret_file.as_uri()

    # A listener on an ephemeral 127.0.0.1 port that counts every request.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    hits: list[bytes] = []
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            try:
                hits.append(conn.recv(1024))
            finally:
                conn.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        http_url = f"http://127.0.0.1:{port}/x.css"
        html = f"""<!doctype html><html><head>
<link rel="stylesheet" href="{http_url}">
<style>@import url("{http_url}");
.x{{background-image:url({file_uri})}}</style>
</head><body>
<iframe src="{file_uri}"></iframe>
<embed src="{file_uri}">
<object data="{file_uri}"></object>
<img src="{file_uri}" alt="probe">
<img src="{http_url}" alt="probe-http">
<link rel="attachment" href="{file_uri}">
<a rel="attachment" href="{http_url}">download</a>
<p style="background-image:url({file_uri})">styled</p>
<h1>INVOICE-AO107-R3</h1>
</body></html>
"""
        pdf = render_invoice_pdf(html)
    finally:
        stop.set()
        time.sleep(0.2)
        server.close()
        thread.join(timeout=5)

    assert pdf[:4] == b"%PDF", "no PDF produced"
    text = _extract_text(pdf)
    assert text is not None, "pypdf could not extract text from the PDF"
    assert marker not in text, (
        f"the file marker {marker!r} leaked into the PDF extracted text"
    )
    assert f"127.0.0.1:{port}" not in text, "internal host leaked into the PDF text"
    assert "INVOICE-AO107-R3" in text, "the document itself did not render"
    # The listener received zero requests: no http(s) fetch of any kind.
    assert hits == [], f"the internal listener received {len(hits)} request(s): {hits!r}"


def _extract_text(pdf: bytes) -> str | None:
    """The pypdf-EXTRACTED text, or None when pypdf is unavailable."""
    import io

    try:
        reader_cls = __import__("pypdf", fromlist=["PdfReader"]).PdfReader
    except Exception:
        try:
            reader_cls = __import__("PyPDF2", fromlist=["PdfReader"]).PdfReader
        except Exception:
            return None
    try:
        reader = reader_cls(io.BytesIO(pdf))
        return "\n".join(
            (reader.pages[i].extract_text() or "") for i in range(len(reader.pages))
        )
    except Exception:
        return None
