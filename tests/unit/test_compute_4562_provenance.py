"""Anti-fabrication tests for the Form 4562 computed path.

The platform's core promise is that no figure reaches a form without a
``source_ref``. These tests run the engine output through the REAL guards
(``work_product_service.normalize_provenance_fields`` and
``api.work_products._assert_provenance``) rather than re-implementing the rule,
so a change to either guard is caught here.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.api.work_products import _assert_provenance
from app.exports.form_prefill import WatermarkMode
from app.tax.compute_4562 import compute_4562
from app.tax.work_product_service import normalize_provenance_fields


def sample_assets() -> list[dict]:
    return [
        {
            "asset_id": "lathe",
            "description": "CNC lathe",
            "placed_in_service": date(2024, 3, 1),
            "cost": 100_000,
            "recovery_period_years": "7",
            "section_179_elected": 20_000,
            "source_ref": "fixed_asset:11111111-1111-1111-1111-111111111111",
        },
        {
            "asset_id": "van",
            "description": "Delivery van",
            "placed_in_service": date(2024, 5, 1),
            "cost": 50_000,
            "recovery_period_years": "5",
            "is_listed_property": True,
            "listed_property_type": "heavy_vehicle",
            "business_use_pct": 80,
            "source_ref": "fixed_asset:22222222-2222-2222-2222-222222222222",
        },
        {
            "asset_id": "office",
            "description": "Office building",
            "placed_in_service": date(2024, 2, 15),
            "cost": 900_000,
            "recovery_period_years": "39",
            "source_ref": "fixed_asset:33333333-3333-3333-3333-333333333333",
        },
    ]


@pytest.fixture(scope="module")
def payload() -> dict:
    return compute_4562(
        2024,
        sample_assets(),
        business_taxable_income=500_000,
        name="Acme Manufacturing Inc",
        business_activity="Machining",
    )


def test_every_emitted_figure_carries_a_source_ref(payload: dict) -> None:
    """The whole anti-fabrication contract, asserted field by field."""
    inputs = payload["inputs"]
    assert inputs, "expected the computed path to emit form inputs"
    for key, record in inputs.items():
        assert isinstance(record, dict), f"{key} is not a provenance record"
        assert str(record.get("value", "")).strip() != "", f"{key} has a blank value"
        assert record.get("source_ref"), f"{key} has a value but NO source_ref"
        assert record.get("origin"), f"{key} has no origin"


def test_inputs_pass_the_real_storage_guard(payload: dict) -> None:
    """normalize_provenance_fields is what the MCP + REST create paths run."""
    normalized = normalize_provenance_fields(payload["inputs"])
    assert set(normalized) == set(payload["inputs"])


def test_inputs_pass_the_real_render_guard(payload: dict) -> None:
    """_assert_provenance is what the PDF serve route runs; it must not raise."""
    _assert_provenance(payload["inputs"])


def test_computed_lines_are_tagged_as_engine_computed(payload: dict) -> None:
    inputs = payload["inputs"]
    expected_ref = "computed:form4562@2024"
    for line in ("1", "2", "3", "5", "12", "22"):
        assert inputs[line]["source_ref"] == expected_ref
        assert inputs[line]["origin"] == "computed"


def test_asset_identity_lines_carry_the_asset_source_ref(payload: dict) -> None:
    """A figure traceable to an asset row must name that row, not the engine."""
    inputs = payload["inputs"]
    listed = [v for k, v in inputs.items() if k.startswith("listed26_1_")]
    assert listed, "expected the van on Part V line 26"
    assert any(v["source_ref"].startswith("fixed_asset:") for v in listed)


def test_identifying_number_is_never_emitted(payload: dict) -> None:
    """EIN/SSN is left for the human — it must not even enter the inputs map."""
    assert "identifying_number" not in payload["inputs"]


def test_a_figure_without_provenance_is_rejected_by_the_guard() -> None:
    """Sanity-check the guard actually bites (so the tests above mean something)."""
    with pytest.raises(ValueError, match="source_ref"):
        _assert_provenance({"22": {"value": "12345", "origin": "computed"}})
    with pytest.raises(ValueError, match="source_ref"):
        normalize_provenance_fields({"22": {"value": "12345"}})


def test_provenance_block_carries_citations_and_assumptions(payload: dict) -> None:
    prov = payload["provenance"]
    assert prov["form"] == "4562"
    assert prov["tax_year"] == 2024
    assert prov["form_revision"] == "2022_2024"
    # Every year-specific constant used must name its authority.
    assert "Pub 946" in prov["citations"]["section_179"]
    assert "Pub 946" in prov["citations"]["bonus_168k"]
    assert "assumptions" in prov


def test_computed_summary_matches_the_form_lines(payload: dict) -> None:
    prov_computed = payload["provenance"]["computed"]
    inputs = payload["inputs"]
    assert prov_computed["total_depreciation"] == inputs["22"]["value"]
    assert prov_computed["section_179"] == inputs["12"]["value"]


def test_end_to_end_compute_then_render(payload: dict) -> None:
    """The full path: assets -> engine -> provenance guard -> filled PDF."""
    from app.exports.f4562_fill import render_4562

    _assert_provenance(payload["inputs"])
    pdf_bytes, content_type = render_4562(2024, WatermarkMode.DRAFT, payload["inputs"])
    assert content_type == "application/pdf"
    assert pdf_bytes.startswith(b"%PDF")
    # The §179 deduction actually reached the page.
    text = pdf_bytes.decode("latin-1", errors="ignore")
    assert inputs_value(payload, "12") in text


def inputs_value(payload: dict, key: str) -> str:
    return payload["inputs"][key]["value"]


def test_unsupported_year_raises_through_the_computed_path() -> None:
    with pytest.raises(ValueError, match="No depreciation constants on file"):
        compute_4562(2019, sample_assets(), business_taxable_income=100_000)


def test_year_with_constants_but_no_published_form_raises() -> None:
    """TY2026 constants exist, but the IRS has not published a 2026 Form 4562 —
    so the computed path must refuse rather than render onto the 2025 layout."""
    assets = [
        {
            "asset_id": "m", "description": "Machine",
            "placed_in_service": date(2026, 3, 1), "acquired_on": date(2026, 1, 5),
            "cost": 50_000, "recovery_period_years": "7",
        }
    ]
    with pytest.raises(ValueError, match="No verified Form 4562 field map"):
        compute_4562(2026, assets)
