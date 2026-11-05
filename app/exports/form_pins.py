"""Runtime check that a pinned IRS form PDF matches its field map.

A map is pinned only when it exports ``<PREFIX>_PINNED_REVISION`` plus either
``<PREFIX>_PINNED_SHA256`` or both ``<PREFIX>_PINNED_PAGE_COUNT`` and
``<PREFIX>_PINNED_FIELD_COUNT``. Prose that mentions a revision (W-9, 941,
8821, and the rest) is not a pin: those modules do not carry the constants,
and a pin without a sha or a page/field count would refuse every render.

Check order for a pinned file: sha256 first, when the map carries one.
Otherwise the PDF must match the pinned page count, ``get_fields()`` count,
and the printed footer revision (``Form 9465 (Rev. 12-2026)``).
"""

from __future__ import annotations

import hashlib
import importlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

# Same directory ``app.exports.form_prefill.IRS_FORMS_DIR`` points at.
FORMS_DIR = Path(__file__).resolve().parents[2] / "data" / "irs_forms"

# kind, field-map module, constant prefix, filename, download-script stem.
# A row whose module does not export ``<PREFIX>_PINNED_REVISION`` is skipped.
_PIN_SOURCES: tuple[tuple[str, str, str, str, str], ...] = (
    ("9465", "app.exports.f9465_field_map", "F9465", "f9465.pdf", "f9465"),
)

_REV_IN_PARENS = re.compile(r"\(Rev\. [^)]+\)")


@dataclass(frozen=True)
class FormPin:
    kind: str
    label: str
    filename: str
    refresh_name: str
    revision: str
    page_count: int | None
    field_count: int | None
    sha256: str | None

    @property
    def short_revision(self) -> str:
        match = _REV_IN_PARENS.search(self.revision)
        if match:
            return match.group(0)[1:-1]
        return self.revision


@dataclass(frozen=True)
class PinCheck:
    """Result of comparing one PDF on disk to its field-map pin.

    ``ok`` is true when the form is not pinned, or the file matches.
    ``message`` is the refusal text when ``ok`` is false. ``on_disk`` is the
    short revision read from the footer (``Rev. 9-2020``), or ``missing`` /
    ``unreadable`` / ``unrecognized``.
    """

    kind: str
    ok: bool
    pinned: bool
    message: str
    on_disk: str
    pinned_revision: str
    matched_by: str


def _load_pin(
    kind: str,
    module_name: str,
    prefix: str,
    filename: str,
    refresh_name: str,
) -> FormPin | None:
    module = importlib.import_module(module_name)
    revision = getattr(module, f"{prefix}_PINNED_REVISION", None)
    if not isinstance(revision, str) or not revision.strip():
        return None
    page_count = getattr(module, f"{prefix}_PINNED_PAGE_COUNT", None)
    field_count = getattr(module, f"{prefix}_PINNED_FIELD_COUNT", None)
    sha256 = getattr(module, f"{prefix}_PINNED_SHA256", None)
    pages = page_count if isinstance(page_count, int) else None
    fields = field_count if isinstance(field_count, int) else None
    digest = sha256.strip().lower() if isinstance(sha256, str) and sha256.strip() else None
    # A revision string alone cannot be checked. Skip it rather than refuse
    # every render of that form.
    if digest is None and (pages is None or fields is None):
        return None
    label = revision.split(" (Rev.", 1)[0].strip() or f"Form {kind}"
    return FormPin(
        kind=kind,
        label=label,
        filename=filename,
        refresh_name=refresh_name,
        revision=revision.strip(),
        page_count=pages,
        field_count=fields,
        sha256=digest,
    )


def _build_pins() -> dict[str, FormPin]:
    pins: dict[str, FormPin] = {}
    for source in _PIN_SOURCES:
        pin = _load_pin(*source)
        if pin is not None:
            pins[pin.kind] = pin
    return pins


PINS: dict[str, FormPin] = _build_pins()


def refusal_message(pin: FormPin, on_disk: str) -> str:
    """The sentence renderers raise and ``/readyz`` reports."""
    pinned = pin.short_revision or pin.revision
    shown = on_disk or "unrecognized"
    return (
        f"{pin.label} on disk is {shown}, the field map is pinned to "
        f"{pinned}: run scripts/download_irs_forms.sh --refresh {pin.refresh_name}"
    )


def pdf_path_for(pin: FormPin, tax_year: int | None) -> Path:
    """Year file when it exists (``f9465_2025.pdf``), else the unversioned PDF."""
    base = FORMS_DIR / pin.filename
    if tax_year is not None:
        yearly = FORMS_DIR / f"{base.stem}_{tax_year}{base.suffix}"
        if yearly.is_file():
            return yearly
    return base


def _inspect(data: bytes, pin: FormPin) -> tuple[int, int, str]:
    reader = PdfReader(io.BytesIO(data))
    pages = len(reader.pages)
    fields = reader.get_fields() or {}
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    printed = ""
    if pin.revision and pin.revision in text:
        printed = pin.revision
    else:
        match = re.search(re.escape(pin.label) + r" \(Rev\. [^)]+\)", text)
        if match:
            printed = match.group(0)
    return pages, len(fields), printed


def _on_disk_label(pin: FormPin, printed: str, pages: int, n_fields: int) -> str:
    short = ""
    match = _REV_IN_PARENS.search(printed or "")
    if match:
        short = match.group(0)[1:-1]
    if printed and printed != pin.revision and short:
        return short
    if printed == pin.revision and short:
        # Same footer, wrong shape. Say so; "on disk is Rev. 12-2026, pinned
        # to Rev. 12-2026" would hide the page or field mismatch.
        return f"{short} ({pages} pages, {n_fields} fields)"
    return short or "unrecognized"


def _fail(pin: FormPin, on_disk: str) -> PinCheck:
    return PinCheck(
        kind=pin.kind,
        ok=False,
        pinned=True,
        message=refusal_message(pin, on_disk),
        on_disk=on_disk,
        pinned_revision=pin.short_revision,
        matched_by="mismatch",
    )


def _pass(pin: FormPin, matched_by: str) -> PinCheck:
    return PinCheck(
        kind=pin.kind,
        ok=True,
        pinned=True,
        message="",
        on_disk=pin.short_revision,
        pinned_revision=pin.short_revision,
        matched_by=matched_by,
    )


def verify_pinned_form(
    kind: str,
    *,
    tax_year: int | None = None,
    pdf_path: Path | None = None,
) -> PinCheck:
    """Compare the PDF for ``kind`` to its field-map pin.

    An unpinned kind returns ``ok=True`` and ``pinned=False``. Pass ``pdf_path``
    when the caller has already chosen the file (a year-specific PDF).
    """
    key = (kind or "").strip().lower()
    pin = PINS.get(key)
    if pin is None:
        return PinCheck(
            kind=key,
            ok=True,
            pinned=False,
            message="",
            on_disk="",
            pinned_revision="",
            matched_by="unpinned",
        )
    path = Path(pdf_path) if pdf_path is not None else pdf_path_for(pin, tax_year)
    if not path.is_file():
        return _fail(pin, "missing")
    try:
        data = path.read_bytes()
    except OSError:
        return _fail(pin, "unreadable")
    if pin.sha256 and hashlib.sha256(data).hexdigest() == pin.sha256:
        return _pass(pin, "sha256")
    try:
        pages, n_fields, printed = _inspect(data, pin)
    except (OSError, PyPdfError):
        return _fail(pin, "unreadable")
    structure_ok = (
        pin.page_count is not None
        and pin.field_count is not None
        and pages == pin.page_count
        and n_fields == pin.field_count
        and printed == pin.revision
    )
    if structure_ok:
        return _pass(pin, "pages+fields+revision")
    return _fail(pin, _on_disk_label(pin, printed, pages, n_fields))


def pinned_form_warnings() -> list[dict[str, str]]:
    """Mismatches for ``/readyz``. Empty when every pinned PDF matches.

    Does not raise: a missing or unreadable PDF is one warning. The route
    stays ``status: ready``.
    """
    warnings: list[dict[str, str]] = []
    for kind in sorted(PINS):
        try:
            check = verify_pinned_form(kind)
        except (OSError, PyPdfError):
            pin = PINS[kind]
            check = _fail(pin, "unreadable")
        if check.ok:
            continue
        warnings.append(
            {
                "kind": check.kind,
                "on_disk": check.on_disk,
                "pinned": check.pinned_revision,
                "message": check.message,
            }
        )
    return warnings
