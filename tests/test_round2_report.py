from __future__ import annotations

import json
from pathlib import Path

from benchmarks.holdout import _compact_report_for_disk
from benchmarks.round2_report import (
    build_round2_report,
    render_round2_markdown,
    write_round2_report,
)


def _snapshot(
    *,
    material_priced: int,
    material_coverage: float,
    labor_priced: int,
    labor_coverage: float,
    source_supported_material: int | None = None,
) -> dict:
    return {
        "holdout_file": "sample.xlsx",
        "runtime_seconds": {"total": 10.0},
        "evaluation": {
            "rows": 10,
            "run": {
                "metrics": {
                    "total_items": 10,
                    "auto_approved": 2,
                    "review_required": 3,
                    "material_priced": material_priced,
                    "labor_priced": labor_priced,
                }
            },
            "material": {
                "evaluable_items": 5,
                "predicted_items": material_priced,
                "coverage": material_coverage,
                "high_confidence_wrong_match": 0,
            },
            "labor": {
                "evaluable_items": 5,
                "predicted_items": labor_priced,
                "coverage": labor_coverage,
                "high_confidence_wrong_match": 0,
            },
            "provenance": {
                "material_complete": True,
                "labor_complete": True,
            },
        },
        "row_classification_audit": {"priceable_rows": 8},
        "data_gap": {
            "material_source_supported": source_supported_material,
            "labor_source_supported": 4,
            "material_capture_rate": 0.5,
            "labor_capture_rate": 0.75,
            "main_gap_counts": {"MATCH_FAILURE": 2},
        },
        "failure_analysis": {
            "failure_counts": {"MATCH_FAILURE": 2, "MISSING_SOURCE_DATA": 1}
        },
    }


def test_round2_report_preserves_unmeasured_round1_values_as_null() -> None:
    report = build_round2_report(
        _snapshot(
            material_priced=1,
            material_coverage=0.2,
            labor_priced=2,
            labor_coverage=0.4,
        ),
        _snapshot(
            material_priced=2,
            material_coverage=0.4,
            labor_priced=2,
            labor_coverage=0.4,
            source_supported_material=3,
        ),
    )

    material_source = next(
        item
        for item in report["metrics"]
        if item["metric"] == "Material source-supported items"
    )
    material_priced = next(
        item for item in report["metrics"] if item["metric"] == "Material priced items"
    )
    assert material_source["round1"] is None
    assert material_source["round2"] == 3
    assert material_source["delta"] is None
    assert material_priced["delta"] == 1
    assert "Before / after" in render_round2_markdown(report)


def test_compact_primary_report_omits_heavy_row_arrays() -> None:
    report = {
        "data_gap": {"rows": [{"id": i} for i in range(100)]},
        "failure_analysis": {
            "failures": [{"id": 1}],
            "material_failures": [{"id": 6}],
            "labor_failures": [{"id": 7}],
            "pricing_errors": [{"id": 2}],
            "data_gap": {"rows": [{"id": 3}]},
        },
        "temporal_modes": {
            "historical_reproduction": {
                "pricing_errors": [{"id": 4}],
                "material": {"pricing_errors": [{"id": 5}]},
            }
        },
    }
    compact = _compact_report_for_disk(report)

    assert compact["data_gap"]["row_detail_count"] == 100
    assert "rows" not in compact["data_gap"]
    assert compact["failure_analysis"]["failure_sample_count"] == 1
    assert "failures" not in compact["failure_analysis"]
    assert compact["failure_analysis"]["material_failure_sample_count"] == 1
    assert compact["failure_analysis"]["labor_failure_sample_count"] == 1
    assert "material_failures" not in compact["failure_analysis"]
    assert "labor_failures" not in compact["failure_analysis"]
    assert compact["temporal_modes"]["historical_reproduction"]["pricing_error_count"] == 1
    assert "pricing_errors" not in compact["temporal_modes"]["historical_reproduction"]


def test_round2_report_writer_creates_json_and_markdown(tmp_path: Path) -> None:
    report = build_round2_report(
        _snapshot(
            material_priced=1,
            material_coverage=0.2,
            labor_priced=1,
            labor_coverage=0.2,
        ),
        _snapshot(
            material_priced=2,
            material_coverage=0.4,
            labor_priced=2,
            labor_coverage=0.4,
            source_supported_material=3,
        ),
    )
    json_path, markdown_path = write_round2_report(
        tmp_path / "round2-report.json",
        tmp_path / "round2-report.md",
        report,
    )
    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert payload["report"] == "round2_before_after"
    assert "Round 2 benchmark report" in Path(markdown_path).read_text(
        encoding="utf-8"
    )
