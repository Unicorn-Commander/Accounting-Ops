"""MeF-style electronic-filing PACKAGE generation (the artifact, not the wire).

What this module IS
-------------------
It turns a finished work product's provenance-bearing ``inputs`` into a
well-formed, *schema-shaped* IRS Modernized e-File (MeF) style XML return plus a
manifest — a genuine artifact that a *registered transmitter* could submit. The
package is real: a ``ReturnHeader`` (tax year, ``ReturnType`` = ``IRS1040``, the
filer identity, an Originator/EFIN block when one is supplied, and a templated
``ReturnTs`` timestamp) and a ``ReturnData`` block carrying exactly the 1040 line
values present in ``inputs`` — never a fabricated figure.

What this module is NOT
-----------------------
It is NOT a live IRS connection. The IRS MeF system only accepts XML returns from
transmitters enrolled for Application-to-Application (A2A) or Internet Filing
(IFA) under their own **EFIN** (Electronic Filing Identification Number). Standing
up that wire is gated — see :mod:`app.efile.transmit`. This module produces the
package; the gate decides whether it may ever go on the wire. It is also NOT an
XSD-validated artifact: the element names below approximate the public IRS1040
MeF schema for shape/readability and would need to be pinned against the official
schema package (and a registered ``SoftwareId``) before real transmission.

Anti-fabrication
----------------
Mirrors :func:`app.api.work_products._assert_provenance`: any input that carries a
value MUST carry a ``source_ref`` or generation is refused. Missing data is left
out (a transmitter would reject it — surfaced as a warning), never invented. No
line is ever derived from another and no zero is ever inserted.

No wall-clock / randomness
--------------------------
This module never calls ``datetime.now()`` or ``random``. The return timestamp is
either an ``as_of`` value passed in by the caller or a templated placeholder
(``{{RETURN_TIMESTAMP}}``) the transmitter fills at send time. This keeps package
bytes (and therefore the manifest ``sha256``) deterministic for a given input.

Logical-key -> MeF-element mapping
----------------------------------
The work product's logical 1040 keys (the same keys
``app.exports.f1040_field_map`` uses to fill the paper form) map to MeF-style
element names via :data:`MEF_LINE_MAP` (+ :data:`FILING_STATUS_CD` for the filing
status code). NOTE: the e-file XML carries computed lines the *paper* AcroForm
omits — e.g. ``total_tax_24`` -> ``TotalTaxAmt`` has no fillable box on the TY2025
``f1040.pdf`` but IS a transmitted MeF element.
"""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Constants / mappings
# ---------------------------------------------------------------------------

# Default namespace for the (approximate) IRS MeF return. Emitted as a literal
# ``xmlns`` attribute so serialized tags stay clean (<ReturnHeader>, not
# <ns0:ReturnHeader>) while a re-parse still sees a proper namespace.
_MEF_NAMESPACE = "http://www.irs.gov/efile"

# Templated fields the registered transmitter fills at send time. Using a brace
# placeholder (never wall-clock) keeps the artifact deterministic + honest.
_TIMESTAMP_PLACEHOLDER = "{{RETURN_TIMESTAMP}}"

# MeF filing-status codes (IndividualReturnFilingStatusCd). Logical status value
# -> the single-digit MeF code. An unknown status is skipped (never guessed).
FILING_STATUS_CD: dict[str, str] = {
    "single": "1",
    "mfj": "2",     # married filing jointly
    "mfs": "3",     # married filing separately
    "hoh": "4",     # head of household
    "qss": "5",     # qualifying surviving spouse
}

# Logical 1040 key -> MeF-style ReturnData/IRS1040 element name. Only the keys
# present in ``inputs`` are written. Element names approximate the public IRS1040
# schema (see module docstring) and are NOT XSD-authoritative.
MEF_LINE_MAP: dict[str, str] = {
    # --- Income ---
    "wages_1a": "WagesAmt",
    "total_wages_1z": "WagesSalariesAndTipsAmt",
    "taxable_interest_2b": "TaxableInterestAmt",
    "ordinary_div_3b": "OrdinaryDividendsAmt",
    "ira_distrib_4b": "IRADistributionsTaxableAmt",
    "pensions_5b": "PensionsAnnuitiesTaxableAmt",
    "social_security_6b": "SocialSecurityBnftTaxableAmt",
    "capital_gain_7": "CapitalGainLossAmt",
    "additional_income_8": "AdditionalIncomeAmt",
    "total_income_9": "TotalIncomeAmt",
    "adjustments_10": "AdjustmentsToIncomeAmt",
    "agi_11": "AdjustedGrossIncomeAmt",
    # --- Deductions / taxable income / tax ---
    "std_or_itemized_12": "TotalItemizedOrStandardDedAmt",
    "qbi_deduction_13a": "QualifiedBusinessIncomeDedAmt",
    "total_deductions_14": "TotalDeductionsAmt",
    "taxable_income_15": "TaxableIncomeAmt",
    "tax_16": "TaxAmt",
    "other_taxes_23": "OtherTaxAmt",
    # total tax (line 24) — NO fillable box on the paper form, but a real MeF
    # element. The e-file package carries it; the AcroForm renderer skips it.
    "total_tax_24": "TotalTaxAmt",
    # --- Payments / withholding ---
    "federal_withholding_25a": "FormW2WithholdingTaxAmt",
    "federal_withholding_25b": "Form1099WithholdingTaxAmt",
    "total_withholding_25d": "WithholdingTaxAmt",
    "estimated_payments_26": "EstimatedTaxPaymentsAmt",
    "eic_27a": "EarnedIncomeCreditAmt",
    "actc_28": "AdditionalChildTaxCreditAmt",
    "total_payments_33": "TotalPaymentsAmt",
    # --- Refund / balance due ---
    "overpayment_34": "OverpaymentAmt",
    "refund_35a": "RefundAmt",
    "amount_you_owe_37": "BalanceDueAmt",
    "estimated_penalty_38": "EsmtdTaxPenaltyAmt",
}


# ---------------------------------------------------------------------------
# Package container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EfilePackage:
    """A generated e-file package — the artifact, not a transmission.

    Attributes
    ----------
    xml:
        The serialized, pretty-printed, well-formed return bytes (UTF-8 with an
        XML declaration).
    manifest:
        A small descriptor: ``form``, ``tax_year``, ``return_type``,
        ``efin_present`` (bool), ``line_count``, ``byte_size``, ``sha256`` (of the
        xml), ``transmitted`` (always ``False`` here), ``schema_profile`` and
        ``timestamp_templated``.
    content_type:
        Always ``"application/xml"``.
    warnings:
        Human-readable strings naming what a transmitter would reject (e.g. a
        missing filing status / AGI / total tax). The package still generates so
        the user can see exactly what is left to supply.
    """

    xml: bytes
    manifest: dict[str, Any]
    content_type: str = "application/xml"
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Provenance + validation
# ---------------------------------------------------------------------------


def assert_inputs_provenance(inputs: dict[str, Any]) -> None:
    """Refuse to build from any input that has a value but no ``source_ref``.

    The same anti-fabrication guard the work-products render path enforces: a
    non-blank figure with no provenance is a fabrication risk, so generation is
    refused (raises :class:`ValueError`). Blank/absent values are fine — they are
    simply omitted (conservative).
    """
    for key, rec in inputs.items():
        if not isinstance(rec, dict):
            raise ValueError(
                f"e-file input {key!r} is malformed (expected an object with "
                "value/source_ref/origin)"
            )
        value = rec.get("value")
        if value is None or str(value).strip() == "":
            continue  # blank is allowed — left out of the return
        if not rec.get("source_ref"):
            raise ValueError(
                f"e-file input {key!r} has a value but no source_ref — refusing "
                "to build a return figure without provenance"
            )


def validate_1040_inputs(inputs: dict[str, Any]) -> list[str]:
    """Return warnings for MISSING fields a transmitter would require.

    Does NOT invent anything and does NOT raise: a draft package can still be
    generated, but the caller is told exactly what is missing for the return to
    be acceptable to the IRS MeF system. Covers the four hard requirements:
    filing status, filer identity, adjusted gross income, and total tax.
    """
    warnings: list[str] = []

    def _present(key: str) -> bool:
        rec = inputs.get(key)
        return bool(rec) and str(rec.get("value", "")).strip() != ""

    if not _present("filing_status"):
        warnings.append(
            "Missing filing status — a transmittable 1040 requires a filing "
            "status (single / mfj / mfs / hoh / qss)."
        )
    elif str(inputs["filing_status"].get("value", "")).strip().lower() not in FILING_STATUS_CD:
        warnings.append(
            "Filing status value is not one of single/mfj/mfs/hoh/qss — no MeF "
            "filing-status code was written (not fabricated)."
        )
    if not (_present("first_name_mi") or _present("last_name")):
        warnings.append(
            "Missing filer identity (name) — the ReturnHeader has no filer name; "
            "a transmitter would reject the return."
        )
    if not _present("agi_11"):
        warnings.append(
            "Missing adjusted gross income (line 11 / AdjustedGrossIncomeAmt)."
        )
    if not (_present("total_tax_24") or _present("tax_16")):
        warnings.append(
            "Missing total tax (line 24 / TotalTaxAmt, or at least line 16 tax)."
        )
    return warnings


# ---------------------------------------------------------------------------
# Small XML helpers (no wall-clock, no randomness)
# ---------------------------------------------------------------------------


def _present_value(inputs: dict[str, Any], key: str) -> str | None:
    """Return the trimmed string value for ``key`` if present + non-blank."""
    rec = inputs.get(key)
    if not isinstance(rec, dict):
        return None
    value = rec.get("value")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pretty_xml(root: ET.Element) -> bytes:
    """Serialize ``root`` to pretty, well-formed UTF-8 bytes with a declaration."""
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _filer_name(inputs: dict[str, Any]) -> str | None:
    first = _present_value(inputs, "first_name_mi")
    last = _present_value(inputs, "last_name")
    parts = [p for p in (first, last) if p]
    return " ".join(parts) if parts else None


def _write_us_address(filer: ET.Element, inputs: dict[str, Any]) -> bool:
    addr = _present_value(inputs, "home_address")
    apt = _present_value(inputs, "apartment_no")
    city = _present_value(inputs, "city_town")
    state = _present_value(inputs, "state")
    zip_code = _present_value(inputs, "zip_code")
    if not any((addr, apt, city, state, zip_code)):
        return False
    us = ET.SubElement(filer, "USAddress")
    if addr:
        ET.SubElement(us, "AddressLine1Txt").text = addr
    if apt:
        ET.SubElement(us, "AddressLine2Txt").text = apt
    if city:
        ET.SubElement(us, "CityNm").text = city
    if state:
        ET.SubElement(us, "StateAbbreviationCd").text = state
    if zip_code:
        ET.SubElement(us, "ZIPCd").text = zip_code
    return True


def _write_1040_lines(form: ET.Element, inputs: dict[str, Any]) -> int:
    """Write the present 1040 line values into the IRS1040 element. Returns the
    number of line elements written (filing-status code counts as one line)."""
    count = 0
    status = _present_value(inputs, "filing_status")
    if status is not None:
        code = FILING_STATUS_CD.get(status.lower())
        if code:  # unknown status -> skip (validate_1040_inputs surfaces it)
            ET.SubElement(form, "IndividualReturnFilingStatusCd").text = code
            count += 1
    for key, element_name in MEF_LINE_MAP.items():
        value = _present_value(inputs, key)
        if value is None:
            continue
        ET.SubElement(form, element_name).text = value
        count += 1
    return count


# ---------------------------------------------------------------------------
# Federal 1040 package
# ---------------------------------------------------------------------------


def build_1040_efile_package(
    tax_year: int | None,
    inputs: dict[str, Any],
    *,
    efin: str | None = None,
    originator: dict[str, Any] | None = None,
    as_of: str | None = None,
) -> EfilePackage:
    """Build a MeF-style 1040 e-file package from provenance-bearing ``inputs``.

    Parameters
    ----------
    tax_year:
        The return's tax year (drives ``TaxYr`` + the non-authoritative
        ``returnVersion`` shape). May be ``None`` (templated).
    inputs:
        ``{logical_key: {"value": str, "source_ref": str, "origin": str}}`` — the
        WorkProduct.inputs map. Only present, non-blank values are written; every
        value with a non-blank ``value`` MUST carry a ``source_ref`` or generation
        is refused (raises :class:`ValueError`). Identity keys (``first_name_mi``,
        ``last_name``, address keys) flow into the ``ReturnHeader`` filer; the line
        keys in :data:`MEF_LINE_MAP` + ``filing_status`` flow into ``ReturnData``.
    efin:
        The org's IRS-issued EFIN. When supplied, an ``Originator`` block (EFIN +
        ``OriginatorTypeCd``) is written and ``manifest['efin_present']`` is True.
        When ``None`` the block is OMITTED — honest: there is no transmitter
        identity yet, and the package cannot actually be filed.
    originator:
        Optional extra originator detail, e.g. ``{"type": "ERO"}``. ``type``
        defaults to ``"ERO"`` when an EFIN is present.
    as_of:
        An ISO-8601 timestamp string for ``ReturnTs``. When omitted, a templated
        placeholder (``{{RETURN_TIMESTAMP}}``) is left for the transmitter to fill
        at send time. This function NEVER calls ``datetime.now()``.

    Returns
    -------
    EfilePackage
        ``{xml, manifest, content_type, warnings}``. ``warnings`` is
        :func:`validate_1040_inputs` output (missing required fields); the package
        still generates as a draft so the gaps are visible.
    """
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be a dict of {key: {value, source_ref, origin}}")

    assert_inputs_provenance(inputs)
    warnings = validate_1040_inputs(inputs)

    root = ET.Element("Return")
    # Literal xmlns attribute -> clean tags on write, real namespace on re-parse.
    root.set("xmlns", _MEF_NAMESPACE)
    root.set("returnVersion", f"{tax_year}v1.0" if tax_year else "{{RETURN_VERSION}}")

    # --- ReturnHeader ---
    header = ET.SubElement(root, "ReturnHeader")
    ts = ET.SubElement(header, "ReturnTs")
    ts.text = as_of.strip() if (as_of and str(as_of).strip()) else _TIMESTAMP_PLACEHOLDER
    ET.SubElement(header, "TaxYr").text = str(tax_year) if tax_year else "{{TAX_YEAR}}"
    ET.SubElement(header, "ReturnType").text = "IRS1040"

    filer = ET.SubElement(header, "Filer")
    name = _filer_name(inputs)
    if name:
        ET.SubElement(filer, "Name").text = name
    _write_us_address(filer, inputs)
    if len(filer) == 0:  # nothing known about the filer — don't emit an empty node
        header.remove(filer)

    if efin and str(efin).strip():
        orig = ET.SubElement(header, "Originator")
        ET.SubElement(orig, "EFIN").text = str(efin).strip()
        otype = (originator or {}).get("type") or "ERO"
        ET.SubElement(orig, "OriginatorTypeCd").text = str(otype)

    # --- ReturnData / IRS1040 ---
    data = ET.SubElement(root, "ReturnData")
    data.set("documentCnt", "1")
    form = ET.SubElement(data, "IRS1040")
    form.set("documentName", "IRS1040")
    line_count = _write_1040_lines(form, inputs)

    xml_bytes = _pretty_xml(root)
    manifest: dict[str, Any] = {
        "form": "1040",
        "tax_year": tax_year,
        "return_type": "IRS1040",
        "efin_present": bool(efin and str(efin).strip()),
        "line_count": line_count,
        "byte_size": len(xml_bytes),
        "sha256": hashlib.sha256(xml_bytes).hexdigest(),
        "transmitted": False,
        "schema_profile": "mef-style-1040 (approximate; not XSD-validated)",
        "timestamp_templated": not bool(as_of and str(as_of).strip()),
    }
    return EfilePackage(
        xml=xml_bytes,
        manifest=manifest,
        content_type="application/xml",
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# State package (stub — shape exists, schema not yet mapped)
# ---------------------------------------------------------------------------


def build_state_efile_package(
    state: str,
    tax_year: int | None,
    inputs: dict[str, Any] | None = None,
    *,
    as_of: str | None = None,
) -> EfilePackage:
    """A clearly-labeled state e-file STUB so the shape exists.

    Each state revenue department has its own Fed/State MeF schema and transmitter
    enrollment. This returns a structural placeholder artifact (no line values are
    emitted — that would require the per-state schema mapping) so callers can rely
    on a consistent :class:`EfilePackage` shape for a state today and the real
    mapping can land behind it later. ``inputs`` is accepted for signature parity
    but intentionally not transcribed.
    """
    state_code = (state or "XX").strip().upper() or "XX"

    root = ET.Element("StateReturn")
    root.set("xmlns", f"http://www.example-state.gov/efile/{state_code}")
    root.set("returnVersion", "stub")
    header = ET.SubElement(root, "StateReturnHeader")
    ts = ET.SubElement(header, "ReturnTs")
    ts.text = as_of.strip() if (as_of and str(as_of).strip()) else _TIMESTAMP_PLACEHOLDER
    ET.SubElement(header, "TaxYr").text = str(tax_year) if tax_year else "{{TAX_YEAR}}"
    ET.SubElement(header, "StateCd").text = state_code
    ET.SubElement(header, "Status").text = "not-schema-mapped"
    note = ET.SubElement(root, "Note")
    note.text = (
        f"State e-file package for {state_code} is not yet schema-mapped. This is "
        "a structural placeholder artifact; the per-state Fed/State MeF schema "
        "mapping and transmitter enrollment are required before it can be filed."
    )

    xml_bytes = _pretty_xml(root)
    warnings = [
        f"State e-file for {state_code} is a stub: the {state_code} return schema "
        "is not yet mapped; no line values are emitted.",
    ]
    manifest: dict[str, Any] = {
        "form": f"state-{state_code}",
        "tax_year": tax_year,
        "return_type": f"STATE-{state_code}",
        "efin_present": False,
        "line_count": 0,
        "byte_size": len(xml_bytes),
        "sha256": hashlib.sha256(xml_bytes).hexdigest(),
        "transmitted": False,
        "schema_profile": f"state-stub-{state_code} (NOT schema-mapped)",
        "timestamp_templated": not bool(as_of and str(as_of).strip()),
    }
    return EfilePackage(
        xml=xml_bytes,
        manifest=manifest,
        content_type="application/xml",
        warnings=warnings,
    )
