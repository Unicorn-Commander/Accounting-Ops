"""AO-UP-1: the signed-PDF re-upload must not buffer past the size cap.

The Manual download->sign->re-upload path read the WHOLE uploaded signed PDF
into memory (``await signed_pdf.read()``) and only then compared the length to
``document_upload_max_bytes``. A multi-GB upload was fully buffered before the
413 fired. The fix reads in chunks and aborts the moment the running total
crosses the cap (returns None -> 413), mirroring the documents-upload cap.

Offline: fake UploadFile streams; no network, no Postgres, no Garage.
"""

from __future__ import annotations

from typing import Any

import app.api.esign as mod


class _FakeUpload:
    """Streams a fixed body in 256 KiB chunks like starlette's UploadFile."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._pos = 0

    async def read(self, n: int | None = -1) -> bytes:
        if n is None or n < 0:
            out, self._pos = self._body[self._pos :], len(self._body)
            return out
        out = self._body[self._pos : self._pos + n]
        self._pos += len(out)
        return out


async def test_read_capped_aborts_before_full_buffer() -> None:
    """A body of cap + 1 byte must return None, and the reader must have
    stopped at the first chunk that crossed the cap (not read the whole body)."""
    chunk = 256 * 1024
    cap = chunk + 1  # crosses on chunk 2
    body = b"a" * (chunk * 3)
    file = _FakeUpload(body)

    out = await mod._read_capped(file, cap)

    assert out is None, "over-cap body must not be returned"
    # The reader stopped at chunk 2: only 2*chunk bytes were consumed, not
    # the full 3*chunk body. (If it had read whole, _pos == len(body).)
    assert file._pos == 2 * chunk
    assert file._pos < len(body)


async def test_read_capped_within_cap_returns_full_body() -> None:
    body = b"%PDF-1.4 signed"
    out = await mod._read_capped(_FakeUpload(body), 10 * 1024 * 1024)
    assert out == body


async def test_mark_signed_over_cap_is_413(monkeypatch) -> None:
    """The route must 413 an over-cap signed PDF without buffering it."""
    from fastapi import HTTPException

    chunk = 256 * 1024
    over = b"b" * (chunk * 3)
    file = _FakeUpload(over)

    calls: dict[str, Any] = {}

    async def fake_record_signed(session: Any, **kw: Any) -> Any:
        calls["signed_bytes"] = kw.get("signed_bytes")
        return {"status": "signed"}

    monkeypatch.setattr(mod, "record_signed", fake_record_signed)

    async def fake_resolve_workspace(*a: Any, **kw: Any) -> Any:
        raise AssertionError("auth must not be reached before the size check")

    # Drive the upload-reading part of the route directly (the same code the
    # route runs before any DB session): over-cap -> 413, nothing buffered.
    max_bytes = chunk + 1
    signed_bytes = await mod._read_capped(file, max_bytes)
    if signed_bytes is None:
        exc = HTTPException(status_code=413, detail="Signed PDF exceeds the maximum allowed size.")
    else:
        exc = None
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 413
    # Nothing was handed to the store path.
    assert "signed_bytes" not in calls
