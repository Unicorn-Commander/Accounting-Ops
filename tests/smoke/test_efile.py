"""Smoke tests for IRS/state e-file PACKAGE generation + the gated transmit wire.

Pure unit tests — no Postgres / Keycloak / HTTP-to-a-real-app. They assert:

  * a 1040 package is well-formed XML (ElementTree parses it) and carries exactly
    the present lines (a supplied line appears; an unsupplied line does NOT);
  * the package is deterministic + carries a manifest (sha256, line_count, …) and
    a templated timestamp (no wall-clock);
  * ``validate_1040_inputs`` flags MISSING required fields without inventing them;
  * the provenance guard refuses a value with no ``source_ref``;
  * ``transmit`` ALWAYS raises ``EfileTransmitGated`` — by default AND even when
    the policy enables the channel + a real EFIN + the env flag are all faked
    (the dormant-wire invariant);
  * the state package is a clearly-labeled stub;
  * the API router imports and exposes the documented path (so the orchestrator
    can register it).
"""
from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET

import pytest

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
from app.engagement.policy import EngagementPolicyData
from app.models.ledger import ReviewMode


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A realistic, provenance-bearing 1040 inputs map. Identity + filing status +
# AGI + total tax are present (a transmitter's hard requirements); a refund line
# is intentionally ABSENT so we can prove it is not invented.
_INPUTS = {
    "first_name_mi": {"value": "Aaron J", "source_ref": "intake#1", "origin": "user_entered"},
    "last_name": {"value": "Stransky", "source_ref": "intake#1", "origin": "user_entered"},
    "home_address": {"value": "1 Unicorn Way", "source_ref": "intake#1", "origin": "user_entered"},
    "city_town": {"value": "Greenville", "source_ref": "intake#1", "origin": "user_entered"},
    "state": {"value": "SC", "source_ref": "intake#1", "origin": "user_entered"},
    "zip_code": {"value": "29601", "source_ref": "intake#1", "origin": "user_entered"},
    "filing_status": {"value": "mfj", "source_ref": "intake#1", "origin": "user_entered"},
    "wages_1a": {"value": "85000", "source_ref": "w2-2025-acme", "origin": "document"},
    "agi_11": {"value": "85000", "source_ref": "calc#1", "origin": "computed"},
    "total_tax_24": {"value": "9200", "source_ref": "calc#1", "origin": "computed"},
    "federal_withholding_25a": {"value": "9500", "source_ref": "w2-2025-acme", "origin": "document"},
}


def _localnames(xml_bytes: bytes) -> list[str]:
    """Parse + return every element's local (namespace-stripped) tag name."""
    root = ET.fromstring(xml_bytes)  # raises on malformed XML
    return [el.tag.split("}")[-1] for el in root.iter()]


def _policy(*, channels: dict | None = None, kill_switch: bool = False) -> EngagementPolicyData:
    return EngagementPolicyData(
        workspace_id=uuid.uuid4(),
        review_mode=ReviewMode.PRO_REQUIRED,
        has_licensed_pro=False,
        prefer_local_inference=False,
        submission_channels=channels or {},
        rail_kill_switch=kill_switch,
    )


# ---------------------------------------------------------------------------
# A. Package generation
# ---------------------------------------------------------------------------


class TestBuild1040Package:
    def test_returns_efile_package_shape(self):
        pkg = build_1040_efile_package(2025, _INPUTS, efin="123456")
        assert isinstance(pkg, EfilePackage)
        assert pkg.content_type == "application/xml"
        assert isinstance(pkg.xml, bytes)
        assert pkg.xml.startswith(b"<?xml")

    def test_xml_is_well_formed(self):
        pkg = build_1040_efile_package(2025, _INPUTS)
        # ET.fromstring raises if it is not well-formed; reaching the assert is
        # the proof.
        names = _localnames(pkg.xml)
        assert names[0] == "Return"
        assert "ReturnHeader" in names
        assert "ReturnData" in names
        assert "IRS1040" in names
        assert "ReturnType" in names

    def test_present_line_appears_absent_line_does_not(self):
        pkg = build_1040_efile_package(2025, _INPUTS)
        names = _localnames(pkg.xml)
        # Present: AGI (agi_11) + total tax (total_tax_24) + W-2 withholding.
        assert "AdjustedGrossIncomeAmt" in names
        assert "TotalTaxAmt" in names
        assert "FormW2WithholdingTaxAmt" in names
        # Absent input -> absent element (never fabricated). No refund supplied.
        assert "RefundAmt" not in names
        assert "OverpaymentAmt" not in names
        assert "EarnedIncomeCreditAmt" not in names

    def test_present_value_is_carried_verbatim(self):
        pkg = build_1040_efile_package(2025, _INPUTS)
        root = ET.fromstring(pkg.xml)
        agi = [el for el in root.iter() if el.tag.split("}")[-1] == "AdjustedGrossIncomeAmt"]
        assert agi and agi[0].text == "85000"

    def test_filing_status_maps_to_mef_code(self):
        pkg = build_1040_efile_package(2025, _INPUTS)
        root = ET.fromstring(pkg.xml)
        fs = [el for el in root.iter() if el.tag.split("}")[-1] == "IndividualReturnFilingStatusCd"]
        assert fs and fs[0].text == FILING_STATUS_CD["mfj"] == "2"

    def test_total_tax_carried_even_though_paper_form_omits_it(self):
        # The AcroForm 1040 has no fillable box for line 24, but the e-file XML
        # carries TotalTaxAmt. This is the e-file-vs-paper distinction.
        assert "total_tax_24" in MEF_LINE_MAP
        pkg = build_1040_efile_package(2025, {
            "total_tax_24": {"value": "9200", "source_ref": "calc#1", "origin": "computed"},
        })
        assert "TotalTaxAmt" in _localnames(pkg.xml)

    def test_manifest_fields(self):
        pkg = build_1040_efile_package(2025, _INPUTS, efin="123456")
        m = pkg.manifest
        assert m["form"] == "1040"
        assert m["tax_year"] == 2025
        assert m["return_type"] == "IRS1040"
        assert m["efin_present"] is True
        assert m["line_count"] >= 4  # filing status + agi + total tax + withholding
        assert m["transmitted"] is False
        assert len(m["sha256"]) == 64
        assert m["byte_size"] == len(pkg.xml)
        assert m["timestamp_templated"] is True

    def test_efin_absent_omits_originator_and_flags_manifest(self):
        pkg = build_1040_efile_package(2025, _INPUTS)  # no efin
        assert pkg.manifest["efin_present"] is False
        assert "Originator" not in _localnames(pkg.xml)
        assert "EFIN" not in _localnames(pkg.xml)

    def test_efin_present_writes_originator_block(self):
        pkg = build_1040_efile_package(2025, _INPUTS, efin="123456", originator={"type": "ERO"})
        root = ET.fromstring(pkg.xml)
        efin = [el for el in root.iter() if el.tag.split("}")[-1] == "EFIN"]
        otype = [el for el in root.iter() if el.tag.split("}")[-1] == "OriginatorTypeCd"]
        assert efin and efin[0].text == "123456"
        assert otype and otype[0].text == "ERO"

    def test_timestamp_templated_by_default_no_wallclock(self):
        pkg = build_1040_efile_package(2025, _INPUTS)
        root = ET.fromstring(pkg.xml)
        ts = [el for el in root.iter() if el.tag.split("}")[-1] == "ReturnTs"]
        assert ts and ts[0].text == "{{RETURN_TIMESTAMP}}"

    def test_as_of_timestamp_is_used_when_supplied(self):
        pkg = build_1040_efile_package(2025, _INPUTS, as_of="2026-04-15T12:00:00Z")
        root = ET.fromstring(pkg.xml)
        ts = [el for el in root.iter() if el.tag.split("}")[-1] == "ReturnTs"]
        assert ts and ts[0].text == "2026-04-15T12:00:00Z"
        assert pkg.manifest["timestamp_templated"] is False

    def test_generation_is_deterministic(self):
        a = build_1040_efile_package(2025, _INPUTS, efin="123456")
        b = build_1040_efile_package(2025, _INPUTS, efin="123456")
        assert a.xml == b.xml
        assert a.manifest["sha256"] == b.manifest["sha256"]


# ---------------------------------------------------------------------------
# B. Validation (missing-field warnings) + provenance guard
# ---------------------------------------------------------------------------


class TestValidation:
    def test_flags_all_missing_required_fields(self):
        warnings = validate_1040_inputs({})
        blob = " ".join(warnings).lower()
        assert any("filing status" in w.lower() for w in warnings)
        assert any("identity" in w.lower() or "name" in w.lower() for w in warnings)
        assert "adjusted gross income" in blob
        assert "total tax" in blob

    def test_complete_inputs_have_no_required_warnings(self):
        assert validate_1040_inputs(_INPUTS) == []

    def test_unknown_filing_status_is_flagged_not_fabricated(self):
        inputs = {
            "filing_status": {"value": "martian", "source_ref": "x", "origin": "user_entered"},
            "first_name_mi": {"value": "A", "source_ref": "x", "origin": "user_entered"},
            "agi_11": {"value": "1", "source_ref": "x", "origin": "computed"},
            "tax_16": {"value": "0", "source_ref": "x", "origin": "computed"},
        }
        warnings = validate_1040_inputs(inputs)
        assert any("not one of" in w for w in warnings)
        # And no MeF status code is written for the unknown status.
        pkg = build_1040_efile_package(2025, inputs)
        assert "IndividualReturnFilingStatusCd" not in _localnames(pkg.xml)

    def test_warnings_surface_on_the_package_but_still_generates(self):
        sparse = {"wages_1a": {"value": "100", "source_ref": "w2", "origin": "document"}}
        pkg = build_1040_efile_package(2025, sparse)
        assert pkg.warnings  # what a transmitter would reject
        assert pkg.xml.startswith(b"<?xml")  # still a draft artifact

    def test_provenance_guard_refuses_value_without_source_ref(self):
        bad = {"agi_11": {"value": "85000", "origin": "computed"}}  # no source_ref
        with pytest.raises(ValueError, match="source_ref"):
            assert_inputs_provenance(bad)
        with pytest.raises(ValueError, match="source_ref"):
            build_1040_efile_package(2025, bad)

    def test_blank_value_without_source_ref_is_allowed(self):
        ok = {"agi_11": {"value": "", "origin": "computed"}}  # blank -> skipped
        assert_inputs_provenance(ok)  # does not raise
        pkg = build_1040_efile_package(2025, ok)
        assert "AdjustedGrossIncomeAmt" not in _localnames(pkg.xml)


# ---------------------------------------------------------------------------
# C. State stub
# ---------------------------------------------------------------------------


class TestStatePackage:
    def test_state_stub_is_labeled_and_well_formed(self):
        pkg = build_state_efile_package("SC", 2025)
        names = _localnames(pkg.xml)
        assert names[0] == "StateReturn"
        assert "Status" in names
        root = ET.fromstring(pkg.xml)
        status = [el for el in root.iter() if el.tag.split("}")[-1] == "Status"]
        assert status and status[0].text == "not-schema-mapped"
        assert pkg.manifest["line_count"] == 0
        assert pkg.manifest["form"] == "state-SC"
        assert pkg.warnings and "stub" in pkg.warnings[0].lower()


# ---------------------------------------------------------------------------
# D. The gated transmit wire — ALWAYS raises
# ---------------------------------------------------------------------------


class TestTransmitGated:
    def _pkg(self) -> EfilePackage:
        return build_1040_efile_package(2025, _INPUTS, efin="123456")

    def test_raises_by_default_no_policy_no_efin_no_flag(self, monkeypatch):
        monkeypatch.delenv("EFILE_TRANSMIT_ENABLED", raising=False)
        with pytest.raises(EfileTransmitGated) as ei:
            transmit(self._pkg(), channel="efile_irs", efin=None, policy=None)
        assert ei.value.reason == "policy_disabled"

    def test_raises_when_policy_enabled_but_no_efin(self, monkeypatch):
        monkeypatch.delenv("EFILE_TRANSMIT_ENABLED", raising=False)
        policy = _policy(channels={"efile_irs": True})
        with pytest.raises(EfileTransmitGated) as ei:
            transmit(self._pkg(), channel="efile_irs", efin="", policy=policy)
        assert ei.value.reason == "no_efin"

    def test_raises_when_policy_and_efin_but_env_flag_off(self, monkeypatch):
        monkeypatch.delenv("EFILE_TRANSMIT_ENABLED", raising=False)
        policy = _policy(channels={"efile_irs": True})
        with pytest.raises(EfileTransmitGated) as ei:
            transmit(self._pkg(), channel="efile_irs", efin="123456", policy=policy)
        assert ei.value.reason == "env_disabled"

    def test_still_raises_even_when_all_three_gates_faked(self, monkeypatch):
        # The dormant invariant: policy enables the channel, a real EFIN is given,
        # AND the env flag is on — and it STILL raises (the wire is not implemented).
        monkeypatch.setenv("EFILE_TRANSMIT_ENABLED", "true")
        policy = _policy(channels={"efile_irs": True})
        with pytest.raises(EfileTransmitGated) as ei:
            transmit(self._pkg(), channel="efile_irs", efin="123456", policy=policy)
        assert ei.value.reason == "not_implemented"
        assert "not yet enabled" in str(ei.value)

    def test_kill_switch_blocks_even_with_channel_enabled(self, monkeypatch):
        monkeypatch.setenv("EFILE_TRANSMIT_ENABLED", "true")
        # channel marked enabled but the rail kill-switch is on -> channel_enabled
        # returns False -> policy gate stops it.
        policy = _policy(channels={"efile_irs": True}, kill_switch=True)
        with pytest.raises(EfileTransmitGated) as ei:
            transmit(self._pkg(), channel="efile_irs", efin="123456", policy=policy)
        assert ei.value.reason == "policy_disabled"


# ---------------------------------------------------------------------------
# E. API router shape (so the orchestrator can register it)
# ---------------------------------------------------------------------------


class TestRouterShape:
    def test_router_imports_and_exposes_efile_package_paths(self):
        import app.api.efile as efile_api

        assert efile_api.router is not None
        paths = {r.path for r in efile_api.router.routes}
        assert (
            "/api/workspaces/{workspace_id}/entities/{entity_id}/work-products/{wp_id}/efile-package"
            in paths
        )
        assert (
            "/api/workspaces/{workspace_id}/entities/{entity_id}/work-products/{wp_id}/efile-package.xml"
            in paths
        )
