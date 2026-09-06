"""Generic data-gap and failure-analysis reports for the pricing engine.

The holdout benchmark deliberately keeps evaluation and reporting separate:
the benchmark owns leakage-safe ground truth, while this module turns a
persisted pricing run into an actionable source-coverage/failure report.  It
works with the current schema and tolerates optional columns added by later
migrations (for example ``line_class`` or ``status_reason``).

No report helper changes a price or filters rows out of the benchmark.  The
``source_supported`` metric is theoretical: it means that the catalog has at
least one usable source in the same normalized category.  ``capture_rate`` is
the engine's retrieval/pricing result over that source-supported subset.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.normalize import normalize_text, technical_attributes


CATEGORY_ORDER = (
    "Cable",
    "Wire",
    "Conduit",
    "Pipe",
    "Cable tray",
    "Lighting",
    "Switch/socket",
    "MCB/MCCB/Protection",
    "Panel",
    "Transformer",
    "Earthing",
    "Lightning protection",
    "Supports/accessories",
    "Civil works",
    "Labor-only",
    "External quotation",
    "Unknown",
)

_CATEGORY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Cable tray", ("cable_support", "thang cap", "mang cap", "khay cap", "cable tray", "cable ladder")),
    ("Lightning protection", ("chong set", "kim thu set", "kim thu", "lightning", "thu set")),
    ("MCB/MCCB/Protection", ("mcb", "mccb", "rccb", "rcbo", "aptomat", "cau chi", "bao ve")),
    ("Switch/socket", ("cong tac", "o cam", "switch", "socket", "receptacle")),
    ("Transformer", ("bien ap", "may bien ap", "transformer")),
    ("Panel", ("panel", "tu dien", "vo tu", "cabinet", "mdb", "msb", "db-")),
    ("Lighting", ("lighting", "chieu sang", "den ", "led", "luminaire", "tru den")),
    ("Earthing", ("earthing", "tiep dia", "coc tiep dia", "thanh tiep dia", "day dong tran")),
    ("Conduit", ("conduit", "ong luon", "ong pvc", "ong hdpe", "ong upvc")),
    ("Pipe", ("pipe", "ong ", "hdpe", "upvc", "ppr", "sprinkler")),
    ("Supports/accessories", ("support", "gia do", "ke", "kep ", "phu kien", "accessor", "han hoa nhiet")),
    ("Civil works", ("civil", "xay dung", "dao lap", "ho ga", "mong ", "tru ", "cot ")),
    ("Labor-only", ("labor", "nhan cong", "nhân công", "cong tac lap dat")),
    ("External quotation", ("bao gia ncc", "external quotation", "vendor quote", "dat hang")),
    ("Wire", ("wire", "day ", "vctf", "vctfk", "vcmd", "acsr")),
    ("Cable", ("cable", "cap ", "cxv", "cvv", "dsta", "data", "swa", "abc", "axv")),
)

_PRICEABLE_CLASSES = {
    "PRICEABLE_LINE_ITEM",
    "PRICEABLE",
    "LINE_ITEM",
}


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        result = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return result


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def normalize_category(description: Any, explicit: Any = None) -> str:
    """Map parser/catalog categories to the report's stable category labels."""

    explicit_text = normalize_text(explicit)
    text = normalize_text(description)
    combined = f"{explicit_text} {text}".strip()

    def marker_matches(marker: str) -> bool:
        marker = normalize_text(marker)
        # Short Vietnamese tokens such as ``ong`` (pipe) must not match the
        # middle of a longer word (``khong``). Markers ending in a space are
        # intentionally token-oriented in the vocabulary above.
        if marker.endswith(" ") or len(marker) <= 4:
            token = marker.strip()
            return bool(
                token
                and re.search(
                    rf"(?<![a-z0-9]){re.escape(token)}(?=\s|$)",
                    combined,
                )
            )
        return marker in combined

    for label, markers in _CATEGORY_ALIASES:
        if any(marker_matches(marker) for marker in markers):
            return label
    # Preserve useful broad parser categories when no vocabulary marker
    # matches. This still gives an explicit Unknown bucket for source-gap
    # reporting instead of manufacturing a category from a project name.
    if explicit_text in {"cable", "wire", "pipe", "lighting", "panel", "equipment"}:
        return {
            "cable": "Cable",
            "wire": "Wire",
            "pipe": "Pipe",
            "lighting": "Lighting",
            "panel": "Panel",
            "equipment": "External quotation",
        }[explicit_text]
    return "Unknown"


def _line_class(row: Any) -> str | None:
    value = _row_get(row, "line_class") or _row_get(row, "row_class") or _row_get(row, "classification")
    return normalize_text(value).upper().replace(" ", "_") if value else None


def is_priceable_row(row: Any) -> bool:
    """Use an explicit row class when present, with a conservative fallback."""

    classification = _line_class(row)
    if classification:
        if classification in _PRICEABLE_CLASSES:
            return True
        # Explicit non-priceable classifications are authoritative.
        if classification in {
            "SECTION",
            "SUBSECTION",
            "NOTE",
            "SUBTOTAL",
            "TOTAL",
            "HEADER",
            "NON_PRICEABLE_REFERENCE",
            "UNKNOWN",
        }:
            return False
    description = str(
        _row_get(row, "raw_description")
        or _row_get(row, "normalized_description")
        or ""
    ).strip()
    if not description:
        return False
    quantity = _safe_float(_row_get(row, "quantity"))
    unit = normalize_text(_row_get(row, "unit"))
    # A non-empty description with an operational field is retained as a
    # priceable line; headings/notes with neither are not counted.
    return quantity is not None or bool(unit) or any(
        _safe_float(_row_get(row, key)) is not None
        for key in ("material_price", "labor_price", "material_total", "labor_total")
    )


def _status_reason(row: Any) -> tuple[str, str]:
    status = str(_row_get(row, "status") or "UNKNOWN").upper()
    explicit_reason = (
        _row_get(row, "status_reason")
        or _row_get(row, "reason_code")
        or _row_get(row, "failure_reason")
    )
    if explicit_reason:
        return status, str(explicit_reason)
    if status in {"NO_MATCH", "MATCH_FAILURE"}:
        return status, "no_candidate_retrieved"
    if status in {"NO_PRICE_FOUND", "MISSING_SOURCE_DATA"}:
        return status, "candidate_without_usable_price"
    if status == "REVIEW_REQUIRED":
        return status, "confidence_or_specification_review"
    if status == "AUTO_APPROVED":
        return status, "priced_and_approved"
    return status, "unclassified_status"


def _catalog_source_index(conn: Any) -> dict[str, dict[str, int]]:
    """Return category-level counts of usable material/labor sources."""

    material: Counter[str] = Counter()
    labor: Counter[str] = Counter()
    product_query = """
        SELECT p.category, p.normalized_name
        FROM products p
        WHERE EXISTS (
            SELECT 1 FROM product_prices pp
            WHERE pp.product_id=p.id AND pp.is_approved=1 AND pp.net_price IS NOT NULL
        )
    """
    try:
        products = conn.execute(product_query).fetchall()
    except Exception:
        products = []
    for row in products:
        category = normalize_category(row["normalized_name"], row["category"])
        material[category] += 1
    labor_query = """
        SELECT li.category, li.normalized_name
        FROM labor_items li
        WHERE EXISTS (
            SELECT 1 FROM labor_rates lr
            WHERE lr.labor_item_id=li.id AND lr.rate IS NOT NULL
        )
    """
    try:
        labor_rows = conn.execute(labor_query).fetchall()
    except Exception:
        labor_rows = []
    for row in labor_rows:
        category = normalize_category(row["normalized_name"], row["category"])
        labor[category] += 1
    return {"material": dict(material), "labor": dict(labor)}


def _row_record(row: Any) -> dict[str, Any]:
    description = str(
        _row_get(row, "raw_description")
        or _row_get(row, "normalized_description")
        or ""
    )
    attrs = technical_attributes(description)
    category = normalize_category(description, attrs.get("category"))
    status, reason = _status_reason(row)
    material_id = _row_get(row, "matched_product_id")
    labor_id = _row_get(row, "matched_labor_item_id")
    material_price = _safe_float(_row_get(row, "material_price"))
    labor_price = _safe_float(_row_get(row, "labor_price"))
    return {
        "id": _row_get(row, "id"),
        "description": description,
        "category": category,
        "technical_attributes": attrs,
        "line_class": _row_get(row, "line_class") or _row_get(row, "row_class"),
        "quantity": _safe_float(_row_get(row, "quantity")),
        "unit": _row_get(row, "unit"),
        "status": status,
        "reason": reason,
        "material_candidate_id": int(material_id) if material_id is not None else None,
        "labor_candidate_id": int(labor_id) if labor_id is not None else None,
        "material_price": material_price,
        "labor_price": labor_price,
        "priced": material_price is not None or labor_price is not None,
        "matched": material_id is not None or labor_id is not None,
        "material_source": _loads(_row_get(row, "material_source_json"), {}),
        "labor_source": _loads(_row_get(row, "labor_source_json"), {}),
        "alternatives": _loads(_row_get(row, "alternatives_json"), {}),
    }


def build_data_gap_report(
    conn: Any,
    project_id: int,
    *,
    run_id: int | None = None,
) -> dict[str, Any]:
    """Build category-level theoretical source coverage for a pricing run."""

    query = "SELECT * FROM boq_items WHERE project_id=?"
    params: list[Any] = [project_id]
    if run_id is not None:
        query += " AND pricing_run_id=?"
        params.append(run_id)
    query += " ORDER BY id"
    rows = conn.execute(query, tuple(params)).fetchall()
    source_index = _catalog_source_index(conn)
    buckets: dict[str, dict[str, Any]] = {}
    row_records: list[dict[str, Any]] = []
    for row in rows:
        if not is_priceable_row(row):
            continue
        record = _row_record(row)
        row_records.append(record)
        category = record["category"]
        bucket = buckets.setdefault(
            category,
            {
                "category": category,
                "priceable_items": 0,
                "source_supported": 0,
                "matched": 0,
                "priced": 0,
                "material_source_supported": 0,
                "labor_source_supported": 0,
                "material_matched": 0,
                "labor_matched": 0,
                "material_priced": 0,
                "labor_priced": 0,
                "status_counts": Counter(),
                "reason_counts": Counter(),
                "material_source_count": source_index["material"].get(category, 0),
                "labor_source_count": source_index["labor"].get(category, 0),
            },
        )
        bucket["priceable_items"] += 1
        bucket["status_counts"][record["status"]] += 1
        bucket["reason_counts"][record["reason"]] += 1
        source_supported = bool(
            bucket["material_source_count"] or bucket["labor_source_count"]
        )
        bucket["source_supported"] += int(source_supported)
        bucket["material_source_supported"] += int(
            bucket["material_source_count"] > 0
        )
        bucket["labor_source_supported"] += int(bucket["labor_source_count"] > 0)
        bucket["matched"] += int(record["matched"])
        bucket["priced"] += int(record["priced"])
        bucket["material_matched"] += int(record["material_candidate_id"] is not None)
        bucket["labor_matched"] += int(record["labor_candidate_id"] is not None)
        bucket["material_priced"] += int(record["material_price"] is not None)
        bucket["labor_priced"] += int(record["labor_price"] is not None)

    def finalize(bucket: dict[str, Any]) -> dict[str, Any]:
        total = int(bucket["priceable_items"])
        supported = int(bucket["source_supported"])
        matched = int(bucket["matched"])
        priced = int(bucket["priced"])
        reason_counts = dict(bucket.pop("reason_counts"))
        status_counts = dict(bucket.pop("status_counts"))
        if supported == 0:
            main_gap = "MISSING_SOURCE_DATA"
        elif matched < total:
            main_gap = "MATCH_FAILURE"
        elif priced < matched:
            main_gap = "NO_PRICE_FOUND"
        elif reason_counts.get("insufficient_spec") or reason_counts.get("INSUFFICIENT_SPEC"):
            main_gap = "INSUFFICIENT_SPEC"
        elif status_counts.get("REVIEW_REQUIRED"):
            main_gap = "REVIEW_REQUIRED"
        else:
            main_gap = "NONE"
        return {
            **bucket,
            "coverage": round(priced / total, 4) if total else 0.0,
            "source_coverage": round(supported / total, 4) if total else 0.0,
            "capture_rate": round(matched / supported, 4) if supported else 0.0,
            "priced_capture_rate": round(priced / supported, 4) if supported else 0.0,
            "status_counts": dict(sorted(status_counts.items())),
            "reason_counts": dict(sorted(reason_counts.items())),
            "main_gap": main_gap,
        }

    categories = [finalize(buckets[key]) for key in CATEGORY_ORDER if key in buckets]
    categories.extend(
        finalize(buckets[key])
        for key in sorted(set(buckets) - set(CATEGORY_ORDER))
    )
    total = len(row_records)
    supported_total = sum(item["source_supported"] for item in categories)
    matched_total = sum(item["matched"] for item in categories)
    priced_total = sum(item["priced"] for item in categories)
    material_source_supported_total = sum(
        item["material_source_supported"] for item in categories
    )
    labor_source_supported_total = sum(
        item["labor_source_supported"] for item in categories
    )
    material_matched_total = sum(item["material_matched"] for item in categories)
    labor_matched_total = sum(item["labor_matched"] for item in categories)
    material_priced_total = sum(item["material_priced"] for item in categories)
    labor_priced_total = sum(item["labor_priced"] for item in categories)
    gap_counts = Counter(item["main_gap"] for item in categories if item["main_gap"] != "NONE")
    return {
        "schema_version": "1.0",
        "project_id": project_id,
        "run_id": run_id,
        "priceable_items": total,
        "source_supported": supported_total,
        "matched": matched_total,
        "priced": priced_total,
        "material_source_supported": material_source_supported_total,
        "labor_source_supported": labor_source_supported_total,
        "material_matched": material_matched_total,
        "labor_matched": labor_matched_total,
        "material_priced": material_priced_total,
        "labor_priced": labor_priced_total,
        "source_coverage": round(supported_total / total, 4) if total else 0.0,
        "engine_capture_rate": round(matched_total / supported_total, 4) if supported_total else 0.0,
        "priced_capture_rate": round(priced_total / supported_total, 4) if supported_total else 0.0,
        "material_source_coverage": round(
            material_source_supported_total / total, 4
        )
        if total
        else 0.0,
        "labor_source_coverage": round(labor_source_supported_total / total, 4)
        if total
        else 0.0,
        "material_capture_rate": round(
            material_matched_total / material_source_supported_total, 4
        )
        if material_source_supported_total
        else 0.0,
        "labor_capture_rate": round(
            labor_matched_total / labor_source_supported_total, 4
        )
        if labor_source_supported_total
        else 0.0,
        "material_priced_capture_rate": round(
            material_priced_total / material_source_supported_total, 4
        )
        if material_source_supported_total
        else 0.0,
        "labor_priced_capture_rate": round(
            labor_priced_total / labor_source_supported_total, 4
        )
        if labor_source_supported_total
        else 0.0,
        "priced_coverage": round(priced_total / total, 4) if total else 0.0,
        "main_gap_counts": dict(sorted(gap_counts.items())),
        "catalog_source_index": source_index,
        "categories": categories,
        "rows": row_records,
    }


def _relative_error(predicted: Any, actual: Any) -> float | None:
    predicted_value = _safe_float(predicted)
    actual_value = _safe_float(actual)
    if predicted_value is None or actual_value in (None, 0):
        return None
    return abs(predicted_value - actual_value) / abs(actual_value)


def _failure_code(record: Mapping[str, Any], source_available: bool) -> str:
    status = str(record.get("status") or "").upper()
    if status == "AUTO_APPROVED" and record.get("priced"):
        return "NONE"
    if status in {"MISSING_SOURCE_DATA", "EXTERNAL_QUOTATION_REQUIRED"}:
        return status
    if not source_available:
        return "MISSING_SOURCE_DATA"
    if not record.get("matched"):
        attrs = record.get("technical_attributes") or {}
        if len(attrs) <= 1 and not record.get("unit"):
            return "INSUFFICIENT_SPEC"
        return "MATCH_FAILURE"
    if not record.get("priced"):
        return "NO_PRICE_FOUND"
    if status == "REVIEW_REQUIRED":
        return "REVIEW_REQUIRED"
    return status or "UNKNOWN"


def _side_failure_code(
    record: Mapping[str, Any],
    *,
    side: str,
    source_available: bool,
) -> str:
    """Route one material/labor side without conflating the other side."""

    candidate_key = f"{side}_candidate_id"
    price_key = f"{side}_price"
    candidate_id = record.get(candidate_key)
    if candidate_id is None:
        if not source_available:
            return "MISSING_SOURCE_DATA"
        attrs = record.get("technical_attributes") or {}
        if len(attrs) <= 1 and not record.get("unit"):
            return "INSUFFICIENT_SPEC"
        return "MATCH_FAILURE"
    if record.get(price_key) is None:
        return "NO_PRICE_FOUND"
    if str(record.get("status") or "").upper() in {
        "REVIEW_REQUIRED",
        "PRICE_DRIFT_WARNING",
    }:
        return "REVIEW_REQUIRED"
    return "NONE"


def _candidate_summary(alternatives: Any, side: str, limit: int = 3) -> list[dict[str, Any]]:
    if not isinstance(alternatives, Mapping):
        return []
    values = alternatives.get(side)
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    for candidate in values[:limit]:
        if not isinstance(candidate, Mapping):
            continue
        # Failure reports can include prices for internal audit, but only
        # fields needed to diagnose retrieval/source issues are retained.
        result.append(
            {
                key: candidate.get(key)
                for key in ("id", "name", "code", "score", "explanation", "price")
                if candidate.get(key) not in (None, "")
            }
        )
    return result


def build_failure_analysis(
    conn: Any,
    project_id: int,
    *,
    run_id: int | None = None,
    ground_truth: Mapping[int | str, Mapping[str, Any]] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Build actionable material/labor failure and pricing-error samples."""

    report = build_data_gap_report(conn, project_id, run_id=run_id)
    rows = report["rows"]
    source_by_category = {
        item["category"]: {
            "material": bool(item.get("material_source_count")),
            "labor": bool(item.get("labor_source_count")),
            "combined": bool(
                item.get("material_source_count") or item.get("labor_source_count")
            ),
        }
        for item in report["categories"]
    }
    failures: list[dict[str, Any]] = []
    side_failures: dict[str, list[dict[str, Any]]] = {
        "material": [],
        "labor": [],
    }
    side_failure_counts: dict[str, Counter[str]] = {
        "material": Counter(),
        "labor": Counter(),
    }
    for record in rows:
        category_sources = source_by_category.get(
            record["category"],
            {"material": False, "labor": False, "combined": False},
        )
        failure_code = _failure_code(
            record,
            bool(category_sources.get("combined")),
        )
        if failure_code == "NONE":
            pass
        else:
            failures.append(
                {
                    "id": record["id"],
                    "description": record["description"],
                    "category": record["category"],
                    "technical_attributes": record["technical_attributes"],
                    "status": record["status"],
                    "reason": record["reason"],
                    "failure_code": failure_code,
                    "matched": record["matched"],
                    "priced": record["priced"],
                    "material_candidates": _candidate_summary(record["alternatives"], "material"),
                    "labor_candidates": _candidate_summary(record["alternatives"], "labor"),
                }
            )
        truth = (
            (ground_truth or {}).get(record["id"])
            or (ground_truth or {}).get(str(record["id"]))
            or {}
        )
        for side in ("material", "labor"):
            truth_price = _safe_float(truth.get(f"{side}_price"))
            # In a holdout, evaluate only sides that actually have a
            # ground-truth amount. In an operational report without truth,
            # retain unresolved sides so routing remains useful.
            relevant = (
                truth_price is not None and truth_price > 0
                if ground_truth
                else True
            )
            if not relevant:
                continue
            side_code = _side_failure_code(
                record,
                side=side,
                source_available=bool(category_sources.get(side)),
            )
            if side_code == "NONE":
                continue
            side_failure_counts[side][side_code] += 1
            side_failures[side].append(
                {
                    "id": record["id"],
                    "side": side,
                    "description": record["description"],
                    "category": record["category"],
                    "technical_attributes": record["technical_attributes"],
                    "status": record["status"],
                    "reason": record["reason"],
                    "failure_code": side_code,
                    "candidate_id": record.get(f"{side}_candidate_id"),
                    "predicted": record.get(f"{side}_price"),
                    "ground_truth": truth_price,
                    "source_available": bool(category_sources.get(side)),
                    "candidates": _candidate_summary(
                        record["alternatives"], side
                    ),
                }
            )
    failures.sort(
        key=lambda item: (
            0 if item["failure_code"] == "MISSING_SOURCE_DATA" else 1,
            0 if item["failure_code"] == "MATCH_FAILURE" else 1,
            int(item["id"] or 0),
        )
    )

    pricing_errors: list[dict[str, Any]] = []
    if ground_truth:
        truth_map = {str(key): value for key, value in ground_truth.items()}
        for record in rows:
            truth = truth_map.get(str(record["id"])) or {}
            for side, predicted_key, truth_key, source_key in (
                ("material", "material_price", "material_price", "material_source"),
                ("labor", "labor_price", "labor_price", "labor_source"),
            ):
                actual = truth.get(truth_key)
                predicted = record.get(predicted_key)
                error = _relative_error(predicted, actual)
                if error is None or error <= 0.005:
                    continue
                source = record.get(source_key) or {}
                if isinstance(source, Mapping):
                    source_type = source.get("source_type") or source.get("source_tier")
                else:
                    source_type = None
                if record.get("matched") and source_type in {
                    "historical_boq",
                    "historical_exact",
                    "historical_aggregate",
                }:
                    root_cause = "HISTORICAL_PRICE_DRIFT"
                elif source_type:
                    root_cause = "PRICING_POLICY_OR_TAX_BASIS"
                else:
                    root_cause = "SOURCE_OR_UNIT_MAPPING"
                pricing_errors.append(
                    {
                        "id": record["id"],
                        "side": side,
                        "description": record["description"],
                        "category": record["category"],
                        "matched_candidate_id": (
                            record["material_candidate_id"]
                            if side == "material"
                            else record["labor_candidate_id"]
                        ),
                        "source": source,
                        "predicted": predicted,
                        "ground_truth": actual,
                        "relative_error": round(error, 6),
                        "root_cause": root_cause,
                    }
                )
    pricing_errors.sort(key=lambda item: item["relative_error"], reverse=True)
    by_code = Counter(item["failure_code"] for item in failures)
    sample_limit = max(1, int(limit))
    return {
        "schema_version": "1.0",
        "project_id": project_id,
        "run_id": run_id,
        "failure_count": len(failures),
        "failure_counts": dict(sorted(by_code.items())),
        "failures": failures[:sample_limit],
        "material_failure_count": len(side_failures["material"]),
        "material_failure_counts": dict(
            sorted(side_failure_counts["material"].items())
        ),
        "material_failures": side_failures["material"][:sample_limit],
        "labor_failure_count": len(side_failures["labor"]),
        "labor_failure_counts": dict(
            sorted(side_failure_counts["labor"].items())
        ),
        "labor_failures": side_failures["labor"][:sample_limit],
        "pricing_error_count": len(pricing_errors),
        "pricing_errors": pricing_errors[:sample_limit],
        "data_gap": {
            key: value
            for key, value in report.items()
            if key not in {"rows"}
        },
    }


def build_data_requirements(
    data_gap: Mapping[str, Any],
    *,
    failure_analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn observed category gaps into concrete, non-project-specific asks."""

    source_requests = {
        "Cable": "Bảng giá cáp hạ thế/trung thế hiện hành, gồm mã, cấu tạo, đơn vị, list/net, VAT và ngày hiệu lực.",
        "Wire": "Bảng giá dây điện/dây điều khiển và dây dân dụng, gồm tiết diện, vật liệu ruột, đơn vị và giá ex-VAT.",
        "Conduit": "Bảng giá ống luồn dây (PVC/HDPE/ruột gà), phụ kiện và quy cách DN/đường kính.",
        "Pipe": "Bảng giá ống kỹ thuật/PPR/HDPE và phụ kiện theo đường kính, tiêu chuẩn và đơn vị.",
        "Cable tray": "Bảng giá thang/máng/khay cáp và phụ kiện theo kích thước, vật liệu, lớp mạ.",
        "Lighting": "Bảng giá đèn và thiết bị chiếu sáng (hãng, công suất, kiểu lắp, IP, VAT).",
        "Switch/socket": "Bảng giá công tắc, ổ cắm và thiết bị bảo vệ theo hãng, cực, dòng định mức.",
        "MCB/MCCB/Protection": "Bảng giá Schneider/ABB/LS hoặc hãng đang dùng cho MCB/MCCB/RCCB/RCBO.",
        "Panel": "BOM/tủ điện được duyệt và giá gia công, thiết bị trong tủ, phụ kiện và quy cách.",
        "Transformer": "Bảng giá máy biến áp/thiết bị nguồn theo công suất, điện áp và ngày hiệu lực.",
        "Earthing": "Bảng giá cọc/thanh/dây tiếp địa và vật tư chống sét theo vật liệu, kích thước.",
        "Lightning protection": "Bảng giá kim thu sét, dây thoát sét, phụ kiện và hồ sơ kỹ thuật.",
        "Supports/accessories": "Bảng giá giá đỡ, kẹp, phụ kiện, co nối và vật tư lắp đặt phụ.",
        "Civil works": "Đơn giá nhân công/vật tư xây dựng phụ trợ theo khu vực và đơn vị tính.",
        "Labor-only": "Bảng labor master đã điền đơn giá và ít nhất 2–3 báo giá dự án gần nhất.",
        "External quotation": "Danh sách nhà cung cấp/đầu mối để yêu cầu báo giá ngoài catalog.",
        "Unknown": "Danh mục chuẩn hóa hoặc mapping category cho các mô tả chưa đủ thông tin.",
    }
    categories = []
    for item in data_gap.get("categories") or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("main_gap") == "NONE" and item.get("source_coverage", 0) >= 0.9:
            continue
        category = str(item.get("category") or "Unknown")
        priceable = int(item.get("priceable_items") or 0)
        supported = int(item.get("source_supported") or 0)
        unlockable = max(0, priceable - supported)
        categories.append(
            {
                "category": category,
                "priceable_items": priceable,
                "source_supported": supported,
                "potential_unlock_items": unlockable,
                "main_gap": item.get("main_gap") or "UNKNOWN",
                "requested_source": source_requests.get(category, source_requests["Unknown"]),
            }
        )
    categories.sort(key=lambda item: item["potential_unlock_items"], reverse=True)
    return {
        "schema_version": "1.0",
        "priceable_items": int(data_gap.get("priceable_items") or 0),
        "source_supported": int(data_gap.get("source_supported") or 0),
        "potential_unlock_items": sum(item["potential_unlock_items"] for item in categories),
        "categories": categories,
        "failure_counts": dict((failure_analysis or {}).get("failure_counts") or {}),
    }


def render_data_gap_markdown(report: Mapping[str, Any]) -> str:
    def pct(value: Any) -> str:
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return "—"

    lines = [
        "# Data gap and achievable coverage report",
        "",
        f"- Project: `{report.get('project_id')}`",
        f"- Priceable line items: **{report.get('priceable_items', 0)}**",
        f"- Theoretical source-supported: **{report.get('source_supported', 0)}** ({pct(report.get('source_coverage'))})",
        f"- Engine matched: **{report.get('matched', 0)}**",
        f"- Priced: **{report.get('priced', 0)}** ({pct(report.get('priced_coverage'))})",
        f"- Engine capture rate on source-supported rows: **{pct(report.get('engine_capture_rate'))}**",
        "",
        "## Coverage by category",
        "",
        "| Category | Priceable | Source-supported | Matched | Priced | Source coverage | Capture rate | Main gap |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.get("categories") or []:
        lines.append(
            f"| {item.get('category')} | {item.get('priceable_items', 0)} | "
            f"{item.get('source_supported', 0)} | {item.get('matched', 0)} | "
            f"{item.get('priced', 0)} | {pct(item.get('source_coverage'))} | "
            f"{pct(item.get('capture_rate'))} | {item.get('main_gap')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "`source_supported` is a theoretical category-level upper bound: at "
            "least one usable product price or labor rate exists in the catalog. "
            "`engine_capture_rate` measures retrieval/matching within that "
            "subset. It is not a claim that every row shares the same product "
            "or labor identity.",
            "",
        ]
    )
    return "\n".join(lines)


def render_failure_markdown(report: Mapping[str, Any]) -> str:
    def render_side_section(
        title: str,
        values: Any,
    ) -> list[str]:
        lines = [
            "",
            f"## Top {title} failures",
            "",
            "| ID | Category | Description | Status | Failure code | Candidate evidence |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
        for item in values or []:
            if not isinstance(item, Mapping):
                continue
            description = str(item.get("description") or "").replace("|", "\\|")
            lines.append(
                f"| {item.get('id')} | {item.get('category')} | {description} | "
                f"{item.get('status')} | {item.get('failure_code')} | "
                f"{len(item.get('candidates') or [])} candidate(s) |"
            )
        return lines

    lines = [
        "# Pricing failure analysis",
        "",
        f"- Project: `{report.get('project_id')}`",
        f"- Failure rows sampled: **{len(report.get('failures') or [])}**",
        f"- Material failure rows sampled: **{len(report.get('material_failures') or [])}**",
        f"- Labor failure rows sampled: **{len(report.get('labor_failures') or [])}**",
        f"- Pricing errors sampled: **{len(report.get('pricing_errors') or [])}**",
        "",
        "## Failure reason counts",
        "",
        "| Reason | Rows |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| `{key}` | {value} |"
        for key, value in sorted((report.get("failure_counts") or {}).items())
    )
    lines.extend(
        [
            "",
            "## Top failures",
            "",
            "| ID | Category | Description | Status | Failure code | Candidate evidence |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for item in report.get("failures") or []:
        evidence = len(item.get("material_candidates") or []) + len(item.get("labor_candidates") or [])
        description = str(item.get("description") or "").replace("|", "\\|")
        lines.append(
            f"| {item.get('id')} | {item.get('category')} | {description} | "
            f"{item.get('status')} | {item.get('failure_code')} | {evidence} candidate(s) |"
        )
    lines.extend(
        render_side_section(
            "material",
            report.get("material_failures"),
        )
    )
    lines.extend(
        render_side_section(
            "labor",
            report.get("labor_failures"),
        )
    )
    lines.extend(
        [
            "",
            "## Top pricing errors",
            "",
            "| ID | Side | Category | Predicted | Ground truth | Relative error | Root cause |",
            "| ---: | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for item in report.get("pricing_errors") or []:
        lines.append(
            f"| {item.get('id')} | {item.get('side')} | {item.get('category')} | "
            f"{item.get('predicted')} | {item.get('ground_truth')} | "
            f"{float(item.get('relative_error') or 0):.1%} | {item.get('root_cause')} |"
        )
    lines.extend(
        [
            "",
            "Failure codes are diagnostic routing labels, not proof of a single "
            "root cause. Review the candidate/provenance evidence before changing "
            "a production pricing rule.",
            "",
        ]
    )
    return "\n".join(lines)


def render_data_requirements_markdown(requirements: Mapping[str, Any]) -> str:
    lines = [
        "# Data requirements next",
        "",
        "This list is generated from source-gap observations. It estimates "
        "potentially unlockable rows; it does not promise exact coverage.",
        "",
        f"- Priceable rows observed: **{requirements.get('priceable_items', 0)}**",
        f"- Source-supported rows: **{requirements.get('source_supported', 0)}**",
        f"- Potential unlock rows: **{requirements.get('potential_unlock_items', 0)}**",
        "",
        "| Priority category | Current gap | Potential rows | Requested data |",
        "| --- | --- | ---: | --- |",
    ]
    for item in requirements.get("categories") or []:
        requested = str(item.get("requested_source") or "").replace("|", "\\|")
        lines.append(
            f"| {item.get('category')} | {item.get('main_gap')} | "
            f"{item.get('potential_unlock_items', 0)} | {requested} |"
        )
    lines.extend(
        [
            "",
            "Recommended order: add current supplier net-price files first, then "
            "labor master plus 2–3 recent project quotations, then fill external "
            "quotation contacts for categories that are intentionally out of "
            "catalog.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report_files(
    output_dir: Path,
    *,
    data_gap: Mapping[str, Any],
    failure_analysis: Mapping[str, Any] | None = None,
    data_requirements: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Write JSON/Markdown report artifacts and return absolute paths."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    failure_analysis = failure_analysis or {}
    data_requirements = data_requirements or build_data_requirements(
        data_gap,
        failure_analysis=failure_analysis,
    )
    paths = {
        "data_gap_json": output_dir / "data-gap-report.json",
        "data_gap_markdown": output_dir / "data-gap-report.md",
        "failure_analysis_markdown": output_dir / "failure-analysis.md",
        "data_requirements_markdown": output_dir.parent / "docs" / "DATA-REQUIREMENTS-NEXT.md",
    }
    paths["data_requirements_markdown"].parent.mkdir(parents=True, exist_ok=True)
    paths["data_gap_json"].write_text(
        json.dumps(data_gap, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    paths["data_gap_markdown"].write_text(
        render_data_gap_markdown(data_gap),
        encoding="utf-8",
    )
    paths["failure_analysis_markdown"].write_text(
        render_failure_markdown(failure_analysis),
        encoding="utf-8",
    )
    paths["data_requirements_markdown"].write_text(
        render_data_requirements_markdown(data_requirements),
        encoding="utf-8",
    )
    return {key: str(path.resolve()) for key, path in paths.items()}


__all__ = [
    "CATEGORY_ORDER",
    "build_data_gap_report",
    "build_data_requirements",
    "build_failure_analysis",
    "is_priceable_row",
    "normalize_category",
    "render_data_gap_markdown",
    "render_data_requirements_markdown",
    "render_failure_markdown",
    "write_report_files",
]
