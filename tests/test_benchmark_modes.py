from __future__ import annotations

import json

import pytest

from app import db
from benchmarks.modes import (
    CURRENT_REPRICING,
    HISTORICAL_REPRODUCTION,
    _db_source_observations,
    classify_price_error,
    compare_mode_metrics,
    eligible_observations,
    evaluate_temporal_modes,
    infer_benchmark_date,
    make_mode_spec,
    select_mode_observation,
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


def test_db_source_observations_prefers_active_ex_vat_observations(isolated_db) -> None:
    """DB-backed temporal reports must follow the immutable material history."""

    with db.db_session() as conn:
        source_current = conn.execute(
            """
            INSERT INTO source_files(
                filename, storage_key, sha256, content_sha256, extension,
                size_bytes, detected_type, processing_status, lifecycle_status,
                uploaded_at
            ) VALUES ('current.xlsx', 'unused', 'current', 'current', '.xlsx',
                      1, 'SUPPLIER_PRICE', 'COMPLETED', 'ACTIVE', datetime('now'))
            """
        ).lastrowid
        source_old = conn.execute(
            """
            INSERT INTO source_files(
                filename, storage_key, sha256, content_sha256, extension,
                size_bytes, detected_type, processing_status, lifecycle_status,
                uploaded_at
            ) VALUES ('old.xlsx', 'unused', 'old', 'old', '.xlsx',
                      1, 'SUPPLIER_PRICE', 'COMPLETED', 'ACTIVE', datetime('now'))
            """
        ).lastrowid
        source_archived = conn.execute(
            """
            INSERT INTO source_files(
                filename, storage_key, sha256, content_sha256, extension,
                size_bytes, detected_type, processing_status, lifecycle_status,
                uploaded_at
            ) VALUES ('archived.xlsx', 'unused', 'archived', 'archived', '.xlsx',
                      1, 'SUPPLIER_PRICE', 'COMPLETED', 'ARCHIVED', datetime('now'))
            """
        ).lastrowid
        product_id = int(
            conn.execute(
                """
                INSERT INTO products(
                    canonical_key, normalized_name, category, unit,
                    technical_attributes_json, aliases_json, created_at
                ) VALUES ('db-observation-product', 'Cable DB observation',
                          'cable', 'm', '{}', '[]', datetime('now'))
                """
            ).lastrowid
        )
        # Legacy row is retained but must not shadow the newer immutable source.
        conn.execute(
            """
            INSERT INTO product_prices(
                product_id, source_file_id, supplier, net_price, tax_mode,
                effective_date, created_at
            ) VALUES (?, ?, 'Historical quotation', 100, 'ex_vat',
                      '2025-01-01', datetime('now'))
            """,
            (product_id, source_old),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date, created_at
            ) VALUES (?, ?, 'Current supplier', 'supplier_list', 200,
                      'ex_vat', 'net', '2026-01-01', datetime('now'))
            """,
            (product_id, source_current),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date, created_at
            ) VALUES (?, ?, 'Archived supplier', 'supplier_list', 999,
                      'ex_vat', 'net', '2026-02-01', datetime('now'))
            """,
            (product_id, source_archived),
        )
        observations = _db_source_observations(
            conn, [{"matched_product_id": product_id}]
        )

    selected = observations[("material", product_id)]
    assert [row["net_price"] for row in selected] == [200]
    assert selected[0]["observation_type"] == "supplier_list"


def test_mode_selector_prefers_explicit_tier_and_normalizes_tax_basis() -> None:
    observations = [
        {
            "id": 1,
            "net_price": 100,
            "observation_type": "supplier_list",
            "tax_mode": "EX VAT",
            "price_basis": "NET PRICE",
            "effective_date": "2026-01-01",
            "context_json": json.dumps({"selected": True}),
        },
        {
            "id": 2,
            "net_price": 70,
            "observation_type": "supplier_discounted",
            "tax_mode": "ex_vat",
            "price_basis": "net",
            "effective_date": "2026-01-01",
            "context_json": json.dumps({"selected": False}),
        },
        {
            "id": 3,
            "net_price": 108,
            "observation_type": "supplier_list",
            "tax_mode": "INC-VAT",
            "price_basis": "gross",
            "effective_date": "2026-01-01",
            "context_json": json.dumps({"selected": False}),
        },
    ]
    value, selected = select_mode_observation(observations, side="material")
    assert value == 100
    assert selected is not None
    assert selected["id"] == 1


def test_db_legacy_price_fallback_prefers_ex_vat_rows(isolated_db) -> None:
    with db.db_session() as conn:
        source_id = conn.execute(
            """
            INSERT INTO source_files(
                filename, storage_key, sha256, content_sha256, extension,
                size_bytes, detected_type, processing_status, lifecycle_status,
                uploaded_at
            ) VALUES ('legacy-prices.xlsx', 'unused', 'legacy-prices',
                      'legacy-prices', '.xlsx', 1, 'SUPPLIER_PRICE',
                      'COMPLETED', 'ACTIVE', datetime('now'))
            """
        ).lastrowid
        product_id = int(
            conn.execute(
                """
                INSERT INTO products(
                    canonical_key, normalized_name, category, unit,
                    technical_attributes_json, aliases_json, created_at
                ) VALUES ('legacy-price-product', 'Legacy price product',
                          'cable', 'm', '{}', '[]', datetime('now'))
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO product_prices(
                product_id, source_file_id, supplier, net_price, tax_mode,
                effective_date, created_at
            ) VALUES (?, ?, 'Supplier', 100, 'EX VAT', '2026-01-01', datetime('now'))
            """,
            (product_id, source_id),
        )
        conn.execute(
            """
            INSERT INTO product_prices(
                product_id, source_file_id, supplier, net_price, tax_mode,
                effective_date, created_at
            ) VALUES (?, ?, 'Supplier', 108, 'INC-VAT', '2026-01-01', datetime('now'))
            """,
            (product_id, source_id),
        )
        rows = _db_source_observations(
            conn, [{"matched_product_id": product_id}]
        )

    selected = rows[("material", product_id)]
    assert [item["net_price"] for item in selected] == [100]
