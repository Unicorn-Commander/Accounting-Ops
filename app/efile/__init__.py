"""IRS/state e-file PACKAGE generation — the artifact, with the wire gated.

This package turns a finished work product's provenance-bearing ``inputs`` into a
real, submission-shaped IRS Modernized e-File (MeF) style XML return + manifest
(:mod:`app.efile.mef`), and exposes a single honest transmit seam
(:mod:`app.efile.transmit`) that ALWAYS raises until the org is an authorized
transmitter (EFIN + A2A/IFA enrollment). The package is genuine; the wire is off.

Public API
----------
    from app.efile import (
        EfilePackage,
        build_1040_efile_package,
        build_state_efile_package,
        validate_1040_inputs,
        transmit,
        EfileTransmitGated,
    )
"""
from __future__ import annotations

from app.efile.mef import (
    FILING_STATUS_CD,
    MEF_LINE_MAP,
    EfilePackage,
    assert_inputs_provenance,
    build_1040_efile_package,
    build_state_efile_package,
    validate_1040_inputs,
)
from app.efile.transmit import EfileTransmitGated, transmit

__all__ = [
    "EfilePackage",
    "MEF_LINE_MAP",
    "FILING_STATUS_CD",
    "assert_inputs_provenance",
    "build_1040_efile_package",
    "build_state_efile_package",
    "validate_1040_inputs",
    "transmit",
    "EfileTransmitGated",
]
