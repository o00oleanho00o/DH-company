from __future__ import annotations

import json

import pytest

from benchmarks.modes import (
    CURRENT_REPRICING,
    HISTORICAL_REPRODUCTION,
    classify_price_error,
    compare_mode_metrics,
    eligible_observations,
    evaluate_temporal_modes,
    infer_benchmark_date,
    make_mode_spec,
    temporal_observation_status,
)


def _row(
    row_id: int,
    *,
    material_price: float | None = None,
    labor_price: float | None = None,
    matched_product_id: int | None = None,
    matched_labor_item_id: int | None = None,
    material_source: dict | None = None,
    labor_source: dict | None = None,
    confidence: float = 0.95,
) -> dict:
    return {
        "id": row_id,
        "raw_description": "Cáp Cu/PVC 1x10",
        "normalized_description": "cap cu pvc 1x10",
        "unit": "m",
        "quantity": 1,
        "material_price": material_price,
        "labor_price": labor_price,
        "matched_product_id": matched_product_id,
        "matched_labor_item_id": matched_labor_item_id,
        "material_confidence": confidence,
        "labor_confidence": confidence,
        "material_source_json": json.dumps(material_source or {}),
        "labor_source_json": json.dumps(labor_source or {}),
        "alternatives_json": "{}",
        "status": "AUTO_APPROVED",
    }


def test_mode_date_inference_requires_unambiguous_date() -> None:
    explicit = infer_benchmark_date(explicit="2024-01-29", filename="DH290124.xlsx")
    assert explicit["date"] == "2024-01-29"
    assert explicit["source"] == "explicit"
    assert explicit["uncertain"] is False

    compact = infer_benchmark_date(filename="BOQ-DH290124.xlsx")
    assert compact["date"] is None
    assert compact["uncertain"] is True

    spec = make_mode_spec(
        HISTORICAL_REPRODUCTION,
        filename="BOQ-DH290124.xlsx",
    )
    assert spec.as_of is None
    assert spec.temporal_semantics == "undated_temporal_semantics"

    current = make_mode_spec(
        CURRENT_REPRICING,
        project={"quotation_date": "2024-01-29"},
        current_date="2026-09-05",
    )
    assert current.as_of == "2026-09-05"
    assert current.as_of_source == "runtime.current_date"


def test_future_observations_are_excluded_only_by_temporal_cutoff() -> None:
    observations = [
        {"id": 1, "rate": 100, "effective_date": "2024-01-01"},
        {"id": 2, "rate": 200, "effective_date": "2026-01-01"},
        {"id": 3, "rate": 300},
    ]
    historical = make_mode_spec(
        HISTORICAL_REPRODUCTION,
        as_of="2024-12-31",
        strict_dates=True,
    )
    assert temporal_observation_status(observations[0], historical) == "eligible"
    assert temporal_observation_status(observations[1], historical) == "future_source"
    assert temporal_observation_status(observations[2], historical) == "undated_excluded"
    eligible, counts = eligible_observations(observations, historical)
    assert [row["id"] for row in eligible] == [1]
    assert counts == {"eligible": 1, "future_source": 1, "undated_excluded": 1}


def test_evaluate_temporal_modes_separates_historical_and_current_metrics() -> None:
    rows = [
        _row(
            1,
            material_price=100,
            labor_price=40,
            matched_product_id=10,
            matched_labor_item_id=20,
            material_source={"effective_date": "2024-01-01", "source_type": "supplier_price"},
            labor_source={"effective_date": "2024-01-01", "source_type": "historical_boq"},
        ),
        _row(
            2,
            material_price=None,
            labor_price=None,
            matched_product_id=10,
            matched_labor_item_id=20,
            material_source={},
            labor_source={},
        ),
    ]
    source_observations = {
        ("material", 10): [
            {"id": 1, "net_price": 120, "effective_date": "2026-01-01"},
            {"id": 2, "net_price": 100, "effective_date": "2024-01-01"},
        ],
        ("labor", 20): [
            {"id": 3, "rate": 50, "effective_date": "2026-01-01"},
            {"id": 4, "rate": 40, "effective_date": "2024-01-01"},
        ],
    }
    truth = {
        1: {"material_price": 100, "labor_price": 40},
        2: {"material_price": 100, "labor_price": 40},
    }
    report = evaluate_temporal_modes(
        rows,
        ground_truth=truth,
        source_observations=source_observations,
        historical_as_of="2024-12-31",
        current_as_of="2026-09-05",
        strict_historical_dates=True,
    )
    historical = report["historical_reproduction"]
    current = report["current_repricing"]
    assert historical["mode"] == HISTORICAL_REPRODUCTION
    assert current["mode"] == CURRENT_REPRICING
    assert historical["material"]["future_observations_excluded"] >= 1
    assert historical["material"]["temporal_excluded_items"] == 0
    assert historical["material"]["exact_price_accuracy"] == 1.0
    assert historical["labor"]["exact_price_accuracy"] == 1.0
    assert historical["material"]["predicted_items"] == 2
    assert historical["labor"]["predicted_items"] == 2
    assert current["material"]["predicted_items"] == 2
    assert current["labor"]["predicted_items"] == 2
    assert current["material"]["mean_relative_error"] == pytest.approx(0.2)
    assert current["material"]["current_repricing_relative_drift"] == pytest.approx(0.2)
    assert historical["material"]["current_repricing_relative_drift"] is None
    assert report["comparison"]["material"]["current_repricing_coverage"] == 1.0


def test_price_error_root_causes_are_evidence_backed() -> None:
    missing_discount = classify_price_error(
        predicted=110,
        actual=100,
        source={
            "source_type": "supplier_price",
            "list_price": 110,
            "discount": 0,
            "tax_mode": "ex_vat",
        },
    )
    assert missing_discount["root_cause"] == "MISSING_DISCOUNT"
    assert missing_discount["confidence"] == "medium"

    vat = classify_price_error(
        predicted=110,
        actual=100,
        source={"source_type": "supplier_price", "tax_mode": "inc_vat"},
    )
    assert vat["root_cause"] == "VAT_BASIS_MISMATCH"

    alternative = classify_price_error(
        predicted=200,
        actual=100,
        source={"source_type": "supplier_price", "tax_mode": "ex_vat"},
        alternatives=[{"id": 7, "price": 105, "name": "near match"}],
    )
    assert alternative["root_cause"] == "SOURCE_PRIORITY_PROBLEM"

    future = classify_price_error(
        predicted=200,
        actual=100,
        source={"source_type": "supplier_price", "effective_date": "2026-01-01"},
        target_date="2024-12-31",
    )
    assert future["root_cause"] == "FUTURE_SOURCE_USED"


def test_compare_mode_metrics_reports_current_minus_historical() -> None:
    historical = {
        "mode": HISTORICAL_REPRODUCTION,
        "priceable_rows": 10,
        "mode_spec": {"as_of": "2024-01-01"},
        "material": {
            "exact_price_accuracy": 0.2,
            "coverage": 0.3,
            "capture_rate": 0.5,
            "mean_relative_error": 0.4,
        },
        "labor": {
            "exact_price_accuracy": 0.1,
            "coverage": 0.2,
            "capture_rate": 0.4,
            "mean_relative_error": 0.3,
        },
        "temporal": {"future_sources_excluded": 4, "uncertain": False},
    }
    current = {
        "mode": CURRENT_REPRICING,
        "priceable_rows": 10,
        "mode_spec": {"as_of": "2026-09-05"},
        "material": {
            "exact_price_accuracy": 0.0,
            "coverage": 0.8,
            "capture_rate": 0.9,
            "mean_relative_error": 0.2,
        },
        "labor": {
            "exact_price_accuracy": 0.0,
            "coverage": 0.7,
            "capture_rate": 0.8,
            "mean_relative_error": 0.1,
        },
        "temporal": {"future_sources_excluded": 0, "uncertain": False},
    }
    comparison = compare_mode_metrics(historical, current)
    assert comparison["material"]["capture_rate_delta"] == pytest.approx(0.4)
    assert comparison["material"]["coverage_delta"] == pytest.approx(0.5)
    assert comparison["labor"]["relative_error_delta"] == pytest.approx(-0.2)
    assert comparison["temporal"]["historical_future_excluded"] == 4
