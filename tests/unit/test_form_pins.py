"""Pinned IRS form PDFs are refused when they are not the mapped revision.

The refusal sentence is the contract::

    Form 9465 on disk is Rev. 9-2020, the field map is pinned to Rev. 12-2026:
    run scripts/download_irs_forms.sh --refresh f9465

Stubs are built in tmp. The official PDF is gitignored; the sha256 pass runs
only when ``data/irs_forms/f9465.pdf`` is that exact pin.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.exports.f9465_field_map import F9465_PINNED_SHA256
from app.exports.form_pins import (
    FORMS_DIR,
    PINS,
    verify_pinned_form,
)
from app.exports.form_prefill import WatermarkMode

_WRONG = (
    "Form 9465 on disk is Rev. 9-2020, the field map is pinned to "
    "Rev. 12-2026: run scripts/download_irs_forms.sh --refresh f9465"
)
_PINNED_FOOTER = "Form 9465 (Rev. 12-2026)"
_OLD_FOOTER = "Form 9465 (Rev. 9-2020)"


def _write_stub(path: Path, *, pages: int, fields: int, footer: str) -> Path:
    import pymupdf

    doc = pymupdf.open()
    for page_index in range(pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 36), footer)
        if page_index != 0:
            continue
        for n in range(fields):
            widget = pymupdf.Widget()
            widget.field_name = f"f_{n}"
            widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            row = n % 25
            col = n // 25
            x = 72 + col * 180
            y = 80 + row * 22
            widget.rect = pymupdf.Rect(x, y, x + 80, y + 14)
            page.add_widget(widget)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def test_only_maps_that_export_a_pin_are_checked():
    # Other field maps name a revision in prose and do not export
    # ``*_PINNED_REVISION``. Pinning them here would refuse every render.
    assert list(PINS) == ["9465"]
    assert PINS["9465"].sha256 == F9465_PINNED_SHA256
    assert PINS["9465"].page_count == 1
    assert PINS["9465"].field_count == 40
    unpinned = verify_pinned_form("1040")
    assert unpinned.ok is True
    assert unpinned.pinned is False
    assert unpinned.matched_by == "unpinned"


def test_stub_with_the_old_revision_is_refused(tmp_path: Path):
    path = _write_stub(tmp_path / "f9465.pdf", pages=2, fields=3, footer=_OLD_FOOTER)
    check = verify_pinned_form("9465", pdf_path=path)
    assert check.ok is False
    assert check.on_disk == "Rev. 9-2020"
    assert check.pinned_revision == "Rev. 12-2026"
    assert check.message == _WRONG


def test_render_9465_refuses_the_stub_and_does_not_fill(monkeypatch, tmp_path: Path):
    import app.exports.f9465_fill as fill

    _write_stub(tmp_path / "f9465.pdf", pages=2, fields=3, footer=_OLD_FOOTER)
    monkeypatch.setattr(fill, "IRS_FORMS_DIR", tmp_path)
    with pytest.raises(ValueError) as exc:
        fill.render_9465(2025, WatermarkMode.NONE, {})
    assert str(exc.value) == _WRONG


def test_render_form_refuses_with_the_same_sentence(monkeypatch, tmp_path: Path):
    import app.exports.form_pins as pins
    from app.exports.form_registry import render_form

    _write_stub(tmp_path / "f9465.pdf", pages=2, fields=3, footer=_OLD_FOOTER)
    monkeypatch.setattr(pins, "FORMS_DIR", tmp_path)
    with pytest.raises(ValueError) as exc:
        render_form("9465", None, WatermarkMode.NONE, {})
    assert str(exc.value) == _WRONG


def test_year_specific_file_is_the_one_that_is_checked(monkeypatch, tmp_path: Path):
    import app.exports.f9465_fill as fill

    _write_stub(tmp_path / "f9465.pdf", pages=1, fields=40, footer=_PINNED_FOOTER)
    _write_stub(tmp_path / "f9465_2025.pdf", pages=2, fields=3, footer=_OLD_FOOTER)
    monkeypatch.setattr(fill, "IRS_FORMS_DIR", tmp_path)
    with pytest.raises(ValueError) as exc:
        fill.render_9465(2025, WatermarkMode.DRAFT, {})
    assert str(exc.value) == _WRONG


def test_structure_match_passes_when_the_sha_differs(tmp_path: Path):
    path = _write_stub(tmp_path / "f9465.pdf", pages=1, fields=40, footer=_PINNED_FOOTER)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest != F9465_PINNED_SHA256
    check = verify_pinned_form("9465", pdf_path=path)
    assert check.ok is True
    assert check.matched_by == "pages+fields+revision"
    assert check.message == ""


def test_sha256_match_passes_before_page_and_field_counts(monkeypatch, tmp_path: Path):
    from dataclasses import replace

    import app.exports.form_pins as pins

    path = _write_stub(tmp_path / "f9465.pdf", pages=2, fields=3, footer=_OLD_FOOTER)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setitem(pins.PINS, "9465", replace(pins.PINS["9465"], sha256=digest))
    check = verify_pinned_form("9465", pdf_path=path)
    assert check.ok is True
    assert check.matched_by == "sha256"


def test_missing_pdf_is_a_mismatch_not_an_exception(tmp_path: Path, monkeypatch):
    import app.exports.form_pins as pins

    monkeypatch.setattr(pins, "FORMS_DIR", tmp_path)
    check = verify_pinned_form("9465")
    assert check.ok is False
    assert check.on_disk == "missing"
    assert "download_irs_forms.sh --refresh f9465" in check.message


def test_pinned_file_passes_when_present():
    path = FORMS_DIR / "f9465.pdf"
    if not path.is_file():
        pytest.skip("data/irs_forms/f9465.pdf is not on this machine")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != F9465_PINNED_SHA256:
        pytest.skip("f9465.pdf is present but it is not the pinned revision")
    check = verify_pinned_form("9465", pdf_path=path)
    assert check.ok is True
    assert check.matched_by == "sha256"


def test_same_footer_with_the_wrong_shape_names_the_counts(tmp_path: Path):
    path = _write_stub(tmp_path / "f9465.pdf", pages=2, fields=3, footer=_PINNED_FOOTER)
    check = verify_pinned_form("9465", pdf_path=path)
    assert check.ok is False
    assert check.on_disk == "Rev. 12-2026 (2 pages, 3 fields)"
    assert "pinned to Rev. 12-2026" in check.message
    assert "--refresh f9465" in check.message


@pytest.mark.asyncio
async def test_readyz_warning_field_shape(monkeypatch, tmp_path: Path):
    import app.exports.form_pins as pins
    from app.main import readyz

    _write_stub(tmp_path / "f9465.pdf", pages=2, fields=3, footer=_OLD_FOOTER)
    monkeypatch.setattr(pins, "FORMS_DIR", tmp_path)
    body = await readyz()
    assert body["status"] == "ready"
    warnings = body["pinned_form_warnings"]
    assert isinstance(warnings, list) and len(warnings) == 1
    warning = warnings[0]
    assert set(warning) == {"kind", "on_disk", "pinned", "message"}
    assert warning["kind"] == "9465"
    assert warning["on_disk"] == "Rev. 9-2020"
    assert warning["pinned"] == "Rev. 12-2026"
    assert warning["message"] == _WRONG


@pytest.mark.asyncio
async def test_readyz_stays_ready_when_the_pin_matches(monkeypatch, tmp_path: Path):
    import app.exports.form_pins as pins
    from app.main import healthz, readyz

    _write_stub(tmp_path / "f9465.pdf", pages=1, fields=40, footer=_PINNED_FOOTER)
    monkeypatch.setattr(pins, "FORMS_DIR", tmp_path)
    body = await readyz()
    assert body["status"] == "ready"
    assert body["pinned_form_warnings"] == []
    health = await healthz()
    assert health["status"] == "ok"
    assert "pinned_form_warnings" not in health
