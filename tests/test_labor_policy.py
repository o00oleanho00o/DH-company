from __future__ import annotations

import json

import pytest

from app import db
from app.labor_policy import (
    labor_rate_statistics,
    normalise_labor_policy,
    select_labor_rate,
)
from app.pricing import Candidate, _status_for, choose_labor_rate


def _row(
    rate: float,
    when: str,
    *,
    row_id: int,
    source_type: str = "historical_boq",
    project_id: int | None = None,
    confidence: float = 1.0,
) -> dict[str, object]:
    return {
        "id": row_id,
        "rate": rate,
        "effective_date": when,
        "source_project_id": project_id,
        "source_file_id": 10 + row_id,
        "source_sheet_id": 20 + row_id,
        "source_row_id": 30 + row_id,
        "filename": f"project-{row_id}.xlsx",
        "sheet_name": "BOQ",
        "row_no": row_id,
        "project_name": f"Project {row_id}",
        "confidence": confidence,
        "policy_json": json.dumps({"source_type": source_type}),
    }


def test_normalise_policy_accepts_percent_adjustment_and_aliases() -> None:
    policy = normalise_labor_policy(
        {"strategy": "median last 3", "adjustment_pct": 10, "max_cv": 0.4}
    )
    assert policy["strategy"] == "median_last_3"
    assert policy["adjustment"] == pytest.approx(0.10)
    assert policy["escalation_factor"] == pytest.approx(1.10)
    assert policy["max_cv"] == pytest.approx(0.4)

    assert normalise_labor_policy("median_last_3")["window"] == 3
    assert normalise_labor_policy("median_last_5")["window"] == 5


def test_statistics_include_spread_variance_and_recency() -> None:
    rows = [
        _row(130.0, "2026-09-01", row_id=3),
        _row(100.0, "2026-08-01", row_id=2),
        _row(110.0, "2026-07-01", row_id=1),
    ]
    stats = labor_rate_statistics(rows, as_of="2026-09-06")
    assert stats["count"] == 3
    assert stats["available_count"] == 3
    assert stats["latest"] == 130.0
    assert stats["min"] == 100.0
    assert stats["max"] == 130.0
    assert stats["median"] == 110.0
    assert stats["spread"] == 30.0
    assert stats["spread_ratio"] == pytest.approx(30 / 110)
    assert stats["variance"] == pytest.approx(1400 / 9)
    assert stats["stddev"] == pytest.approx((1400 / 9) ** 0.5)
    assert stats["recency_days"] == 5
    assert stats["observation_ids"] == [3, 2, 1]


def test_median_last_3_with_adjustment_preserves_all_provenance() -> None:
    rows = [
        _row(130.0, "2026-09-01", row_id=3, project_id=3),
        _row(100.0, "2026-08-01", row_id=2, project_id=2),
        _row(110.0, "2026-07-01", row_id=1, project_id=1),
        _row(80.0, "2026-06-01", row_id=0, project_id=0),
    ]
    rate, source = select_labor_rate(
        rows,
        {"strategy": "median_last_3_with_adjustment", "adjustment_pct": 10},
        as_of="2026-09-06",
    )
    assert rate == 121.0  # median(130, 100, 110) × 1.10
    assert source["source_tier"] == "historical_aggregate"
    assert source["observation_count"] == 3
    assert source["available_observation_count"] == 4
    assert source["base_rate"] == 110.0
    assert source["adjustment"] == pytest.approx(0.10)
    assert source["statistics"]["spread"] == 30.0
    assert source["provenance"][0]["source_project_id"] == 3
    assert source["provenance"][-1]["source_project_id"] == 1
    assert "median_last_3_with_adjustment" in source["explanation"]


def test_master_rate_wins_by_default_but_can_be_bypassed() -> None:
    rows = [
        _row(200.0, "2026-09-01", row_id=2, source_type="historical_boq"),
        _row(150.0, "2026-08-01", row_id=1, source_type="labor_master"),
    ]
    latest, source = select_labor_rate(rows, "latest")
    assert latest == 150.0
    assert source["source_tier"] == "labor_master"
    assert source["observation_count"] == 1

    historical, source = select_labor_rate(
        rows,
        {"strategy": "latest", "prefer_master": False},
    )
    assert historical == 200.0
    assert source["source_tier"] == "historical_exact"


def test_high_spread_and_variance_become_review_warnings() -> None:
    rows = [
        _row(100.0, "2026-09-01", row_id=2),
        _row(1000.0, "2026-08-01", row_id=1),
    ]
    rate, source = select_labor_rate(
        rows,
        {
            "strategy": "median_last_3",
            "max_spread_ratio": 0.25,
            "max_cv": 0.20,
        },
    )
    assert rate == 550.0
    assert source["needs_review"] is True
    assert "LABOR_RATE_SPREAD_HIGH" in source["warnings"]
    assert "LABOR_RATE_VARIANCE_HIGH" in source["warnings"]
    assert source["reason_code"] == "LABOR_RATE_SPREAD_HIGH"
    assert source["confidence"] < 1.0


def test_invalid_or_empty_rates_are_not_selected() -> None:
    rate, source = select_labor_rate(
        [
            {"id": 1, "rate": None, "effective_date": "2026-01-01"},
            {"id": 2, "rate": -10, "effective_date": "2026-01-02"},
        ],
        "latest",
    )
    assert rate is None
    assert source["needs_review"] is True
    assert source["reason_code"] == "LABOR_NO_RATE_OBSERVATION"


def test_choose_labor_rate_wires_policy_and_sql_provenance(isolated_db) -> None:
    with db.db_session() as conn:
        item_id = int(
            conn.execute(
                """
                INSERT INTO labor_items(
                    canonical_key, normalized_name, category, unit,
                    technical_attributes_json, created_at
                ) VALUES ('wire-install|m', 'wire install', 'cable', 'm', '{}', ?)
                RETURNING id
                """,
                ("2026-09-01T00:00:00+00:00",),
            ).fetchone()[0]
        )
        for rate, when, source_type in (
            (130.0, "2026-09-01", "historical_boq"),
            (100.0, "2026-08-01", "historical_boq"),
            (110.0, "2026-07-01", "historical_boq"),
        ):
            conn.execute(
                """
                INSERT INTO labor_rates(
                    labor_item_id, rate, effective_date, confidence,
                    policy_json, created_at
                ) VALUES (?, ?, ?, 0.9, ?, ?)
                """,
                (
                    item_id,
                    rate,
                    when,
                    json.dumps({"source_type": source_type}),
                    f"{when}T00:00:00+00:00",
                ),
            )
        selected, source = choose_labor_rate(
            conn,
            item_id,
            "2026-09-06",
            {"strategy": "median_last_3_with_adjustment", "adjustment_pct": 10},
        )

    assert selected == 121.0
    assert source["policy"] == "median_last_3_with_adjustment"
    assert source["observation_count"] == 3
    assert source["available_observation_count"] == 3
    assert source["provenance"][0]["id"] is not None
    assert source["statistics"]["spread"] == 30.0


def test_labor_warning_prevents_auto_approval() -> None:
    candidate = Candidate(
        entity_id=1,
        name="lap dat cap",
        code=None,
        unit="m",
        brand=None,
        origin=None,
        attrs={"category": "labor"},
        score=0.98,
        components={},
        explanation="",
    )
    status, risk, explanation = _status_for(
        candidate,
        100.0,
        candidate,
        550.0,
        1.0,
        labor_source={
            "needs_review": True,
            "reason_code": "LABOR_RATE_SPREAD_HIGH",
            "warnings": ["LABOR_RATE_SPREAD_HIGH"],
        },
    )
    assert status == "REVIEW_REQUIRED"
    assert risk == "MEDIUM"
    assert "LABOR_RATE_SPREAD_HIGH" in explanation
