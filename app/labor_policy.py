"""Deterministic labor-rate policy and statistics helpers.

The labor catalog intentionally stores observations rather than one mutable
rate.  A labor item can therefore have a master rate, several historical
quotation observations, or both.  This module keeps the selection policy
small, explicit and testable; it never calls an LLM and never invents a
numeric rate.

The public entry point is :func:`select_labor_rate`.  It accepts SQLite rows
from ``labor_rates`` (optionally enriched with project/source columns) and
returns a selected rate together with a JSON-safe explanation/provenance
payload.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence


MASTER_SOURCE_TYPES = frozenset(
    {
        "labor_master",
        "approved_internal",
        "internal_master",
        "internal_approved",
        "manual_approved",
    }
)

HISTORICAL_SOURCE_TYPES = frozenset(
    {
        "historical_boq",
        "historical_quotation",
        "historical",
        "project_quotation",
    }
)

POLICY_ALIASES = {
    "latest": "latest",
    "latest_rate": "latest",
    "latest_historical_rate": "latest",
    "latest_historical": "latest",
    "median": "median_last_3",
    "median_last_3": "median_last_3",
    "median_3": "median_last_3",
    "median_last_5": "median_last_5",
    "median_5": "median_last_5",
    "median_last_3_with_adjustment": "median_last_3_with_adjustment",
    "median_last_3_adjusted": "median_last_3_with_adjustment",
    "median_3_with_adjustment": "median_last_3_with_adjustment",
}


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _safe_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_json(value: Any, default: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return default
    try:
        result = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return result if isinstance(result, type(default)) else default


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # SQLite and Excel ingestion use ISO dates, while accepting a timestamp
    # here makes the helper safe for API callers and synthetic tests.
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


def _date_text(value: Any) -> str | None:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else (str(value)[:32] if value else None)


def _normalise_adjustment(value: Any) -> float:
    """Convert a user-facing adjustment to a fractional multiplier delta.

    ``0.10`` and ``10`` are both accepted as ten percent.  Negative values
    are allowed for a deliberate reduction, but the resulting escalation
    factor is clamped to a non-negative value by the selector.
    """

    parsed = _safe_float(value)
    if parsed is None:
        return 0.0
    if abs(parsed) > 1.0:
        parsed /= 100.0
    return parsed


def normalise_labor_policy(
    policy: str | Mapping[str, Any] | None = None,
    *,
    adjustment: Any = None,
    escalation_factor: Any = None,
    max_spread_ratio: Any = None,
    max_cv: Any = None,
    min_observations: Any = None,
) -> dict[str, Any]:
    """Return a bounded, canonical labor policy configuration.

    The function accepts either a short policy name or a mapping.  Mapping
    aliases intentionally cover the spellings used by the CLI, API and older
    pricing runs.  Unknown names fall back to ``latest`` rather than silently
    changing the rate.
    """

    source: dict[str, Any] = dict(policy) if isinstance(policy, Mapping) else {}
    raw_name = (
        policy
        if isinstance(policy, str)
        else source.get("strategy", source.get("policy", source.get("name")))
    )
    name = str(raw_name or "latest").strip().lower().replace(" ", "_")
    strategy = POLICY_ALIASES.get(name, "latest")

    raw_adjustment = (
        adjustment
        if adjustment is not None
        else source.get("adjustment", source.get("adjustment_pct", 0.0))
    )
    adjustment_rate = _normalise_adjustment(raw_adjustment)
    raw_factor = (
        escalation_factor
        if escalation_factor is not None
        else source.get("escalation_factor")
    )
    factor = _safe_float(raw_factor)
    if factor is None:
        factor = max(0.0, 1.0 + adjustment_rate)
    elif factor <= 0:
        factor = 0.0
    # If a caller supplied a percentage as ``escalation_factor=10``, interpret
    # it consistently with adjustment fields. Explicit factors such as 1.10
    # remain unchanged.
    elif factor > 3.0:
        factor = 1.0 + _normalise_adjustment(factor)
    adjustment_rate = factor - 1.0

    def bounded_float(value: Any, default: float, *, upper: float | None = None) -> float:
        parsed = _safe_float(value)
        if parsed is None:
            parsed = default
        parsed = max(0.0, parsed)
        if upper is not None:
            parsed = min(upper, parsed)
        return parsed

    spread_limit = bounded_float(
        max_spread_ratio
        if max_spread_ratio is not None
        else source.get("max_spread_ratio", source.get("spread_threshold", 0.25)),
        0.25,
        upper=100.0,
    )
    cv_limit = bounded_float(
        max_cv if max_cv is not None else source.get("max_cv", source.get("variance_threshold", 0.20)),
        0.20,
        upper=100.0,
    )
    min_count = _safe_int(
        min_observations
        if min_observations is not None
        else source.get("min_observations", 2)
    )
    min_count = max(1, min_count if min_count is not None else 2)

    window = 1
    if strategy == "median_last_3" or strategy == "median_last_3_with_adjustment":
        window = 3
    elif strategy == "median_last_5":
        window = 5
    requested_window = _safe_int(source.get("window"))
    if requested_window is not None:
        window = max(1, min(50, requested_window))

    prefer_master = source.get("prefer_master", True)
    if isinstance(prefer_master, str):
        prefer_master = prefer_master.strip().lower() in {"1", "true", "yes", "on"}
    else:
        prefer_master = bool(prefer_master)

    return {
        "strategy": strategy,
        "adjustment": round(adjustment_rate, 8),
        "escalation_factor": round(max(0.0, factor), 8),
        "window": window,
        "max_spread_ratio": spread_limit,
        "max_cv": cv_limit,
        "min_observations": min_count,
        "prefer_master": prefer_master,
    }


def _observation(row: Any) -> dict[str, Any] | None:
    """Convert a sqlite row/dict into a stable, JSON-safe observation."""

    if isinstance(row, Mapping):
        get = row.get
    else:
        # sqlite3.Row supports key access but not ``get``.
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        get = lambda key, default=None: row[key] if key in keys else default
    rate = _safe_float(get("rate"))
    if rate is None or rate < 0:
        return None
    policy = _parse_json(get("policy_json"), {})
    source_type = str(
        policy.get("source_type")
        or get("source_type")
        or ("labor_master" if not get("source_project_id") else "historical_boq")
    ).strip().lower()
    effective = get("effective_date") or get("project_quotation_date") or get("quotation_date")
    created = get("created_at")
    observed_date = _date_text(effective) or _date_text(created)
    confidence = _safe_float(get("confidence"))
    if confidence is None:
        confidence = 1.0
    observation: dict[str, Any] = {
        "id": _safe_int(get("id")),
        "rate": rate,
        "effective_date": _date_text(get("effective_date")),
        "observed_date": observed_date,
        "source_type": source_type,
        "source_project_id": _safe_int(get("source_project_id")),
        "source_file_id": _safe_int(get("source_file_id")),
        "source_sheet_id": _safe_int(get("source_sheet_id")),
        "source_row_id": _safe_int(get("source_row_id")),
        "filename": get("filename"),
        "sheet_name": get("sheet_name"),
        "row_no": _safe_int(get("row_no")),
        "project_name": get("project_name"),
        "project_quotation_date": _date_text(get("project_quotation_date")),
        "confidence": max(0.0, min(1.0, confidence)),
        "policy": policy,
        "created_at": get("created_at"),
    }
    return observation


def normalise_observations(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Drop invalid rates and sort observations newest-first deterministically."""

    observations = [item for row in rows if (item := _observation(row)) is not None]

    def sort_key(item: dict[str, Any]) -> tuple[int, str, int]:
        # Date-bearing observations precede undated rows; ``id`` breaks ties
        # in a stable way when several project rows share one effective date.
        observed = item.get("observed_date") or ""
        return (1 if observed else 0, observed, int(item.get("id") or 0))

    observations.sort(key=sort_key, reverse=True)
    return observations


def _public_provenance(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return source coordinates without exposing internal implementation noise."""

    keys = (
        "id",
        "rate",
        "effective_date",
        "observed_date",
        "source_type",
        "source_project_id",
        "source_file_id",
        "source_sheet_id",
        "source_row_id",
        "filename",
        "sheet_name",
        "row_no",
        "project_name",
        "project_quotation_date",
        "confidence",
    )
    return {key: item[key] for key in keys if item.get(key) not in (None, "")}


def labor_rate_statistics(
    rows: Iterable[Any],
    *,
    as_of: Any = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Compute descriptive statistics for labor observations.

    ``limit`` is applied after newest-first sorting, making
    ``labor_rate_statistics(rows, limit=3)`` exactly the statistics used by
    ``median_last_3``.  The full count is retained as ``available_count``.
    """

    observations = normalise_observations(rows)
    available_count = len(observations)
    if limit is not None:
        try:
            n = max(1, int(limit))
        except (TypeError, ValueError):
            n = available_count
        observations = observations[:n]
    rates = [float(item["rate"]) for item in observations]
    count = len(rates)
    if not rates:
        return {
            "count": 0,
            "available_count": available_count,
            "latest": None,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "spread": None,
            "spread_ratio": None,
            "variance": None,
            "stddev": None,
            "coefficient_variation": None,
            "latest_date": None,
            "oldest_date": None,
            "recency_days": None,
            "observation_ids": [],
            "source_types": [],
            "observations": [],
        }
    latest_date = _parse_date(observations[0].get("observed_date"))
    oldest_date = _parse_date(observations[-1].get("observed_date"))
    reference = _parse_date(as_of) or datetime.now(timezone.utc).date()
    recency_days = (reference - latest_date).days if latest_date else None
    minimum = min(rates)
    maximum = max(rates)
    mean = statistics.fmean(rates)
    median = statistics.median(rates)
    spread = maximum - minimum
    spread_ratio = spread / abs(median) if median else None
    variance = statistics.pvariance(rates) if count > 1 else 0.0
    stddev = math.sqrt(variance)
    coefficient = stddev / abs(mean) if mean else None
    return {
        "count": count,
        "available_count": available_count,
        "latest": rates[0],
        "min": minimum,
        "max": maximum,
        "mean": mean,
        "median": median,
        "spread": spread,
        "spread_ratio": spread_ratio,
        "variance": variance,
        "stddev": stddev,
        "coefficient_variation": coefficient,
        "latest_date": _date_text(observations[0].get("observed_date")),
        "oldest_date": _date_text(observations[-1].get("observed_date")),
        "recency_days": recency_days,
        "observation_ids": [
            item["id"] for item in observations if item.get("id") is not None
        ],
        "source_types": sorted({item["source_type"] for item in observations}),
        "observations": [_public_provenance(item) for item in observations],
    }


def _explanation(
    *,
    strategy: str,
    base_rate: float,
    selected_rate: float,
    observations: Sequence[Mapping[str, Any]],
    adjustment: float,
    master: bool,
    stats: Mapping[str, Any],
) -> str:
    if master:
        source = observations[0]
        origin = source.get("filename") or source.get("source_type") or "labor master"
        text = (
            f"Áp dụng đơn giá nhân công master chính xác {base_rate:,.0f} "
            f"từ {origin}."
        )
    elif strategy == "latest":
        source = observations[0]
        origin = source.get("filename") or source.get("project_name") or "báo giá lịch sử"
        text = (
            f"Không có labor master; dùng quan sát nhân công gần nhất "
            f"{base_rate:,.0f} từ {origin}."
        )
    else:
        ids = ", ".join(str(item.get("id")) for item in observations if item.get("id"))
        text = (
            f"Dùng {strategy} trên {len(observations)} quan sát gần nhất "
            f"(rate cơ sở {base_rate:,.0f}; observation IDs: {ids or 'n/a'})."
        )
    if adjustment:
        text += f" Điều chỉnh {adjustment:+.1%} → {selected_rate:,.0f}."
    if stats.get("spread_ratio") is not None:
        text += f" Spread/median={float(stats['spread_ratio']):.1%}."
    return text


def select_labor_rate(
    rows: Iterable[Any],
    policy: str | Mapping[str, Any] | None = None,
    *,
    adjustment: Any = None,
    escalation_factor: Any = None,
    max_spread_ratio: Any = None,
    max_cv: Any = None,
    min_observations: Any = None,
    as_of: Any = None,
) -> tuple[float | None, dict[str, Any]]:
    """Select a labor rate and return deterministic metadata/provenance.

    A valid master observation wins over historical aggregation by default.
    This implements the business rule that a maintained exact labor master is
    safer than deriving a rate from old project quotations.  Set
    ``prefer_master=False`` in a mapping when an explicit historical
    reproduction run needs to bypass that rule.
    """

    config = normalise_labor_policy(
        policy,
        adjustment=adjustment,
        escalation_factor=escalation_factor,
        max_spread_ratio=max_spread_ratio,
        max_cv=max_cv,
        min_observations=min_observations,
    )
    observations = normalise_observations(rows)
    if not observations:
        return None, {
            "type": "labor_rate",
            "policy": config["strategy"],
            "needs_review": True,
            "reason_code": "LABOR_NO_RATE_OBSERVATION",
            "explanation": "Không có quan sát đơn giá nhân công hợp lệ.",
            "statistics": labor_rate_statistics([], as_of=as_of),
        }

    masters = [
        item
        for item in observations
        if item.get("source_type") in MASTER_SOURCE_TYPES
    ]
    use_master = bool(masters and config["prefer_master"])
    selected_observations = masters[:1] if use_master else observations[: config["window"]]
    stats = labor_rate_statistics(selected_observations, as_of=as_of)
    rates = [float(item["rate"]) for item in selected_observations]
    if use_master or config["strategy"] == "latest":
        base_rate = rates[0]
    else:
        base_rate = float(statistics.median(rates))

    selected_rate = base_rate * float(config["escalation_factor"])
    # Keep the selected value stable and suitable for VND exports. The caller
    # may apply its own money-rounding policy after this function.
    selected_rate = float(round(selected_rate))

    spread_high = (
        len(rates) >= config["min_observations"]
        and stats.get("spread_ratio") is not None
        and float(stats["spread_ratio"]) > config["max_spread_ratio"]
    )
    variance_high = (
        len(rates) >= config["min_observations"]
        and stats.get("coefficient_variation") is not None
        and float(stats["coefficient_variation"]) > config["max_cv"]
    )
    warnings: list[str] = []
    if spread_high:
        warnings.append("LABOR_RATE_SPREAD_HIGH")
    if variance_high:
        warnings.append("LABOR_RATE_VARIANCE_HIGH")

    source_types = sorted({str(item["source_type"]) for item in selected_observations})
    if use_master:
        source_tier = "labor_master"
    elif len(selected_observations) > 1:
        source_tier = "historical_aggregate"
    elif source_types and source_types[0] in HISTORICAL_SOURCE_TYPES:
        source_tier = "historical_exact"
    else:
        source_tier = source_types[0] if source_types else "unknown"

    source_payload: dict[str, Any] = {
        "type": "labor_rate",
        "policy": config["strategy"],
        "strategy": config["strategy"],
        "source_tier": source_tier,
        "source_type": source_types[0] if len(source_types) == 1 else source_types,
        "rate": selected_rate,
        "base_rate": base_rate,
        "adjustment": config["adjustment"],
        "escalation_factor": config["escalation_factor"],
        "window": config["window"],
        "observation_count": len(selected_observations),
        "available_observation_count": len(observations),
        "statistics": stats,
        "provenance": [_public_provenance(item) for item in selected_observations],
        "effective_date": selected_observations[0].get("effective_date"),
        "source_project_ids": sorted(
            {
                int(item["source_project_id"])
                for item in selected_observations
                if item.get("source_project_id") is not None
            }
        ),
        "needs_review": bool(warnings),
        "warnings": warnings,
        "reason_code": warnings[0] if warnings else None,
        "confidence": max(
            0.0,
            min(
                1.0,
                min(float(item.get("confidence", 1.0)) for item in selected_observations)
                - (0.15 if warnings else 0.0),
            ),
        ),
    }
    source_payload["explanation"] = _explanation(
        strategy=config["strategy"],
        base_rate=base_rate,
        selected_rate=selected_rate,
        observations=selected_observations,
        adjustment=config["adjustment"],
        master=use_master,
        stats=stats,
    )
    return selected_rate, source_payload


__all__ = [
    "HISTORICAL_SOURCE_TYPES",
    "MASTER_SOURCE_TYPES",
    "labor_rate_statistics",
    "normalise_labor_policy",
    "normalise_observations",
    "select_labor_rate",
]
