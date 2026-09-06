from __future__ import annotations

"""Run a leakage-safe holdout benchmark against the real sample workbooks.

The benchmark intentionally uses a fresh SQLite database.  All workbooks
except the selected holdout are ingested as training/knowledge data; the
holdout workbook is ingested with ``exclude_prices=True`` so its historical
material/labor prices remain ground truth only.  The pricing engine is then
run on the holdout project and its predictions are compared with the saved
ground truth snapshot.

Examples
--------

    python -m benchmarks.holdout
    python -m benchmarks.holdout --holdout "BOQ-HỆ THỐNG*.xlsx"
    python -m benchmarks.holdout --output-dir benchmarks --db-path storage/holdout.db

The command writes ``holdout-report.json`` and ``holdout-report.md`` under the
requested output directory.
"""

import argparse
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator

from app import config, db, ingest
from app.excel import parse_workbook
from app.ingest import ingest_workbook
from app.normalize import canonical_key, normalize_text, technical_attributes
from app.pricing import AUTO_THRESHOLD, run_pricing
from benchmarks.row_audit import classify_parsed_workbook, markdown_report as _row_audit_markdown
from benchmarks.modes import evaluate_db_modes, write_temporal_report
from benchmarks.reports import (
    build_data_gap_report,
    build_data_requirements,
    build_failure_analysis,
    write_report_files,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT_DIR / "input"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "benchmarks"

def _portable_report_path(value: str | Path) -> str:
    """Prefer repository-relative paths in committed benchmark artifacts."""

    path = Path(value).resolve()
    try:
        return path.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return str(path)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _compact_data_gap(data_gap: dict[str, Any] | None) -> dict[str, Any]:
    """Keep primary benchmark JSON small while preserving source-gap metrics.

    The detailed row records are already written to ``data-gap-report.json``.
    Embedding those records in ``holdout-report.json`` made the primary report
    several megabytes and encouraged consumers to parse the wrong artifact.
    Keep category/source summaries here and expose the omitted-row count.
    """

    if not isinstance(data_gap, dict):
        return {}
    compact = dict(data_gap)
    rows = compact.pop("rows", None)
    if isinstance(rows, list):
        compact["row_detail_count"] = len(rows)
        compact["row_details_omitted"] = True
    return compact


def _compact_failure_analysis(
    failure_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    """Retain failure counts and a pointer-friendly sample summary."""

    if not isinstance(failure_analysis, dict):
        return {}
    compact = dict(failure_analysis)
    failures = compact.pop("failures", None)
    material_failures = compact.pop("material_failures", None)
    labor_failures = compact.pop("labor_failures", None)
    pricing_errors = compact.pop("pricing_errors", None)
    if isinstance(failures, list):
        compact["failure_sample_count"] = len(failures)
        compact["failure_samples_omitted"] = True
        compact["failure_sample_ids"] = [
            item.get("id")
            for item in failures[:10]
            if isinstance(item, dict) and item.get("id") is not None
        ]
    if isinstance(material_failures, list):
        compact["material_failure_sample_count"] = len(material_failures)
        compact["material_failure_samples_omitted"] = True
        compact["material_failure_sample_ids"] = [
            item.get("id")
            for item in material_failures[:10]
            if isinstance(item, dict) and item.get("id") is not None
        ]
    if isinstance(labor_failures, list):
        compact["labor_failure_sample_count"] = len(labor_failures)
        compact["labor_failure_samples_omitted"] = True
        compact["labor_failure_sample_ids"] = [
            item.get("id")
            for item in labor_failures[:10]
            if isinstance(item, dict) and item.get("id") is not None
        ]
    if isinstance(pricing_errors, list):
        compact["pricing_error_sample_count"] = len(pricing_errors)
        compact["pricing_error_samples_omitted"] = True
        compact["pricing_error_sample_ids"] = [
            item.get("id")
            for item in pricing_errors[:10]
            if isinstance(item, dict) and item.get("id") is not None
        ]
    if isinstance(compact.get("data_gap"), dict):
        compact["data_gap"] = _compact_data_gap(compact["data_gap"])
    return compact


def _compact_temporal_modes(
    temporal_modes: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep temporal mode metrics while omitting repeated row error details."""

    if not isinstance(temporal_modes, dict):
        return {}
    compact = dict(temporal_modes)
    for mode_name in ("historical_reproduction", "current_repricing"):
        mode = compact.get(mode_name)
        if not isinstance(mode, dict):
            continue
        mode_copy = dict(mode)
        mode_errors = mode_copy.pop("pricing_errors", None)
        if isinstance(mode_errors, list):
            mode_copy["pricing_error_count"] = len(mode_errors)
            mode_copy["pricing_errors_omitted"] = True
        for side in ("material", "labor"):
            side_data = mode_copy.get(side)
            if not isinstance(side_data, dict):
                continue
            side_copy = dict(side_data)
            side_errors = side_copy.pop("pricing_errors", None)
            if isinstance(side_errors, list):
                side_copy["pricing_error_count"] = len(side_errors)
                side_copy["pricing_errors_omitted"] = True
            mode_copy[side] = side_copy
        compact[mode_name] = mode_copy
    return compact


def _compact_report_for_disk(report: dict[str, Any]) -> dict[str, Any]:
    """Build the compact, machine-readable primary report representation."""

    compact = dict(report)
    compact["report_schema"] = "round2-compact-v1"
    compact["data_gap"] = _compact_data_gap(report.get("data_gap"))
    compact["failure_analysis"] = _compact_failure_analysis(
        report.get("failure_analysis")
    )
    compact["temporal_modes"] = _compact_temporal_modes(
        report.get("temporal_modes")
    )
    compact["detail_artifacts"] = {
        "data_gap": "artifact_paths.data_gap_json",
        "failure_analysis": "artifact_paths.failure_analysis_markdown",
        "temporal_modes": "artifact_paths.temporal_json",
        "row_classification": "row_classification_json",
    }
    return compact


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_workbooks(input_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".xls", ".xlsx"}
    )


def _resolve_holdout(workbooks: list[Path], requested: str | None) -> Path:
    if not workbooks:
        raise FileNotFoundError("No .xls/.xlsx workbooks found in input directory")
    if requested:
        # Accept an exact filename, a glob, or a unique prefix.  This is
        # useful for Vietnamese filenames on shells with quoting differences.
        exact = [path for path in workbooks if path.name == requested]
        if len(exact) == 1:
            return exact[0]
        globbed = sorted(path for path in workbooks if path.match(requested))
        if len(globbed) == 1:
            return globbed[0]
        prefixed = [path for path in workbooks if path.name.startswith(requested)]
        if len(prefixed) == 1:
            return prefixed[0]
        raise ValueError(
            f"Holdout selector {requested!r} matched "
            f"{len(exact) + len(globbed) + len(prefixed)} files; "
            "pass an exact filename, glob, or unique prefix."
        )
    # The supplied sample set has one historical BOQ whose filename starts
    # with BOQ-.  Prefer that deterministic convention, then fall back to a
    # workbook classified as HISTORICAL_BOQ.
    by_name = [path for path in workbooks if path.name.upper().startswith("BOQ-")]
    if len(by_name) == 1:
        return by_name[0]
    classified = []
    for path in workbooks:
        try:
            parsed = parse_workbook(path)
        except Exception:
            continue
        if parsed.get("workbook_type") == "HISTORICAL_BOQ":
            classified.append(path)
    if len(classified) == 1:
        return classified[0]
    raise ValueError(
        "Could not infer a unique holdout workbook; pass --holdout explicitly."
    )


@contextmanager
def _isolated_application_storage(
    db_path: Path,
    *,
    keep_storage: bool = False,
) -> Iterator[Path]:
    """Patch imported module aliases so benchmark writes only to *db_path*."""

    db_path = db_path.resolve()
    storage_dir = db_path.parent
    raw_dir = storage_dir / "raw"
    export_dir = storage_dir / "exports"
    old = {
        "config.STORAGE_DIR": config.STORAGE_DIR,
        "config.RAW_DIR": config.RAW_DIR,
        "config.EXPORT_DIR": config.EXPORT_DIR,
        "config.DB_PATH": config.DB_PATH,
        "db.DB_PATH": db.DB_PATH,
        "ingest.RAW_DIR": ingest.RAW_DIR,
    }
    try:
        config.STORAGE_DIR = storage_dir
        config.RAW_DIR = raw_dir
        config.EXPORT_DIR = export_dir
        config.DB_PATH = db_path
        db.DB_PATH = db_path
        ingest.RAW_DIR = raw_dir
        config.ensure_directories()
        db.init_db()
        yield db_path
    finally:
        config.STORAGE_DIR = old["config.STORAGE_DIR"]
        config.RAW_DIR = old["config.RAW_DIR"]
        config.EXPORT_DIR = old["config.EXPORT_DIR"]
        config.DB_PATH = old["config.DB_PATH"]
        db.DB_PATH = old["db.DB_PATH"]
        ingest.RAW_DIR = old["ingest.RAW_DIR"]
        if not keep_storage:
            # The caller normally supplies a TemporaryDirectory path.  Do not
            # remove a user-specified DB, even when the benchmark fails.
            pass


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _relative_error(predicted: float | None, actual: float | None) -> float | None:
    predicted = _safe_float(predicted)
    actual = _safe_float(actual)
    if predicted is None or actual is None or actual == 0:
        return None
    return abs(predicted - actual) / abs(actual)


def _price_equal(predicted: float | None, actual: float | None) -> bool:
    predicted = _safe_float(predicted)
    actual = _safe_float(actual)
    if predicted is None or actual is None:
        return False
    # Excel prices are occasionally rounded to whole đồng or a few đồng.
    return math.isclose(predicted, actual, rel_tol=0.005, abs_tol=1.0)


def _token_set(value: str | None) -> set[str]:
    return {
        token
        for token in normalize_text(value or "").split()
        if token and token not in {"va", "tu", "cho", "cua", "voi", "theo", "cac"}
    }


def _technical_match_quality(
    query_description: str | None,
    candidate_name: str | None,
    candidate_attrs: dict[str, Any] | None,
) -> bool | None:
    """Estimate whether a selected catalog candidate matches the BOQ item.

    Holdout workbooks do not carry the catalog product/labor IDs, so this is a
    conservative *proxy* for match correctness, not a claim of human-labeled
    truth.  ``None`` means the available text/attributes are insufficient to
    make a reliable judgment.
    """

    query = canonical_key(query_description or "")
    candidate = canonical_key(candidate_name or "")
    if not query or not candidate:
        return None
    if query == candidate:
        return True

    query_attrs = technical_attributes(query_description or "")
    attrs = candidate_attrs or technical_attributes(candidate_name or "")
    query_category = normalize_text(query_attrs.get("category", ""))
    candidate_category = normalize_text(attrs.get("category", ""))
    if query_category and candidate_category and query_category != candidate_category:
        return False

    compared = 0
    matched = 0
    hard_conflict = False
    for key in (
        "category",
        "cable_family",
        "voltage",
        "voltage_class",
        "insulation",
        "armour",
        "conductor",
        "cores",
        "cross_section_mm2",
        "diameter_mm",
        "diameters_mm",
        "material",
    ):
        if key not in query_attrs or key not in attrs:
            continue
        compared += 1
        left, right = query_attrs[key], attrs[key]
        if key == "cores":
            left = query_attrs.get("base_cores", left)
            right = attrs.get("base_cores", right)
        if key == "cable_family":
            left_family = normalize_text(left).replace("/", "-")
            right_family = normalize_text(right).replace("/", "-")
            if left_family == right_family:
                equal = True
            elif left_family.startswith(("fsn-", "frn-")) or right_family.startswith(
                ("fsn-", "frn-")
            ):
                equal = False
            else:
                equal = left_family.split("-")[0] == right_family.split("-")[0]
        elif key == "diameters_mm":
            left_values = list(left) if isinstance(left, list) else [left]
            right_values = list(right) if isinstance(right, list) else [right]
            equal = len(left_values) == len(right_values) and all(
                abs(float(a) - float(b)) < 1e-6
                for a, b in zip(left_values, right_values)
            )
        elif isinstance(left, (int, float)) and isinstance(right, (int, float)):
            equal = abs(float(left) - float(right)) < 1e-6
        else:
            equal = normalize_text(left) == normalize_text(right)
        if equal:
            matched += 1
        elif key in {
            "cable_family",
            "voltage",
            "voltage_class",
            "insulation",
            "armour",
            "conductor",
            "cores",
            "cross_section_mm2",
            "diameter_mm",
            "diameters_mm",
            "material",
        }:
            hard_conflict = True

    if hard_conflict:
        return False
    # A candidate with an unmentioned voltage/armour/size is not provably
    # wrong, but it is not provably the same item either. Keep the benchmark
    # proxy conservative and report it as unknown.
    critical_missing = {
        "voltage",
        "armour",
        "cable_family",
        "cores",
        "cross_section_mm2",
        "diameter_mm",
        "diameters_mm",
    }
    if any(key in attrs and key not in query_attrs for key in critical_missing):
        if compared >= 2:
            return None
    overlap_left = _token_set(query_description)
    overlap_right = _token_set(candidate_name)
    overlap = (
        len(overlap_left & overlap_right) / len(overlap_left | overlap_right)
        if overlap_left and overlap_right
        else 0.0
    )
    sequence = SequenceMatcher(None, query, candidate).ratio()
    if compared and matched / compared >= 0.75:
        return True
    if overlap >= 0.65 and sequence >= 0.65:
        return True
    if compared or overlap >= 0.25:
        return False
    return None


def _count(conn: Any, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    return int(conn.execute(query, params).fetchone()[0])


def _snapshot_ground_truth(conn: Any, project_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, raw_description, normalized_description, unit, quantity,
               material_price, labor_price, source_file_id, source_row_id
        FROM boq_items
        WHERE project_id=?
        ORDER BY id
        """,
        (project_id,),
    ).fetchall()
    return {
        int(row["id"]): {
            "id": int(row["id"]),
            "description": row["raw_description"] or row["normalized_description"],
            "unit": row["unit"],
            "quantity": _safe_float(row["quantity"]),
            "material_price": _safe_float(row["material_price"]),
            "labor_price": _safe_float(row["labor_price"]),
            "source_file_id": row["source_file_id"],
            "source_row_id": row["source_row_id"],
        }
        for row in rows
    }


def _evaluate(
    conn: Any,
    project_id: int,
    holdout_source_id: int,
    ground_truth: dict[int, dict[str, Any]],
    run_result: dict[str, Any],
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT b.id, b.raw_description, b.quantity, b.material_price, b.labor_price,
               b.material_confidence, b.labor_confidence, b.status,
               b.line_class, b.status_reason,
               material_source_json, labor_source_json,
               p.normalized_name AS product_name,
               p.technical_attributes_json AS product_attributes_json,
               l.normalized_name AS labor_name,
               l.technical_attributes_json AS labor_attributes_json
        FROM boq_items b
        LEFT JOIN products p ON p.id = b.matched_product_id
        LEFT JOIN labor_items l ON l.id = b.matched_labor_item_id
        WHERE b.project_id=?
        ORDER BY b.id
        """,
        (project_id,),
    ).fetchall()

    material_evaluable = 0
    labor_evaluable = 0
    material_predicted = 0
    labor_predicted = 0
    material_exact = 0
    labor_exact = 0
    material_errors: list[float] = []
    labor_errors: list[float] = []
    high_conf_material = 0
    high_conf_material_price_error = 0
    high_conf_material_wrong_match = 0
    material_match_evaluable = 0
    material_match_correct = 0
    material_match_incorrect = 0
    material_match_unknown = 0
    high_conf_labor = 0
    high_conf_labor_price_error = 0
    high_conf_labor_wrong_match = 0
    labor_match_evaluable = 0
    labor_match_correct = 0
    labor_match_incorrect = 0
    labor_match_unknown = 0
    provenance_material_missing = 0
    provenance_labor_missing = 0
    statuses: dict[str, int] = {}
    line_classes: dict[str, int] = {}
    priceable_rows = 0
    non_priceable_rows = 0

    for row in rows:
        item_id = int(row["id"])
        truth = ground_truth.get(item_id, {})
        gt_material = truth.get("material_price")
        gt_labor = truth.get("labor_price")
        pred_material = _safe_float(row["material_price"])
        pred_labor = _safe_float(row["labor_price"])
        material_conf = _safe_float(row["material_confidence"])
        labor_conf = _safe_float(row["labor_confidence"])
        status = str(row["status"] or "UNKNOWN")
        statuses[status] = statuses.get(status, 0) + 1
        line_class = str(row["line_class"] or "UNKNOWN").upper()
        line_classes[line_class] = line_classes.get(line_class, 0) + 1
        # Accuracy metrics are intentionally evaluated only on rows positively
        # classified as actual priceable line items. ``UNKNOWN`` rows remain
        # persisted and reviewable, but mixing unresolved parser ambiguity into
        # pricing KPIs would make the benchmark semantics misleading.
        is_metric_priceable = line_class == "PRICEABLE_LINE_ITEM"
        if not is_metric_priceable:
            non_priceable_rows += 1
        else:
            priceable_rows += 1

        if is_metric_priceable and gt_material is not None and gt_material > 0:
            material_evaluable += 1
            if pred_material is not None:
                material_predicted += 1
                err = _relative_error(pred_material, gt_material)
                if err is not None:
                    material_errors.append(err)
                if _price_equal(pred_material, gt_material):
                    material_exact += 1
                if material_conf is not None and material_conf >= AUTO_THRESHOLD:
                    high_conf_material += 1
                    if not _price_equal(pred_material, gt_material):
                        high_conf_material_price_error += 1
                if row["product_name"]:
                    quality = _technical_match_quality(
                        truth.get("description"),
                        row["product_name"],
                        json.loads(row["product_attributes_json"] or "{}"),
                    )
                    if quality is True:
                        material_match_correct += 1
                        material_match_evaluable += 1
                    elif quality is False:
                        material_match_incorrect += 1
                        material_match_evaluable += 1
                        if material_conf is not None and material_conf >= AUTO_THRESHOLD:
                            high_conf_material_wrong_match += 1
                    else:
                        material_match_unknown += 1
                source = row["material_source_json"]
                if not source or source in {"{}", "null"}:
                    provenance_material_missing += 1

        if is_metric_priceable and gt_labor is not None and gt_labor > 0:
            labor_evaluable += 1
            if pred_labor is not None:
                labor_predicted += 1
                err = _relative_error(pred_labor, gt_labor)
                if err is not None:
                    labor_errors.append(err)
                if _price_equal(pred_labor, gt_labor):
                    labor_exact += 1
                if labor_conf is not None and labor_conf >= AUTO_THRESHOLD:
                    high_conf_labor += 1
                    if not _price_equal(pred_labor, gt_labor):
                        high_conf_labor_price_error += 1
                if row["labor_name"]:
                    quality = _technical_match_quality(
                        truth.get("description"),
                        row["labor_name"],
                        json.loads(row["labor_attributes_json"] or "{}"),
                    )
                    if quality is True:
                        labor_match_correct += 1
                        labor_match_evaluable += 1
                    elif quality is False:
                        labor_match_incorrect += 1
                        labor_match_evaluable += 1
                        if labor_conf is not None and labor_conf >= AUTO_THRESHOLD:
                            high_conf_labor_wrong_match += 1
                    else:
                        labor_match_unknown += 1
                source = row["labor_source_json"]
                if not source or source in {"{}", "null"}:
                    provenance_labor_missing += 1

    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    def summary(
        evaluable: int,
        predicted: int,
        exact: int,
        errors: list[float],
        high_conf: int,
        high_conf_price_error: int,
        match_evaluable: int,
        match_correct: int,
        match_incorrect: int,
        match_unknown: int,
        high_conf_wrong_match: int,
    ) -> dict[str, Any]:
        return {
            "evaluable_items": evaluable,
            "predicted_items": predicted,
            "coverage": ratio(predicted, evaluable),
            "exact_price_match": exact,
            "exact_price_accuracy": ratio(exact, evaluable),
            "predicted_price_accuracy": ratio(exact, predicted),
            "mean_relative_error": round(statistics.mean(errors), 6) if errors else None,
            "median_relative_error": round(statistics.median(errors), 6) if errors else None,
            "high_confidence_items": high_conf,
            # Backward-compatible aliases retain the old keys but now make
            # their meaning explicit: these are price errors, not necessarily
            # incorrect product/labor matches.
            "high_confidence_wrong": high_conf_price_error,
            "high_confidence_price_error": high_conf_price_error,
            "high_confidence_price_error_rate": ratio(high_conf_price_error, high_conf),
            "match_evaluable": match_evaluable,
            "match_correct": match_correct,
            "match_incorrect": match_incorrect,
            "match_unknown": match_unknown,
            "match_accuracy": ratio(match_correct, match_evaluable),
            "high_confidence_wrong_match": high_conf_wrong_match,
            "high_confidence_wrong_match_rate": ratio(
                high_conf_wrong_match, high_conf
            ),
            "high_confidence_false_positive_rate": ratio(
                high_conf_wrong_match, high_conf
            ),
        }

    leakage = {
        "holdout_product_price_rows": _count(
            conn, "product_prices", "source_file_id = ?", (holdout_source_id,)
        ),
        "holdout_labor_rate_rows": _count(
            conn, "labor_rates", "source_file_id = ?", (holdout_source_id,)
        ),
    }
    leakage["passed"] = (
        leakage["holdout_product_price_rows"] == 0
        and leakage["holdout_labor_rate_rows"] == 0
    )

    return {
        "run": run_result,
        "rows": len(rows),
        "statuses": statuses,
        "line_classes": line_classes,
        "priceable_rows": priceable_rows,
        "non_priceable_rows": non_priceable_rows,
        "material": summary(
            material_evaluable,
            material_predicted,
            material_exact,
            material_errors,
            high_conf_material,
            high_conf_material_price_error,
            material_match_evaluable,
            material_match_correct,
            material_match_incorrect,
            material_match_unknown,
            high_conf_material_wrong_match,
        ),
        "labor": summary(
            labor_evaluable,
            labor_predicted,
            labor_exact,
            labor_errors,
            high_conf_labor,
            high_conf_labor_price_error,
            labor_match_evaluable,
            labor_match_correct,
            labor_match_incorrect,
            labor_match_unknown,
            high_conf_labor_wrong_match,
        ),
        "provenance": {
            "material_missing_source": provenance_material_missing,
            "labor_missing_source": provenance_labor_missing,
            "applied_material_prices": material_predicted,
            "applied_labor_prices": labor_predicted,
            "material_complete": provenance_material_missing == 0,
            "labor_complete": provenance_labor_missing == 0,
        },
        "leakage": leakage,
    }


def _markdown_report(report: dict[str, Any]) -> str:
    parsing = report["parsing"]
    evaluation = report["evaluation"]
    row_classification = report.get("row_classification") or {}
    material = evaluation["material"]
    labor = evaluation["labor"]
    leakage = evaluation["leakage"]
    provenance = evaluation["provenance"]

    def pct(value: Any) -> str:
        return f"{float(value) * 100:.1f}%" if value is not None else "—"

    lines = [
        "# Holdout pricing benchmark",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Input directory: `{report['input_dir']}`",
        f"- Holdout: `{report['holdout_file']}`",
        f"- Database: `{report['database_path']}`",
        f"- Database persisted: **{'yes' if report.get('database_persisted') else 'no (temporary)'}**",
        (
            f"- Runtime: **{report['runtime_seconds']['total']:.3f}s** "
            f"(ingest {report['runtime_seconds']['ingest']:.3f}s, "
            f"pricing {report['runtime_seconds']['pricing']:.3f}s, "
            f"evaluation {report['runtime_seconds']['evaluation']:.3f}s)"
        ),
        "",
        "## Leakage guard",
        "",
        (
            f"- Holdout material price rows in catalog: **{leakage['holdout_product_price_rows']}**"
        ),
        f"- Holdout labor rate rows in catalog: **{leakage['holdout_labor_rate_rows']}**",
        f"- Result: **{'PASS' if leakage['passed'] else 'FAIL'}**",
        "",
        "## Parsing",
        "",
        f"- Workbooks ingested: {parsing['workbooks_ingested']}",
        f"- Sheets inspected: {parsing['sheets_inspected']}",
        f"- Source rows retained: {parsing['source_rows']}",
        f"- Data rows detected: {parsing['data_rows']}",
        f"- Descriptions present: {parsing['descriptions_present']}",
        f"- Quantities present: {parsing['quantities_present']}",
        "",
        "## BOQ row classification audit",
        "",
        f"- Parsed BOQ/PANEL rows included: {row_classification.get('included_rows', 0)}",
        f"- Priceable line items (audit view): {row_classification.get('priceable_rows', 0)}",
        f"- Non-priceable/uncertain rows: {row_classification.get('non_priceable_rows', 0)}",
        f"- Rows on excluded `OTHER` sheets: {row_classification.get('excluded_other_sheet_rows', 0)}",
        "",
        "| Classification | Rows |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| `{classification}` | {count} |"
        for classification, count in (row_classification.get("counts") or {}).items()
    )
    lines.extend(
        [
            "",
        "## Material",
        "",
        f"- Ground-truth items: {material['evaluable_items']}",
        f"- Predicted/priced: {material['predicted_items']} ({pct(material['coverage'])})",
        f"- Exact price matches: {material['exact_price_match']} ({pct(material['exact_price_accuracy'])})",
        f"- Mean relative error: {pct(material['mean_relative_error'])}",
        f"- Median relative error: {pct(material['median_relative_error'])}",
        f"- Technical/name match accuracy (proxy): {pct(material['match_accuracy'])}",
        f"- High-confidence price-error rate: {pct(material['high_confidence_price_error_rate'])}",
        f"- High-confidence wrong-match rate (proxy): {pct(material['high_confidence_wrong_match_rate'])}",
        "",
        "## Labor",
        "",
        f"- Ground-truth items: {labor['evaluable_items']}",
        f"- Predicted/priced: {labor['predicted_items']} ({pct(labor['coverage'])})",
        f"- Exact price matches: {labor['exact_price_match']} ({pct(labor['exact_price_accuracy'])})",
        f"- Mean relative error: {pct(labor['mean_relative_error'])}",
        f"- Median relative error: {pct(labor['median_relative_error'])}",
        f"- Technical/name match accuracy (proxy): {pct(labor['match_accuracy'])}",
        f"- High-confidence price-error rate: {pct(labor['high_confidence_price_error_rate'])}",
        f"- High-confidence wrong-match rate (proxy): {pct(labor['high_confidence_wrong_match_rate'])}",
        "",
        "## Provenance",
        "",
        f"- Material predictions missing source: {provenance['material_missing_source']}",
        f"- Labor predictions missing source: {provenance['labor_missing_source']}",
        f"- Complete material provenance: **{'PASS' if provenance['material_complete'] else 'FAIL'}**",
        f"- Complete labor provenance: **{'PASS' if provenance['labor_complete'] else 'FAIL'}**",
        "",
        "## Status distribution",
        "",
        "| Status | Rows |",
        "| --- | ---: |",
    ]
    )
    lines.extend(
        f"| `{status}` | {count} |"
        for status, count in sorted(evaluation["statuses"].items())
    )
    lines.extend(
        [
            "",
            "## Persisted row classes",
            "",
            f"- Priceable BOQ rows: {evaluation.get('priceable_rows', 0)}",
            f"- Non-priceable BOQ rows: {evaluation.get('non_priceable_rows', 0)}",
            "",
            "| Row class | Rows |",
            "| --- | ---: |",
        ]
    )
    lines.extend(
        f"| `{line_class}` | {count} |"
        for line_class, count in sorted(
            (evaluation.get("line_classes") or {}).items()
        )
    )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Prices from the holdout workbook are retained on BOQ rows as ground "
            "truth but are excluded from `product_prices` and `labor_rates`. "
            "A price prediction is counted as an exact match within 0.5% (or "
            "one đồng) because historical Excel files may round values. "
            "Technical/name match accuracy is a conservative proxy because the "
            "holdout does not contain catalog IDs. Price errors can therefore "
            "reflect project-specific pricing drift even when the item match "
            "is plausible. Items with missing/ambiguous matches remain "
            "reviewable rather than receiving a fabricated price.",
            "",
        ]
    )
    return "\n".join(lines)


def run_benchmark(
    *,
    input_dir: Path = DEFAULT_INPUT_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    holdout: str | None = None,
    holdout_filename: str | None = None,
    db_path: Path | None = None,
    keep_storage: bool = False,
    llm_enabled: bool = False,
) -> dict[str, Any]:
    benchmark_started = time.perf_counter()
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    workbooks = _find_workbooks(input_dir)
    # ``holdout_filename`` is retained as a compatibility alias for the
    # existing ``app.cli benchmark`` command.
    selector = holdout if holdout is not None else holdout_filename
    holdout_path = _resolve_holdout(workbooks, selector)
    training_paths = [path for path in workbooks if path != holdout_path]
    output_dir.mkdir(parents=True, exist_ok=True)

    temporary: tempfile.TemporaryDirectory[str] | None = None
    database_persisted = db_path is not None
    if db_path is None:
        temporary = tempfile.TemporaryDirectory(prefix="dh-holdout-")
        db_path = Path(temporary.name) / "pricing.db"
    else:
        db_path = db_path.resolve()

    try:
        with _isolated_application_storage(db_path, keep_storage=keep_storage):
            ingest_started = time.perf_counter()
            import_stats: list[dict[str, Any]] = []
            for path in training_paths:
                import_stats.append(ingest_workbook(path, force=True))
            holdout_stats = ingest_workbook(
                holdout_path,
                exclude_prices=True,
                force=True,
            )
            ingest_runtime = time.perf_counter() - ingest_started
            project_id = holdout_stats.get("project_id")
            if not project_id:
                raise RuntimeError(
                    f"Holdout {holdout_path.name!r} did not create a BOQ project"
                )

            with db.db_session() as conn:
                holdout_source_id = int(
                    conn.execute(
                        "SELECT id FROM source_files WHERE filename=?",
                        (holdout_path.name,),
                    ).fetchone()[0]
                )
                ground_truth = _snapshot_ground_truth(conn, int(project_id))

            # Historical holdout rows are intentionally skipped by default in
            # run_pricing; force=True makes this explicit benchmark behavior.
            pricing_started = time.perf_counter()
            # Benchmarks are deterministic by default.  Semantic reranking is
            # an explicit opt-in experiment so a configured API key can never
            # silently change the baseline or incur external calls.
            run_policy = {
                "llm_enabled": bool(llm_enabled),
                "auto_threshold": AUTO_THRESHOLD,
            }
            run_result = run_pricing(
                int(project_id),
                policy=run_policy,
                force=True,
            )
            pricing_runtime = time.perf_counter() - pricing_started
            evaluation_started = time.perf_counter()
            data_gap: dict[str, Any]
            failure_analysis: dict[str, Any]
            data_requirements: dict[str, Any]
            temporal_modes: dict[str, Any]
            with db.db_session() as conn:
                evaluation = _evaluate(
                    conn,
                    int(project_id),
                    holdout_source_id,
                    ground_truth,
                    run_result,
                )
                catalog = {
                    "source_files": _count(conn, "source_files"),
                    "source_sheets": _count(conn, "source_sheets"),
                    "source_rows": _count(conn, "source_rows"),
                    "products": _count(conn, "products"),
                    "product_prices": _count(conn, "product_prices"),
                    "labor_items": _count(conn, "labor_items"),
                    "labor_rates": _count(conn, "labor_rates"),
                    "projects": _count(conn, "projects"),
                }
                data_gap = build_data_gap_report(
                    conn,
                    int(project_id),
                    run_id=int(run_result["run_id"]),
                )
                failure_analysis = build_failure_analysis(
                    conn,
                    int(project_id),
                    run_id=int(run_result["run_id"]),
                    ground_truth=ground_truth,
                    limit=50,
                )
                data_requirements = build_data_requirements(
                    data_gap,
                    failure_analysis=failure_analysis,
                )
                temporal_modes = evaluate_db_modes(
                    conn,
                    int(project_id),
                    run_id=int(run_result["run_id"]),
                    ground_truth=ground_truth,
                    # The holdout filename contains a compact project code
                    # rather than an unambiguous date.  Keep historical
                    # uncertainty explicit instead of guessing.
                    historical_as_of=None,
                    current_date=datetime.now(timezone.utc).date().isoformat(),
                    strict_historical_dates=False,
                    allow_undated=True,
                    auto_threshold=AUTO_THRESHOLD,
                )
            evaluation_runtime = time.perf_counter() - evaluation_started

            parsed_holdout = parse_workbook(holdout_path)
            row_classification = classify_parsed_workbook(parsed_holdout)
            parsing = {
                "workbooks_ingested": len(import_stats) + 1,
                "training_workbooks": len(training_paths),
                "sheets_inspected": sum(
                    int(stats.get("sheets", 0))
                    for stats in [*import_stats, holdout_stats]
                ),
                "source_rows": sum(
                    int(stats.get("source_rows", 0))
                    for stats in [*import_stats, holdout_stats]
                ),
                "data_rows": sum(
                    int(stats.get("data_rows", 0))
                    for stats in [*import_stats, holdout_stats]
                ),
                "descriptions_present": sum(
                    1
                    for sheet in parsed_holdout["sheets"]
                    for row in sheet.rows
                    if row["row_kind"] == "data"
                    and row["fields"].get("description")
                ),
                "quantities_present": sum(
                    1
                    for sheet in parsed_holdout["sheets"]
                    for row in sheet.rows
                    if row["row_kind"] == "data"
                    and row["fields"].get("quantity") is not None
                ),
            }
            report = {
                "benchmark": "holdout",
                "generated_at": _utc_now(),
                "runtime_seconds": {
                    "ingest": round(ingest_runtime, 3),
                    "pricing": round(pricing_runtime, 3),
                    "evaluation": round(evaluation_runtime, 3),
                    "total": round(time.perf_counter() - benchmark_started, 3),
                },
                "input_dir": _portable_report_path(input_dir),
                "holdout_file": holdout_path.name,
                "training_files": [path.name for path in training_paths],
                "database_path": (
                    _portable_report_path(db_path)
                    if database_persisted
                    else None
                ),
                "database_persisted": database_persisted,
                "parsing": parsing,
                "catalog": catalog,
                "imports": import_stats,
                "holdout_import": holdout_stats,
                "row_classification": row_classification,
                "evaluation": evaluation,
                "llm_experiment": {
                    "requested": bool(llm_enabled),
                    "enabled": bool(run_result.get("metrics", {}).get("llm_enabled")),
                    "model_usage": run_result.get("model_usage", {}),
                },
                "data_gap": data_gap,
                "failure_analysis": failure_analysis,
                "data_requirements": data_requirements,
                "temporal_modes": temporal_modes,
            }

        json_path = output_dir / "holdout-report.json"
        markdown_path = output_dir / "holdout-report.md"
        json_path.write_text(
            json.dumps(
                _compact_report_for_disk(report),
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ),
            encoding="utf-8",
        )
        markdown_path.write_text(_markdown_report(report), encoding="utf-8")
        # Keep the classification audit as standalone, reviewable artifacts as
        # well as embedding it in the machine-readable benchmark report.  The
        # files are written under the caller-selected output directory, so a
        # persistent benchmark run cannot accidentally overwrite operational
        # data or another run's database.
        row_classification_json = output_dir / "row-classification.json"
        row_classification_markdown = output_dir / "row-classification.md"
        row_classification_json.write_text(
            json.dumps(
                report["row_classification"],
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ),
            encoding="utf-8",
        )
        row_classification_markdown.write_text(
            _row_audit_markdown(report["row_classification"]),
            encoding="utf-8",
        )
        report["report_json"] = _portable_report_path(json_path)
        report["report_markdown"] = _portable_report_path(markdown_path)
        report["row_classification_json"] = _portable_report_path(
            row_classification_json
        )
        report["row_classification_markdown"] = _portable_report_path(
            row_classification_markdown
        )
        report_paths = write_report_files(
            output_dir,
            data_gap=report["data_gap"],
            failure_analysis=report["failure_analysis"],
            data_requirements=report["data_requirements"],
        )
        temporal_paths = write_temporal_report(
            output_dir,
            report["temporal_modes"],
        )
        report["artifact_paths"] = {
            **{
                key: _portable_report_path(path)
                for key, path in report_paths.items()
            },
            "temporal_json": _portable_report_path(temporal_paths["json"]),
            "temporal_markdown": _portable_report_path(
                temporal_paths["markdown"]
            ),
        }
        # Persist a second copy with the final report's machine-readable
        # metadata after artifact paths are known.  This keeps the ordinary
        # holdout report backward-compatible while exposing Round 2 artifacts.
        json_path.write_text(
            json.dumps(
                _compact_report_for_disk(report),
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ),
            encoding="utf-8",
        )
        return report
    finally:
        if temporary is not None:
            temporary.cleanup()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Directory containing .xls/.xlsx source workbooks.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for holdout-report.md and holdout-report.json.",
    )
    parser.add_argument(
        "--holdout",
        help="Exact filename, glob, or unique prefix for the holdout workbook.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        help="Optional persistent SQLite path. Defaults to a temporary database.",
    )
    parser.add_argument(
        "--keep-storage",
        action="store_true",
        help="Keep a persistent --db-path's raw storage files after completion.",
    )
    parser.add_argument(
        "--enable-llm",
        action="store_true",
        help="Opt in to the bounded semantic reranking experiment.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows PowerShell may expose a legacy cp1252 stream.  Reports remain
    # UTF-8 on disk, while the concise CLI summary should never fail merely
    # because a Vietnamese filename contains a character absent from the
    # console code page.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = _build_parser().parse_args(argv)
    report = run_benchmark(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        holdout=args.holdout,
        db_path=args.db_path,
        keep_storage=args.keep_storage,
        llm_enabled=args.enable_llm,
    )
    material = report["evaluation"]["material"]
    labor = report["evaluation"]["labor"]
    leakage = report["evaluation"]["leakage"]
    print(f"Holdout: {report['holdout_file']}")
    print(
        "Material coverage: "
        f"{material['coverage']:.1%}; exact accuracy: "
        f"{material['exact_price_accuracy']:.1%}"
    )
    print(
        "Labor coverage: "
        f"{labor['coverage']:.1%}; exact accuracy: "
        f"{labor['exact_price_accuracy']:.1%}"
    )
    print(f"Leakage guard: {'PASS' if leakage['passed'] else 'FAIL'}")
    print(f"JSON report: {report['report_json']}")
    print(f"Markdown report: {report['report_markdown']}")
    return 0 if leakage["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
