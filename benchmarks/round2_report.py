from __future__ import annotations

"""Build a compact Round 1 vs Round 2 benchmark summary.

The detailed holdout/data-gap/temporal artifacts intentionally remain separate.
This module only projects stable, decision-useful metrics into the required
``round2-report.json`` and ``round2-report.md`` files.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


def load_report(path: str | Path) -> dict[str, Any]:
    """Load a benchmark JSON report with a useful error for malformed files."""

    report_path = Path(path)
    try:
        value = json.loads(report_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(report_path) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid benchmark JSON: {report_path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Benchmark report must be a JSON object: {report_path}")
    return value


def _get(mapping: Mapping[str, Any] | None, *keys: str, default: Any = None) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, Mapping):
            return default
        value = value.get(key)
    return default if value is None else value


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _delta(before: Any, after: Any) -> float | int | None:
    left, right = _number(before), _number(after)
    if left is None or right is None:
        return None
    value = right - left
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _round(value: Any, places: int = 4) -> float | int | None:
    number = _number(value)
    if number is None:
        return None
    return round(float(number), places)


def _round1_snapshot(baseline: Mapping[str, Any]) -> dict[str, Any]:
    evaluation = baseline.get("evaluation") or {}
    run_metrics = _get(evaluation, "run", "metrics", default={}) or {}
    material = evaluation.get("material") or {}
    labor = evaluation.get("labor") or {}
    audit = baseline.get("row_classification_audit") or {}
    return {
        "source": baseline.get("baseline_source") or "benchmarks/baseline-round1.json",
        "generated_at": baseline.get("generated_at"),
        "holdout_file": baseline.get("holdout_file"),
        "llm_enabled": False,
        "total_boq_items": _number(run_metrics.get("total_items") or evaluation.get("rows")),
        "priceable_rows_audit": _number(audit.get("priceable_rows")),
        "material_ground_truth_items": _number(material.get("evaluable_items")),
        "material_priced_items": _number(material.get("predicted_items")),
        "material_priced_coverage": _round(material.get("coverage")),
        "labor_ground_truth_items": _number(labor.get("evaluable_items")),
        "labor_priced_items": _number(labor.get("predicted_items")),
        "labor_priced_coverage": _round(labor.get("coverage")),
        "source_supported_material_items": None,
        "source_supported_labor_items": None,
        "material_capture_rate": None,
        "labor_capture_rate": None,
        "auto_approved": _number(run_metrics.get("auto_approved")),
        "review_required": _number(run_metrics.get("review_required")),
        "missing_source_data": None,
        "match_failure": None,
        "high_confidence_wrong_match": _number(
            (material.get("high_confidence_wrong_match") or 0)
            + (labor.get("high_confidence_wrong_match") or 0)
        ),
        "provenance_complete": bool(
            _get(evaluation, "provenance", "material_complete", default=True)
            and _get(evaluation, "provenance", "labor_complete", default=True)
        ),
        "runtime_seconds": _round(_get(baseline, "runtime_seconds", "total"), 3),
        "notes": [
            "Round 1 report predates explicit source-coverage and row-class metrics.",
            "Source-supported and engine-capture rates are therefore not available for Round 1.",
        ],
    }


def _round2_snapshot(current: Mapping[str, Any]) -> dict[str, Any]:
    evaluation = current.get("evaluation") or {}
    run_metrics = _get(evaluation, "run", "metrics", default={}) or {}
    material = evaluation.get("material") or {}
    labor = evaluation.get("labor") or {}
    data_gap = current.get("data_gap") or {}
    failure = current.get("failure_analysis") or {}
    row_audit = current.get("row_classification") or {}
    failure_counts = failure.get("failure_counts") or {}
    llm = current.get("llm_experiment") or {}
    return {
        "source": "benchmarks/holdout-report.json",
        "generated_at": current.get("generated_at"),
        "holdout_file": current.get("holdout_file"),
        "llm_enabled": bool(
            llm.get("enabled")
            or _get(evaluation, "run", "metrics", "llm_enabled", default=False)
        ),
        "total_boq_items": _number(run_metrics.get("total_items") or evaluation.get("rows")),
        "priceable_rows_audit": _number(row_audit.get("priceable_rows")),
        "priceable_rows_engine": _number(
            run_metrics.get("priceable_items") or evaluation.get("priceable_rows")
        ),
        "non_priceable_rows_engine": _number(
            run_metrics.get("non_priceable_items")
            or evaluation.get("non_priceable_rows")
        ),
        "material_ground_truth_items": _number(material.get("evaluable_items")),
        "material_priced_items": _number(material.get("predicted_items")),
        "material_priced_coverage": _round(material.get("coverage")),
        "labor_ground_truth_items": _number(labor.get("evaluable_items")),
        "labor_priced_items": _number(labor.get("predicted_items")),
        "labor_priced_coverage": _round(labor.get("coverage")),
        "source_supported_material_items": _number(
            data_gap.get("material_source_supported")
        ),
        "source_supported_labor_items": _number(
            data_gap.get("labor_source_supported")
        ),
        "material_capture_rate": _round(data_gap.get("material_capture_rate")),
        "labor_capture_rate": _round(data_gap.get("labor_capture_rate")),
        "auto_approved": _number(run_metrics.get("auto_approved")),
        "review_required": _number(run_metrics.get("review_required")),
        "missing_source_data": _number(failure_counts.get("MISSING_SOURCE_DATA")),
        "match_failure": _number(failure_counts.get("MATCH_FAILURE")),
        "high_confidence_wrong_match": _number(
            (material.get("high_confidence_wrong_match") or 0)
            + (labor.get("high_confidence_wrong_match") or 0)
        ),
        "provenance_complete": bool(
            _get(evaluation, "provenance", "material_complete", default=False)
            and _get(evaluation, "provenance", "labor_complete", default=False)
        ),
        "runtime_seconds": _round(_get(current, "runtime_seconds", "total"), 3),
        "data_gap_main_counts": data_gap.get("main_gap_counts") or {},
        "notes": [
            "Source-supported is a theoretical category/catalog upper bound.",
            "Capture rate is measured over source-supported rows and does not prove identity correctness.",
            "Failure counts are diagnostic routing counts from the detailed failure analysis artifact.",
        ],
    }


_METRICS: tuple[tuple[str, str, str], ...] = (
    ("BOQ items (total)", "total_boq_items", "count"),
    ("Priceable rows (audit)", "priceable_rows_audit", "count"),
    ("Material ground-truth items", "material_ground_truth_items", "count"),
    ("Material priced items", "material_priced_items", "count"),
    ("Material priced coverage", "material_priced_coverage", "ratio"),
    ("Material source-supported items", "source_supported_material_items", "count"),
    ("Material engine capture rate", "material_capture_rate", "ratio"),
    ("Labor ground-truth items", "labor_ground_truth_items", "count"),
    ("Labor priced items", "labor_priced_items", "count"),
    ("Labor priced coverage", "labor_priced_coverage", "ratio"),
    ("Labor source-supported items", "source_supported_labor_items", "count"),
    ("Labor engine capture rate", "labor_capture_rate", "ratio"),
    ("AUTO_APPROVED", "auto_approved", "count"),
    ("REVIEW_REQUIRED", "review_required", "count"),
    ("MISSING_SOURCE_DATA failures", "missing_source_data", "count"),
    ("MATCH_FAILURE failures", "match_failure", "count"),
    ("High-confidence wrong matches", "high_confidence_wrong_match", "count"),
    ("Provenance complete", "provenance_complete", "bool"),
    ("Runtime (seconds)", "runtime_seconds", "seconds"),
)


def build_round2_report(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    baseline_path: str = "benchmarks/baseline-round1.json",
    current_path: str = "benchmarks/holdout-report.json",
) -> dict[str, Any]:
    """Project two detailed reports into a compact before/after summary."""

    round1 = _round1_snapshot(baseline)
    round2 = _round2_snapshot(current)
    metrics: list[dict[str, Any]] = []
    for label, key, kind in _METRICS:
        before = round1.get(key)
        after = round2.get(key)
        metrics.append(
            {
                "metric": label,
                "kind": kind,
                "round1": before,
                "round2": after,
                "delta": _delta(before, after)
                if kind not in {"bool"}
                else (
                    None
                    if before is None or after is None
                    else bool(after) != bool(before)
                ),
            }
        )
    return {
        "schema_version": "1.0",
        "report": "round2_before_after",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "holdout_file": current.get("holdout_file") or baseline.get("holdout_file"),
        "baseline_path": baseline_path,
        "current_path": current_path,
        "round1": round1,
        "round2": round2,
        "metrics": metrics,
        "interpretation": [
            "Round 1 metrics are the immutable baseline snapshot from the previous report.",
            "Round 2 adds explicit row classification, source-supported coverage and capture-rate metrics.",
            "A missing Round 1 value means that metric was not measured, not zero.",
            "Price exact-match remains 0 in both snapshots; current supplier repricing and historical project prices are separate temporal questions.",
        ],
    }


def _format_value(value: Any, kind: str) -> str:
    if value is None:
        return "n/a"
    if kind == "ratio":
        return f"{float(value) * 100:.1f}%"
    if kind == "bool":
        return "PASS" if bool(value) else "FAIL"
    if kind == "seconds":
        return f"{float(value):.3f}s"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render_round2_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Round 2 benchmark report",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Holdout: `{report.get('holdout_file')}`",
        f"- Round 1 source: `{report.get('baseline_path')}`",
        f"- Round 2 source: `{report.get('current_path')}`",
        "",
        "## Before / after",
        "",
        "| Metric | Kind | Round 1 | Round 2 | Delta |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for item in report.get("metrics") or []:
        kind = str(item.get("kind") or "count")
        delta = item.get("delta")
        if kind == "ratio" and delta is not None:
            delta_text = f"{float(delta) * 100:+.1f} pp"
        elif kind == "bool":
            delta_text = "changed" if delta else "unchanged"
        elif delta is None:
            delta_text = "n/a"
        elif kind == "seconds":
            delta_text = f"{float(delta):+.3f}s"
        else:
            delta_text = f"{delta:+g}" if isinstance(delta, (int, float)) else str(delta)
        lines.append(
            f"| {item.get('metric')} | `{kind}` | "
            f"{_format_value(item.get('round1'), kind)} | "
            f"{_format_value(item.get('round2'), kind)} | {delta_text} |"
        )
    lines.extend(
        [
            "",
            "## Round 2 interpretation",
            "",
        ]
    )
    lines.extend(f"- {text}" for text in report.get("interpretation") or [])
    lines.extend(
        [
            "",
            "### Round 2 source-gap summary",
            "",
            f"- Material source-supported items: {_format_value(_get(report.get('round2'), 'source_supported_material_items'), 'count')}",
            f"- Labor source-supported items: {_format_value(_get(report.get('round2'), 'source_supported_labor_items'), 'count')}",
            f"- Main category gap counts: `{json.dumps(report.get('round2', {}).get('data_gap_main_counts', {}), ensure_ascii=False, sort_keys=True)}`",
            "",
            "Detailed row-level evidence remains in the linked data-gap, failure-analysis, temporal-mode and row-classification artifacts referenced by `benchmarks/holdout-report.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def write_round2_report(
    output_json: str | Path,
    output_markdown: str | Path,
    report: Mapping[str, Any],
) -> tuple[str, str]:
    """Write the compact JSON/Markdown Round 2 artifacts."""

    json_path = Path(output_json)
    markdown_path = Path(output_markdown)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_round2_markdown(report),
        encoding="utf-8",
    )
    return str(json_path.resolve()), str(markdown_path.resolve())


__all__ = [
    "build_round2_report",
    "load_report",
    "render_round2_markdown",
    "write_round2_report",
]
