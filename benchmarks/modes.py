"""Temporal benchmark modes for the DH M&E pricing engine.

Two questions are routinely mixed together when an old BOQ is evaluated
against a modern catalog:

``historical_reproduction``
    Could the engine reproduce the price that was available at the BOQ's
    quotation date, using only observations eligible on that date?

``current_repricing``
    What coverage and price drift do we get when the same BOQ is priced using
    the current catalog?

This module keeps the temporal semantics in the benchmark layer.  It does not
change the application pricing engine, mutate catalog rows, or call an LLM.
The DB adapter is intentionally thin; the pure ``evaluate_mode_rows`` helper
is useful for unit tests and for benchmark runners that already have a
ground-truth snapshot.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.normalize import normalize_text, technical_attributes
from app.price_policy import normalize_price_basis, normalize_tax_mode

HISTORICAL_REPRODUCTION = "historical_reproduction"
CURRENT_REPRICING = "current_repricing"
BENCHMARK_MODES = (HISTORICAL_REPRODUCTION, CURRENT_REPRICING)

_MODE_ALIASES = {
    "historical": HISTORICAL_REPRODUCTION,
    "historical_reproduction": HISTORICAL_REPRODUCTION,
    "historical-reproduction": HISTORICAL_REPRODUCTION,
    "reproduction": HISTORICAL_REPRODUCTION,
    "current": CURRENT_REPRICING,
    "current_repricing": CURRENT_REPRICING,
    "current-repricing": CURRENT_REPRICING,
    "repricing": CURRENT_REPRICING,
}

_HISTORICAL_SOURCE_TYPES = {
    "historical",
    "historical_boq",
    "historical_quotation",
    "project_quotation",
}


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def parse_date(value: Any) -> date | None:
    """Parse common ISO/Excel date representations without guessing silently."""

    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def date_text(value: Any) -> str | None:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None


def normalize_mode(mode: str | None) -> str:
    """Normalize mode aliases and fail loudly for unsupported semantics."""

    value = str(mode or CURRENT_REPRICING).strip().lower().replace(" ", "_")
    normalized = _MODE_ALIASES.get(value, value)
    if normalized not in BENCHMARK_MODES:
        raise ValueError(
            f"Unsupported benchmark mode {mode!r}; "
            f"choose one of {', '.join(BENCHMARK_MODES)}."
        )
    return normalized


@dataclass(frozen=True)
class BenchmarkModeSpec:
    """Temporal contract for one evaluation run."""

    mode: str
    as_of: str | None
    as_of_source: str
    allow_undated: bool = True
    strict_dates: bool = False

    @property
    def historical(self) -> bool:
        return self.mode == HISTORICAL_REPRODUCTION

    @property
    def temporal_semantics(self) -> str:
        if self.as_of:
            return (
                "historical_price_eligibility"
                if self.historical
                else "current_catalog_as_of_date"
            )
        return "undated_temporal_semantics"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "as_of": self.as_of,
            "as_of_source": self.as_of_source,
            "allow_undated": self.allow_undated,
            "strict_dates": self.strict_dates,
            "temporal_semantics": self.temporal_semantics,
            "uncertain": self.as_of is None,
        }


def infer_benchmark_date(
    *,
    explicit: Any = None,
    project: Mapping[str, Any] | None = None,
    source_file: Mapping[str, Any] | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """Infer an evaluation date and expose where it came from.

    Filename inference intentionally accepts only unambiguous separated dates
    (``29-01-2024``, ``2024-01-29``). A compact identifier such as ``DH290124``
    is reported as unknown rather than being guessed as a date.
    """

    candidates: list[tuple[str, Any]] = []
    if explicit not in (None, ""):
        candidates.append(("explicit", explicit))
    if project:
        for key in ("quotation_date", "effective_date", "as_of"):
            if project.get(key) not in (None, ""):
                candidates.append((f"project.{key}", project[key]))
        metadata = _loads(project.get("metadata_json", project.get("metadata")), {})
        for key in ("quotation_date", "effective_date", "as_of"):
            if isinstance(metadata, Mapping) and metadata.get(key) not in (None, ""):
                candidates.append((f"project.metadata.{key}", metadata[key]))
    if source_file:
        metadata = _loads(
            source_file.get("metadata_json", source_file.get("metadata")),
            {},
        )
        for key in ("effective_date", "quotation_date", "as_of"):
            if source_file.get(key) not in (None, ""):
                candidates.append((f"source_file.{key}", source_file[key]))
            if isinstance(metadata, Mapping) and metadata.get(key) not in (None, ""):
                candidates.append((f"source_file.metadata.{key}", metadata[key]))
    if filename:
        # Keep this local to avoid relying on parser internals and make the
        # date rule obvious in benchmark reports.
        patterns = (
            (r"(?<!\d)(\d{2})[-/.](\d{2})[-/.](\d{4})(?!\d)", "%d-%m-%Y"),
            (r"(?<!\d)(\d{4})[-/.](\d{2})[-/.](\d{2})(?!\d)", "%Y-%m-%d"),
        )
        for pattern, fmt in patterns:
            match = re.search(pattern, filename)
            if not match:
                continue
            try:
                parsed = datetime.strptime("-".join(match.groups()), fmt).date()
            except ValueError:
                continue
            candidates.append(("filename.separated_date", parsed))
            break

    for source, value in candidates:
        parsed = parse_date(value)
        if parsed:
            return {
                "date": parsed.isoformat(),
                "source": source,
                "confidence": "high" if source == "explicit" else "medium",
                "uncertain": False,
                "candidates": [
                    {"source": item_source, "date": date_text(item_value)}
                    for item_source, item_value in candidates
                    if parse_date(item_value)
                ],
            }
    return {
        "date": None,
        "source": "missing",
        "confidence": "unknown",
        "uncertain": True,
        "candidates": [],
    }


def make_mode_spec(
    mode: str,
    *,
    as_of: Any = None,
    project: Mapping[str, Any] | None = None,
    source_file: Mapping[str, Any] | None = None,
    filename: str | None = None,
    current_date: Any = None,
    allow_undated: bool = True,
    strict_dates: bool = False,
) -> BenchmarkModeSpec:
    """Create a mode contract with explicit date provenance."""

    normalized = normalize_mode(mode)
    if normalized == CURRENT_REPRICING:
        # Current repricing intentionally ignores the historical project's
        # quotation/effective date. Only an explicit current as-of override
        # may replace the runtime/current date.
        explicit_current = parse_date(as_of)
        fallback = explicit_current or parse_date(current_date) or datetime.now(timezone.utc).date()
        inferred = {
            "date": fallback.isoformat(),
            "source": "explicit" if explicit_current else "runtime.current_date",
            "confidence": "high" if explicit_current else "medium",
            "uncertain": False,
        }
    else:
        inferred = infer_benchmark_date(
            explicit=as_of,
            project=project,
            source_file=source_file,
            filename=filename,
        )
    return BenchmarkModeSpec(
        mode=normalized,
        as_of=inferred["date"],
        as_of_source=inferred["source"],
        allow_undated=allow_undated,
        strict_dates=strict_dates,
    )


def observation_date(observation: Mapping[str, Any]) -> date | None:
    """Read an observation's effective/project/created date in priority order."""

    for key in (
        "effective_date",
        "project_quotation_date",
        "quotation_date",
        "observed_date",
        "created_at",
    ):
        parsed = parse_date(observation.get(key))
        if parsed:
            return parsed
    return None


def observation_source_type(observation: Mapping[str, Any]) -> str:
    policy = _loads(observation.get("policy_json", observation.get("policy")), {})
    context = _loads(
        observation.get("context_json", observation.get("context")), {}
    )
    calculation = _loads(
        observation.get("calc_json", observation.get("calc")), {}
    )
    value = (
        observation.get("source_type")
        or (policy.get("source_type") if isinstance(policy, Mapping) else None)
        or observation.get("observation_type")
        or (
            context.get("source_type")
            if isinstance(context, Mapping)
            else None
        )
        or (
            calculation.get("source_type")
            if isinstance(calculation, Mapping)
            else None
        )
        or observation.get("source_tier")
        or observation.get("supplier")
        or "unknown"
    )
    return normalize_text(value).replace(" ", "_") or "unknown"


def observation_selection_explicit(observation: Mapping[str, Any]) -> bool:
    """Return whether ingestion/policy marked an observation as selected."""

    if observation.get("selected") is True or observation.get("is_selected") is True:
        return True
    for field in ("context", "context_json", "calc", "calc_json"):
        value = observation.get(field)
        if field.endswith("_json"):
            value = _loads(value, {})
        if not isinstance(value, Mapping):
            continue
        if value.get("selected") is True or value.get("is_selected") is True:
            return True
        selection = value.get("selection")
        if isinstance(selection, str) and selection.strip():
            return True
    return bool(observation.get("calculated") is True)


def temporal_observation_status(
    observation: Mapping[str, Any],
    spec: BenchmarkModeSpec,
) -> str:
    """Return ``eligible``, ``future_source`` or ``undated_source``."""

    observed = observation_date(observation)
    cutoff = parse_date(spec.as_of)
    if observed is None:
        if spec.strict_dates or not spec.allow_undated:
            return "undated_excluded"
        return "undated_source"
    if cutoff is not None and observed > cutoff:
        return "future_source"
    return "eligible"


def eligible_observations(
    observations: Iterable[Mapping[str, Any]],
    spec: BenchmarkModeSpec,
) -> tuple[list[Mapping[str, Any]], dict[str, int]]:
    """Filter observations by mode without mutating input rows."""

    counts: Counter[str] = Counter()
    eligible: list[Mapping[str, Any]] = []
    for observation in observations:
        status = temporal_observation_status(observation, spec)
        counts[status] += 1
        if status in {"eligible", "undated_source"}:
            eligible.append(observation)
    eligible.sort(
        key=lambda item: (
            observation_date(item) is not None,
            observation_date(item) or date.min,
            int(_safe_float(item.get("id")) or 0),
        ),
        reverse=True,
    )
    return eligible, dict(sorted(counts.items()))


def _lookup_observations(
    source_observations: Mapping[Any, Sequence[Mapping[str, Any]]] | None,
    side: str,
    entity_id: Any,
) -> list[Mapping[str, Any]]:
    if not source_observations or entity_id in (None, ""):
        return []
    try:
        parsed_id = int(entity_id)
    except (TypeError, ValueError):
        parsed_id = entity_id
    keys = (
        (side, parsed_id),
        f"{side}:{parsed_id}",
        parsed_id,
        str(parsed_id),
    )
    for key in keys:
        value = source_observations.get(key)
        if value is not None:
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _source_json(row: Any, side: str) -> dict[str, Any]:
    return _loads(
        _row_get(row, f"{side}_source_json", _row_get(row, f"{side}_source")),
        {},
    )


def _truth_value(
    ground_truth: Mapping[Any, Mapping[str, Any]] | None,
    row_id: Any,
    key: str,
    row: Any,
) -> float | None:
    if ground_truth:
        truth = ground_truth.get(row_id, ground_truth.get(str(row_id), {}))
        if isinstance(truth, Mapping):
            value = _safe_float(truth.get(key))
            if value is not None:
                return value
    # Never treat a post-pricing row's prediction as its own ground truth.
    # Historical/current accuracy callers must pass a snapshot captured before
    # the pricing run.
    return None


def _is_priceable(row: Any) -> bool:
    try:
        # Import lazily so this module stays usable if report helpers are
        # deployed independently of the application package.
        from benchmarks.reports import is_priceable_row

        return bool(is_priceable_row(row))
    except Exception:
        description = str(
            _row_get(row, "raw_description")
            or _row_get(row, "normalized_description")
            or ""
        ).strip()
        return bool(
            description
            and (
                _row_get(row, "quantity") is not None
                or _row_get(row, "unit") not in (None, "")
            )
        )


def _relative_error(predicted: Any, actual: Any) -> float | None:
    pred = _safe_float(predicted)
    truth = _safe_float(actual)
    if pred is None or truth in (None, 0):
        return None
    return abs(pred - truth) / abs(truth)


def _price_equal(predicted: Any, actual: Any) -> bool:
    pred = _safe_float(predicted)
    truth = _safe_float(actual)
    return bool(
        pred is not None
        and truth is not None
        and math.isclose(pred, truth, rel_tol=0.005, abs_tol=1.0)
    )


def _source_date_for_row(source: Mapping[str, Any]) -> date | None:
    provenance = source.get("provenance")
    if isinstance(provenance, Sequence) and not isinstance(provenance, (str, bytes)):
        dates = [
            observation_date(item)
            for item in provenance
            if isinstance(item, Mapping) and observation_date(item)
        ]
        if dates:
            return max(dates)
    return observation_date(source)


def _source_temporal_status(source: Mapping[str, Any], spec: BenchmarkModeSpec) -> str:
    # A selected labor policy may carry source coordinates in a provenance
    # array instead of flattening effective_date onto the outer payload.
    source_date = _source_date_for_row(source)
    if source_date is None:
        return "undated_source" if spec.allow_undated and not spec.strict_dates else "undated_excluded"
    cutoff = parse_date(spec.as_of)
    return "future_source" if cutoff and source_date > cutoff else "eligible"


def observation_amount(
    observation: Mapping[str, Any],
    *,
    side: str,
) -> float | None:
    """Return the numeric price carried by one source observation."""

    keys = (
        ("net_price", "amount", "price", "list_price")
        if side == "material"
        else ("rate", "amount", "price", "net_price")
    )
    for key in keys:
        value = _safe_float(observation.get(key))
        if value is not None and value >= 0:
            return value
    return None


def _source_priority(observation: Mapping[str, Any], side: str) -> int:
    """Mirror the explainable source-tier order used by pricing policy."""

    source_type = observation_source_type(observation)
    supplier = normalize_text(observation.get("supplier") or "").replace(" ", "_")
    if side == "labor":
        if source_type in {
            "labor_master",
            "approved_internal",
            "internal_master",
            "internal_approved",
        }:
            return 0
        if source_type in _HISTORICAL_SOURCE_TYPES:
            return 1
        if source_type in {"manual", "manual_review", "manual_quote", "manual_approved"}:
            return 2
        return 3
    if source_type in {
        "supplier_price",
        "supplier_net",
        "current_supplier_net",
        "supplier_list",
        "supplier_discounted",
    }:
        return 0
    if supplier and supplier != "historical_quotation":
        return 0
    if source_type in {"approved_internal", "internal_approved", "internal_master"}:
        return 1
    if source_type in _HISTORICAL_SOURCE_TYPES or supplier == "historical_quotation":
        return 2
    if source_type in {"manual", "manual_review", "manual_quote", "manual_approved"}:
        return 3
    return 4


def select_mode_observation(
    observations: Iterable[Mapping[str, Any]],
    *,
    side: str,
) -> tuple[float | None, Mapping[str, Any] | None]:
    """Select one eligible exact-item observation deterministically.

    Temporal eligibility is handled before this function. The remaining
    selection follows source quality, then recency and stable observation ID.
    """

    usable = [
        item
        for item in observations
        if observation_amount(item, side=side) is not None
    ]
    if not usable:
        return None, None
    usable.sort(
        key=lambda item: (
            -_source_priority(item, side),
            observation_date(item) is not None,
            observation_date(item) or date.min,
            1 if observation_selection_explicit(item) else 0,
            int(_safe_float(item.get("id")) or 0),
        ),
        reverse=True,
    )
    selected = usable[0]
    return observation_amount(selected, side=side), selected


def classify_price_error(
    *,
    predicted: Any,
    actual: Any,
    source: Mapping[str, Any] | None = None,
    alternatives: Sequence[Mapping[str, Any]] | None = None,
    target_date: Any = None,
) -> dict[str, Any]:
    """Classify a price mismatch with evidence, without overclaiming.

    The returned ``root_cause`` is a diagnostic hypothesis.  It is deliberately
    accompanied by ``evidence`` and ``confidence`` so a human reviewer can
    distinguish a measured ratio from a proven commercial explanation.
    """

    predicted_value = _safe_float(predicted)
    actual_value = _safe_float(actual)
    source = source if isinstance(source, Mapping) else {}
    error = _relative_error(predicted_value, actual_value)
    if predicted_value is None or actual_value in (None, 0) or error is None:
        return {
            "root_cause": "NOT_EVALUABLE",
            "confidence": "low",
            "relative_error": None,
            "evidence": [],
            "explanation": "Thiếu giá dự đoán hoặc giá tham chiếu hợp lệ.",
        }

    ratio = predicted_value / actual_value if actual_value else None
    evidence: list[str] = [f"predicted/actual={ratio:.4f}"]
    source_type = observation_source_type(source)
    tax_mode = normalize_tax_mode(source.get("tax_mode"), default="")
    discount = _safe_float(source.get("discount", source.get("discount_rate")))
    list_price = _safe_float(source.get("list_price"))
    source_date = _source_date_for_row(source)
    target = parse_date(target_date)

    if source_date and target and source_date > target:
        evidence.append(f"source_date={source_date.isoformat()} > target={target.isoformat()}")
        return {
            "root_cause": "FUTURE_SOURCE_USED",
            "confidence": "high",
            "relative_error": round(error, 6),
            "evidence": evidence,
            "explanation": "Nguồn giá được áp dụng sau ngày benchmark; không hợp lệ cho historical reproduction.",
        }

    # If the selected source has a list amount but no discount, a lower ground
    # truth is consistent with an unrecorded negotiated discount.
    if (
        list_price is not None
        and predicted_value >= actual_value
        and discount in (None, 0.0)
        and source_type not in _HISTORICAL_SOURCE_TYPES
        and error >= 0.02
    ):
        evidence.append("source has list_price but no non-zero discount")
        return {
            "root_cause": "MISSING_DISCOUNT",
            "confidence": "medium",
            "relative_error": round(error, 6),
            "evidence": evidence,
            "explanation": "Giá áp dụng có vẻ là list price; workbook/policy chưa chứng minh discount.",
        }

    # VAT mismatch is only a hypothesis unless the source exposes tax basis or
    # the ratio is close to common Vietnamese VAT rates. Evaluate it after the
    # explicit list/discount evidence above: a source explicitly marked
    # ex-VAT with a zero discount is stronger evidence for MISSING_DISCOUNT.
    vat_ratios = (1.08, 1.10, 1.05, 1.1 / 1.08, 1.08 / 1.1)
    near_vat = ratio is not None and any(abs(ratio - candidate) <= 0.015 for candidate in vat_ratios)
    if near_vat or tax_mode == "inc_vat":
        evidence.append(f"tax_mode={tax_mode or 'unknown'}")
        return {
            "root_cause": "VAT_BASIS_MISMATCH",
            "confidence": "medium",
            "relative_error": round(error, 6),
            "evidence": evidence,
            "explanation": "Tỷ lệ lệch gần hệ số VAT hoặc nguồn có tax basis khác nhau.",
        }

    # A close alternative is strong evidence that source priority, rather than
    # identity matching, drove the error.
    best_alternative: Mapping[str, Any] | None = None
    best_alternative_error: float | None = None
    for alternative in alternatives or ():
        if not isinstance(alternative, Mapping):
            continue
        alt_error = _relative_error(alternative.get("price"), actual_value)
        if alt_error is None:
            continue
        if best_alternative_error is None or alt_error < best_alternative_error:
            best_alternative_error = alt_error
            best_alternative = alternative
    if (
        best_alternative is not None
        and best_alternative_error is not None
        and best_alternative_error + 0.02 < error
    ):
        evidence.append(
            f"alternative_id={best_alternative.get('id')} error={best_alternative_error:.4f}"
        )
        return {
            "root_cause": "SOURCE_PRIORITY_PROBLEM",
            "confidence": "medium",
            "relative_error": round(error, 6),
            "evidence": evidence,
            "alternative": {
                key: best_alternative.get(key)
                for key in ("id", "name", "price", "score")
            },
            "explanation": "Một candidate/source thay thế khớp giá tham chiếu tốt hơn đáng kể.",
        }

    # Integer ratios often indicate a unit conversion, parallel-run multiplier
    # or bundle-vs-single-item mismatch. Keep the label broad and actionable.
    if ratio is not None:
        for value in (ratio, 1.0 / ratio if ratio else None):
            if value is None:
                continue
            rounded = round(value)
            if rounded >= 2 and rounded <= 20 and abs(value - rounded) <= 0.03:
                evidence.append(f"integer_ratio≈{rounded}")
                return {
                    "root_cause": "UNIT_OR_BUNDLE_MULTIPLIER",
                    "confidence": "medium",
                    "relative_error": round(error, 6),
                    "evidence": evidence,
                    "explanation": "Tỷ lệ giá gần số nguyên; kiểm tra đơn vị, bó hoặc số tuyến song song.",
                }

    if source_type in _HISTORICAL_SOURCE_TYPES:
        evidence.append(f"source_type={source_type}")
        return {
            "root_cause": "HISTORICAL_PRICE_DRIFT",
            "confidence": "low",
            "relative_error": round(error, 6),
            "evidence": evidence,
            "explanation": "Giá lịch sử khác giá tham chiếu; có thể là trượt giá hoặc giá thương lượng theo dự án.",
        }

    if source_type not in {"unknown", ""}:
        evidence.append(f"source_type={source_type}")
    return {
        "root_cause": "SOURCE_OR_POLICY_MISMATCH",
        "confidence": "low",
        "relative_error": round(error, 6),
        "evidence": evidence,
        "explanation": "Chưa đủ bằng chứng để quy kết discount, VAT, đơn vị hay giá thương lượng.",
    }


def _side_source_key(side: str) -> str:
    return f"{side}_source_json"


def _evaluate_side(
    rows: Sequence[Any],
    *,
    side: str,
    spec: BenchmarkModeSpec,
    ground_truth: Mapping[Any, Mapping[str, Any]] | None,
    source_observations: Mapping[Any, Sequence[Mapping[str, Any]]] | None,
    auto_threshold: float,
) -> dict[str, Any]:
    predicted_key = f"{side}_price"
    matched_key = "matched_product_id" if side == "material" else "matched_labor_item_id"
    truth_key = f"{side}_price"
    source_counts: Counter[str] = Counter()
    errors: list[float] = []
    pricing_errors: list[dict[str, Any]] = []
    evaluable = predicted = exact = source_supported = 0
    applicable = repriced = source_supported_evaluable = 0
    high_conf = high_conf_error = temporal_excluded = undated = future = 0
    provenance_missing = 0
    selection_origins: Counter[str] = Counter()
    future_observations_excluded = 0

    for row in rows:
        row_id = _row_get(row, "id")
        actual = _truth_value(ground_truth, row_id, truth_key, row)
        if actual is not None and actual > 0:
            evaluable += 1
        entity_id = _row_get(row, matched_key)
        predicted_value = _safe_float(_row_get(row, predicted_key))
        side_applicable = bool(
            (actual is not None and actual > 0)
            or entity_id not in (None, "")
            or predicted_value is not None
        )
        if not side_applicable:
            continue
        applicable += 1
        observations = _lookup_observations(source_observations, side, entity_id)
        eligible, statuses = eligible_observations(observations, spec)
        source_counts.update(statuses)
        future_observations_excluded += int(statuses.get("future_source", 0))
        source = _source_json(row, side)
        source_status = _source_temporal_status(source, spec) if source else "missing_source"
        if source_status == "future_source":
            future += 1
        elif source_status in {"undated_source", "undated_excluded"}:
            undated += 1
        # A row's applied source is considered valid in this mode only when it
        # is temporally eligible. If no source coordinates exist, retain the
        # value for current coverage but surface provenance/temporal uncertainty.
        source_usable = source_status in {"eligible", "undated_source"}
        supported = bool(eligible or (source and source_usable))
        source_supported += int(supported)
        if supported and actual is not None and actual > 0:
            source_supported_evaluable += 1
        projected_value, projected_source = select_mode_observation(
            eligible,
            side=side,
        )
        if projected_value is not None and projected_source is not None:
            predicted_for_mode = projected_value
            selected_source = dict(projected_source)
            selection_origin = "eligible_catalog_observation"
            if predicted_value is not None and source_status in {
                "future_source",
                "undated_excluded",
            }:
                temporal_excluded += 1
        elif predicted_value is not None and source_usable:
            # A caller may provide a row with only a flattened provenance
            # payload and no observation index. Preserve that value when it is
            # temporally eligible; otherwise the catalog projection above is
            # the source of truth for this mode.
            predicted_for_mode = predicted_value
            selected_source = source
            selection_origin = "applied_price"
        else:
            if predicted_value is not None and (
                source_status == "future_source"
                or (not eligible and bool(observations))
            ):
                temporal_excluded += 1
            predicted_for_mode = None
            selected_source = source
            selection_origin = "no_eligible_price"
        selection_origins[selection_origin] += 1
        if predicted_for_mode is not None and (
            source_usable or selection_origin == "eligible_catalog_observation"
        ):
            repriced += 1
            if actual is not None and actual > 0:
                predicted += 1
                err = _relative_error(predicted_for_mode, actual)
                if err is not None:
                    errors.append(err)
                if _price_equal(predicted_for_mode, actual):
                    exact += 1
                confidence = _safe_float(
                    _row_get(
                        row,
                        f"{side}_confidence",
                    )
                )
                if confidence is not None and confidence >= auto_threshold:
                    high_conf += 1
                    if not _price_equal(predicted_for_mode, actual):
                        high_conf_error += 1
                if err is not None and err > 0.005:
                    pricing_errors.append(
                        {
                            "id": row_id,
                            "side": side,
                            "description": _row_get(row, "raw_description")
                            or _row_get(row, "normalized_description"),
                            "predicted": predicted_for_mode,
                            "ground_truth": actual,
                            "source": selected_source,
                            "selection_origin": selection_origin,
                            "root_cause": classify_price_error(
                                predicted=predicted_for_mode,
                                actual=actual,
                                source=selected_source,
                                alternatives=(
                                    _loads(_row_get(row, "alternatives_json"), {}).get(side, [])
                                    if isinstance(_loads(_row_get(row, "alternatives_json"), {}), Mapping)
                                    else []
                                ),
                                target_date=spec.as_of,
                            ),
                        }
                    )
            if not selected_source:
                provenance_missing += 1
        elif predicted_value is not None and not source:
            provenance_missing += 1

    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    return {
        "evaluable_items": evaluable,
        "applicable_items": applicable,
        "source_supported_items": source_supported,
        "predicted_items": predicted,
        "repriced_items": repriced,
        "coverage": ratio(predicted, evaluable),
        "repricing_coverage": ratio(repriced, applicable),
        "source_coverage": ratio(source_supported, applicable),
        "capture_rate": ratio(repriced, source_supported),
        "evaluation_capture_rate": ratio(predicted, source_supported_evaluable),
        "exact_price_match": exact,
        "exact_price_accuracy": ratio(exact, evaluable),
        "historical_reproduction_accuracy": (
            ratio(exact, evaluable) if spec.historical else None
        ),
        "current_repricing_relative_drift": (
            round(statistics.mean(errors), 6)
            if not spec.historical and errors
            else None
        ),
        "exact_price_accuracy_on_predicted": ratio(exact, predicted),
        "mean_relative_error": round(statistics.mean(errors), 6) if errors else None,
        "median_relative_error": round(statistics.median(errors), 6) if errors else None,
        "high_confidence_items": high_conf,
        "high_confidence_price_error": high_conf_error,
        "high_confidence_price_error_rate": ratio(high_conf_error, high_conf),
        "temporal_excluded_items": temporal_excluded,
        "future_source_count": future,
        "future_observations_excluded": future_observations_excluded,
        "undated_source_count": undated,
        "provenance_missing": provenance_missing,
        "source_status_counts": dict(sorted(source_counts.items())),
        "selection_origins": dict(sorted(selection_origins.items())),
        "pricing_errors": pricing_errors,
    }


def evaluate_mode_rows(
    rows: Iterable[Any],
    *,
    mode: str,
    ground_truth: Mapping[Any, Mapping[str, Any]] | None = None,
    source_observations: Mapping[Any, Sequence[Mapping[str, Any]]] | None = None,
    as_of: Any = None,
    project: Mapping[str, Any] | None = None,
    source_file: Mapping[str, Any] | None = None,
    filename: str | None = None,
    current_date: Any = None,
    allow_undated: bool = True,
    strict_dates: bool = False,
    auto_threshold: float = 0.90,
) -> dict[str, Any]:
    """Evaluate one temporal mode over rows and a ground-truth snapshot."""

    spec = make_mode_spec(
        mode,
        as_of=as_of,
        project=project,
        source_file=source_file,
        filename=filename,
        current_date=current_date,
        allow_undated=allow_undated,
        strict_dates=strict_dates,
    )
    row_list = list(rows)
    priceable_rows = [row for row in row_list if _is_priceable(row)]
    statuses = Counter(str(_row_get(row, "status") or "UNKNOWN") for row in priceable_rows)
    material = _evaluate_side(
        priceable_rows,
        side="material",
        spec=spec,
        ground_truth=ground_truth,
        source_observations=source_observations,
        auto_threshold=auto_threshold,
    )
    labor = _evaluate_side(
        priceable_rows,
        side="labor",
        spec=spec,
        ground_truth=ground_truth,
        source_observations=source_observations,
        auto_threshold=auto_threshold,
    )
    total = len(priceable_rows)
    applied = material["predicted_items"] + labor["predicted_items"]
    missing_provenance = material["provenance_missing"] + labor["provenance_missing"]
    return {
        "mode": spec.mode,
        "mode_spec": spec.as_dict(),
        "rows": len(row_list),
        "priceable_rows": total,
        "statuses": dict(sorted(statuses.items())),
        "material": material,
        "labor": labor,
        "provenance": {
            "missing_source": missing_provenance,
            "complete": missing_provenance == 0,
            "applied_prices": applied,
        },
        "temporal": {
            "uncertain": spec.as_of is None,
            "as_of": spec.as_of,
            "as_of_source": spec.as_of_source,
            "future_sources_excluded": material["future_source_count"]
            + labor["future_source_count"],
            "undated_sources": material["undated_source_count"]
            + labor["undated_source_count"],
            "strict_dates": spec.strict_dates,
        },
        "pricing_errors": [
            *material["pricing_errors"],
            *labor["pricing_errors"],
        ],
    }


def compare_mode_metrics(
    historical: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare the two modes using metrics with unambiguous denominators."""

    def delta(path: Sequence[str]) -> float | None:
        left: Any = historical
        right: Any = current
        for key in path:
            left = left.get(key) if isinstance(left, Mapping) else None
            right = right.get(key) if isinstance(right, Mapping) else None
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return None
        return round(float(right) - float(left), 6)

    comparison: dict[str, Any] = {
        "historical_mode": historical.get("mode"),
        "current_mode": current.get("mode"),
        "priceable_rows_delta": delta(("priceable_rows",)),
        "material": {
            "historical_reproduction_accuracy": historical.get("material", {}).get(
                "historical_reproduction_accuracy",
                historical.get("material", {}).get("exact_price_accuracy"),
            ),
            "current_repricing_coverage": current.get("material", {}).get(
                "repricing_coverage",
                current.get("material", {}).get("coverage"),
            ),
            "current_repricing_relative_drift": current.get("material", {}).get(
                "current_repricing_relative_drift",
                current.get("material", {}).get("mean_relative_error"),
            ),
            "historical_capture_rate": historical.get("material", {}).get("capture_rate"),
            "current_capture_rate": current.get("material", {}).get("capture_rate"),
            "capture_rate_delta": delta(("material", "capture_rate")),
            "coverage_delta": delta(("material", "coverage")),
            "relative_error_delta": delta(("material", "mean_relative_error")),
        },
        "labor": {
            "historical_reproduction_accuracy": historical.get("labor", {}).get(
                "historical_reproduction_accuracy",
                historical.get("labor", {}).get("exact_price_accuracy"),
            ),
            "current_repricing_coverage": current.get("labor", {}).get(
                "repricing_coverage",
                current.get("labor", {}).get("coverage"),
            ),
            "current_repricing_relative_drift": current.get("labor", {}).get(
                "current_repricing_relative_drift",
                current.get("labor", {}).get("mean_relative_error"),
            ),
            "historical_capture_rate": historical.get("labor", {}).get("capture_rate"),
            "current_capture_rate": current.get("labor", {}).get("capture_rate"),
            "capture_rate_delta": delta(("labor", "capture_rate")),
            "coverage_delta": delta(("labor", "coverage")),
            "relative_error_delta": delta(("labor", "mean_relative_error")),
        },
        "temporal": {
            "historical_as_of": historical.get("mode_spec", {}).get("as_of"),
            "current_as_of": current.get("mode_spec", {}).get("as_of"),
            "historical_future_excluded": historical.get("temporal", {}).get(
                "future_sources_excluded", 0
            ),
            "historical_temporal_uncertain": historical.get("temporal", {}).get(
                "uncertain", True
            ),
        },
    }
    return comparison


def evaluate_temporal_modes(
    rows: Iterable[Any],
    *,
    ground_truth: Mapping[Any, Mapping[str, Any]] | None = None,
    source_observations: Mapping[Any, Sequence[Mapping[str, Any]]] | None = None,
    historical_as_of: Any = None,
    current_as_of: Any = None,
    project: Mapping[str, Any] | None = None,
    source_file: Mapping[str, Any] | None = None,
    filename: str | None = None,
    current_date: Any = None,
    allow_undated: bool = True,
    strict_historical_dates: bool = False,
    auto_threshold: float = 0.90,
) -> dict[str, Any]:
    """Evaluate historical and current semantics side by side."""

    row_list = list(rows)
    historical = evaluate_mode_rows(
        row_list,
        mode=HISTORICAL_REPRODUCTION,
        ground_truth=ground_truth,
        source_observations=source_observations,
        as_of=historical_as_of,
        project=project,
        source_file=source_file,
        filename=filename,
        current_date=current_date,
        allow_undated=allow_undated,
        strict_dates=strict_historical_dates,
        auto_threshold=auto_threshold,
    )
    current = evaluate_mode_rows(
        row_list,
        mode=CURRENT_REPRICING,
        ground_truth=ground_truth,
        source_observations=source_observations,
        as_of=current_as_of,
        project=project,
        source_file=source_file,
        filename=filename,
        current_date=current_date,
        allow_undated=allow_undated,
        strict_dates=False,
        auto_threshold=auto_threshold,
    )
    return {
        "schema_version": "1.0",
        "historical_reproduction": historical,
        "current_repricing": current,
        "comparison": compare_mode_metrics(historical, current),
    }


def _db_rows(conn: Any, project_id: int, run_id: int | None = None) -> list[Any]:
    query = "SELECT * FROM boq_items WHERE project_id=?"
    params: list[Any] = [project_id]
    if run_id is not None:
        query += " AND pricing_run_id=?"
        params.append(run_id)
    return list(conn.execute(query + " ORDER BY id", tuple(params)).fetchall())


def _db_source_observations(conn: Any, rows: Sequence[Any]) -> dict[Any, list[dict[str, Any]]]:
    """Load active material/labor observations for matched IDs in *rows*.

    Immutable ``price_observations`` are authoritative for material pricing.
    ``product_prices`` remains a per-product fallback for legacy databases
    that predate the observation table (or rows with no usable observation).
    """

    product_ids = {
        int(_row_get(row, "matched_product_id"))
        for row in rows
        if _row_get(row, "matched_product_id") not in (None, "")
    }
    labor_ids = {
        int(_row_get(row, "matched_labor_item_id"))
        for row in rows
        if _row_get(row, "matched_labor_item_id") not in (None, "")
    }
    result: dict[Any, list[dict[str, Any]]] = {}
    if product_ids:
        placeholders = ",".join("?" for _ in product_ids)
        observations_by_product: dict[int, list[dict[str, Any]]] = {}
        query = f"""
            SELECT po.*, sf.filename, sf.lifecycle_status AS source_lifecycle_status,
                   ss.sheet_name, sr.row_no
            FROM price_observations po
            JOIN products p ON p.id=po.product_id
            LEFT JOIN source_files sf ON sf.id=po.source_file_id
            LEFT JOIN source_sheets ss ON ss.id=po.source_sheet_id
            LEFT JOIN source_rows sr ON sr.id=po.source_row_id
            WHERE po.product_id IN ({placeholders})
              AND COALESCE(p.lifecycle_status, 'ACTIVE')='ACTIVE'
              AND (po.source_file_id IS NULL
                   OR COALESCE(sf.lifecycle_status, 'ACTIVE')='ACTIVE')
              AND po.is_approved=1
            ORDER BY po.product_id,
                     COALESCE(po.effective_date, po.created_at) DESC,
                     po.id DESC
        """
        for row in conn.execute(query, tuple(sorted(product_ids))).fetchall():
            item = dict(row)
            amount = _safe_float(item.get("net_price"))
            if amount is None or amount < 0:
                continue
            observations_by_product.setdefault(int(row["product_id"]), []).append(item)

        # Prefer observations on the common ex-VAT/net basis per product. A
        # gross-only product remains visible, but is explicitly marked for
        # review instead of being silently mixed with net values.
        for product_id, observations in observations_by_product.items():
            ex_vat = [
                item
                for item in observations
                if normalize_tax_mode(item.get("tax_mode")) == "ex_vat"
                and normalize_price_basis(item.get("price_basis")) == "net"
            ]
            selected = ex_vat or observations
            if not ex_vat:
                for item in selected:
                    warnings = list(item.get("warnings") or [])
                    if "TAX_BASIS_FALLBACK" not in warnings:
                        warnings.append("TAX_BASIS_FALLBACK")
                    item["warnings"] = warnings
                    item["reason_code"] = "TAX_BASIS_FALLBACK"
            for item in selected:
                # Keep the observation type for source-tier selection. The
                # helper also understands context/calc JSON from legacy rows.
                item.setdefault("source_type", item.get("observation_type"))
                result.setdefault(("material", product_id), []).append(item)

        # Only products without a usable immutable observation use the
        # operational compatibility table.
        missing_product_ids = product_ids - set(observations_by_product)
        if missing_product_ids:
            placeholders = ",".join("?" for _ in missing_product_ids)
            query = f"""
                SELECT pp.*, sf.filename,
                       sf.lifecycle_status AS source_lifecycle_status,
                       ss.sheet_name, sr.row_no
                FROM product_prices pp
                JOIN products p ON p.id=pp.product_id
                LEFT JOIN source_files sf ON sf.id=pp.source_file_id
                LEFT JOIN source_sheets ss ON ss.id=pp.source_sheet_id
                LEFT JOIN source_rows sr ON sr.id=pp.source_row_id
                WHERE pp.product_id IN ({placeholders})
                  AND COALESCE(p.lifecycle_status, 'ACTIVE')='ACTIVE'
                  AND (pp.source_file_id IS NULL
                       OR COALESCE(sf.lifecycle_status, 'ACTIVE')='ACTIVE')
                  AND pp.is_approved=1
                ORDER BY pp.product_id,
                         COALESCE(pp.effective_date, pp.created_at) DESC,
                         pp.id DESC
            """
            legacy_by_product: dict[int, list[dict[str, Any]]] = {}
            for row in conn.execute(
                query, tuple(sorted(missing_product_ids))
            ).fetchall():
                item = dict(row)
                amount = _safe_float(item.get("net_price"))
                if amount is None or amount < 0:
                    continue
                item["source_type"] = (
                    "historical_boq"
                    if normalize_text(item.get("supplier"))
                    in {"historical_quotation", "historical"}
                    else "supplier_price"
                )
                legacy_by_product.setdefault(int(row["product_id"]), []).append(item)
            for product_id, legacy_rows in legacy_by_product.items():
                ex_vat = [
                    item
                    for item in legacy_rows
                    if normalize_tax_mode(item.get("tax_mode")) == "ex_vat"
                ]
                selected = ex_vat or legacy_rows
                if not ex_vat:
                    for item in selected:
                        warnings = list(item.get("warnings") or [])
                        if "TAX_BASIS_FALLBACK" not in warnings:
                            warnings.append("TAX_BASIS_FALLBACK")
                        item["warnings"] = warnings
                        item["reason_code"] = "TAX_BASIS_FALLBACK"
                for item in selected:
                    result.setdefault(("material", product_id), []).append(item)
    if labor_ids:
        placeholders = ",".join("?" for _ in labor_ids)
        query = f"""
            SELECT lr.*, sf.filename, sf.lifecycle_status AS source_lifecycle_status,
                   ss.sheet_name, sr.row_no,
                   li.lifecycle_status AS labor_lifecycle_status,
                   p.project_name, p.quotation_date AS project_quotation_date
            FROM labor_rates lr
            JOIN labor_items li ON li.id=lr.labor_item_id
            LEFT JOIN source_files sf ON sf.id=lr.source_file_id
            LEFT JOIN source_sheets ss ON ss.id=lr.source_sheet_id
            LEFT JOIN source_rows sr ON sr.id=lr.source_row_id
            LEFT JOIN projects p ON p.id=lr.source_project_id
            WHERE lr.labor_item_id IN ({placeholders})
              AND COALESCE(li.lifecycle_status, 'ACTIVE')='ACTIVE'
              AND (lr.source_file_id IS NULL
                   OR COALESCE(sf.lifecycle_status, 'ACTIVE')='ACTIVE')
            ORDER BY lr.labor_item_id, lr.effective_date DESC, lr.id DESC
        """
        for row in conn.execute(query, tuple(sorted(labor_ids))).fetchall():
            item = dict(row)
            result.setdefault(("labor", int(row["labor_item_id"])), []).append(item)
    return result


def evaluate_db_modes(
    conn: Any,
    project_id: int,
    *,
    run_id: int | None = None,
    ground_truth: Mapping[Any, Mapping[str, Any]] | None = None,
    historical_as_of: Any = None,
    current_as_of: Any = None,
    current_date: Any = None,
    strict_historical_dates: bool = False,
    allow_undated: bool = True,
    auto_threshold: float = 0.90,
) -> dict[str, Any]:
    """Evaluate both modes directly from an existing SQLite benchmark DB."""

    rows = _db_rows(conn, project_id, run_id=run_id)
    project_row = conn.execute(
        "SELECT * FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    source_file = None
    if project_row is not None and project_row["source_file_id"] is not None:
        source_row = conn.execute(
            "SELECT * FROM source_files WHERE id=?", (project_row["source_file_id"],)
        ).fetchone()
        source_file = dict(source_row) if source_row else None
    project = dict(project_row) if project_row else None
    observations = _db_source_observations(conn, rows)
    return evaluate_temporal_modes(
        rows,
        ground_truth=ground_truth,
        source_observations=observations,
        historical_as_of=historical_as_of,
        current_as_of=current_as_of,
        project=project,
        source_file=source_file,
        current_date=current_date,
        strict_historical_dates=strict_historical_dates,
        allow_undated=allow_undated,
        auto_threshold=auto_threshold,
    )


def render_temporal_markdown(report: Mapping[str, Any]) -> str:
    """Render a compact side-by-side report suitable for Round 2 artifacts."""

    historical = report.get("historical_reproduction") or {}
    current = report.get("current_repricing") or {}
    comparison = report.get("comparison") or {}

    def pct(value: Any) -> str:
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return "—"

    lines = [
        "# Temporal benchmark modes",
        "",
        "Historical reproduction and current repricing are evaluated separately.",
        "",
        "| Metric | Historical reproduction | Current repricing |",
        "| --- | ---: | ---: |",
        f"| As-of date | {historical.get('mode_spec', {}).get('as_of') or 'unknown'} | {current.get('mode_spec', {}).get('as_of') or 'unknown'} |",
        f"| Priceable rows | {historical.get('priceable_rows', 0)} | {current.get('priceable_rows', 0)} |",
        f"| Material source coverage | {pct((historical.get('material') or {}).get('source_coverage'))} | {pct((current.get('material') or {}).get('source_coverage'))} |",
        f"| Material capture rate | {pct((historical.get('material') or {}).get('capture_rate'))} | {pct((current.get('material') or {}).get('capture_rate'))} |",
        f"| Material repricing coverage | {pct((historical.get('material') or {}).get('repricing_coverage'))} | {pct((current.get('material') or {}).get('repricing_coverage'))} |",
        f"| Historical material reproduction accuracy | {pct((historical.get('material') or {}).get('historical_reproduction_accuracy'))} | n/a |",
        f"| Current material relative drift | n/a | {pct((current.get('material') or {}).get('current_repricing_relative_drift'))} |",
        f"| Labor source coverage | {pct((historical.get('labor') or {}).get('source_coverage'))} | {pct((current.get('labor') or {}).get('source_coverage'))} |",
        f"| Labor capture rate | {pct((historical.get('labor') or {}).get('capture_rate'))} | {pct((current.get('labor') or {}).get('capture_rate'))} |",
        f"| Labor repricing coverage | {pct((historical.get('labor') or {}).get('repricing_coverage'))} | {pct((current.get('labor') or {}).get('repricing_coverage'))} |",
        f"| Historical labor reproduction accuracy | {pct((historical.get('labor') or {}).get('historical_reproduction_accuracy'))} | n/a |",
        f"| Current labor relative drift | n/a | {pct((current.get('labor') or {}).get('current_repricing_relative_drift'))} |",
        f"| Future sources excluded | {(historical.get('temporal') or {}).get('future_sources_excluded', 0)} | {(current.get('temporal') or {}).get('future_sources_excluded', 0)} |",
        "",
        "## Interpretation",
        "",
        "- Historical reproduction must use observations eligible at the historical as-of date.",
        "- Current repricing measures usable current coverage and relative drift against the old BOQ price; it is not a claim that the old project price was a supplier net price.",
        "- Unknown quotation dates and undated observations remain explicit uncertainty rather than being silently treated as historical truth.",
        "",
    ]
    return "\n".join(lines)


def write_temporal_report(
    output_dir: Path,
    report: Mapping[str, Any],
    *,
    stem: str = "temporal-modes-report",
) -> dict[str, str]:
    """Persist JSON + Markdown artifacts for a temporal benchmark run."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_temporal_markdown(report),
        encoding="utf-8",
    )
    return {
        "json": str(json_path.resolve()),
        "markdown": str(markdown_path.resolve()),
    }


__all__ = [
    "BENCHMARK_MODES",
    "BenchmarkModeSpec",
    "CURRENT_REPRICING",
    "HISTORICAL_REPRODUCTION",
    "classify_price_error",
    "compare_mode_metrics",
    "date_text",
    "eligible_observations",
    "evaluate_db_modes",
    "evaluate_mode_rows",
    "evaluate_temporal_modes",
    "infer_benchmark_date",
    "make_mode_spec",
    "normalize_mode",
    "observation_date",
    "observation_source_type",
    "parse_date",
    "render_temporal_markdown",
    "write_temporal_report",
    "temporal_observation_status",
]
