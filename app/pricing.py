from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Iterable

from .ai import AIProvider, RerankResult, ai_provider, safe_rerank_sync
from .db import db_session, dumps, loads, utc_now, row_dict
from .normalize import canonical_key, normalize_text, normalize_unit, technical_attributes


AUTO_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.62


@dataclass
class Candidate:
    entity_id: int
    name: str
    code: str | None
    unit: str | None
    brand: str | None
    origin: str | None
    attrs: dict[str, Any]
    score: float
    components: dict[str, float]
    explanation: str


# A pricing run keeps one SQLite connection open. Cache immutable catalog
# retrieval features and selected price provenance for that connection so a
# 2,000+ line BOQ does not repeatedly tokenize/parse the same catalog rows.
_CATALOG_CACHE: dict[tuple[str, str, int], list[tuple[Any, str, set[str], set[str]]]] = {}
_PRICE_CACHE: dict[tuple[str, str, int, str | None], tuple[float | None, dict[str, Any]]] = {}


def clear_runtime_caches() -> None:
    """Clear process-local caches (useful after a database reset in tests)."""

    _CATALOG_CACHE.clear()
    _PRICE_CACHE.clear()


def _db_identity(conn) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is not None and row[2]:
            return str(row[2])
    except Exception:
        pass
    return f"connection:{id(conn)}"


def _tokens(value: str) -> set[str]:
    # Technical tokens (numbers, cable families, dimensions) are intentionally
    # retained; stopwords are only common Vietnamese connective words.
    stop = {"va", "tu", "cho", "cua", "voi", "theo", "cac", "loai", "bo", "he", "thong"}
    return {t for t in re.findall(r"[a-z0-9]+", normalize_text(value)) if t not in stop}


def _technical_tokens(value: str) -> set[str]:
    text = normalize_text(value)
    tokens: set[str] = set()
    known = (
        "cxv", "cvv", "cv", "dsta", "data", "swa", "cts", "cws", "xlpe",
        "pvc", "hdpe", "upvc", "frn", "fsn", "dvv", "axv", "adsta",
        "adata", "aswa", "acsr", "abc", "24kv", "22kv", "12kv", "0.6/1kv",
    )
    for token in known:
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text):
            tokens.add(token)
    # Capture engineering sizes and voltage/current markers. Do not retain
    # arbitrary route numbers (DB-5.1, MCC-21) as technical evidence.
    tokens.update(re.findall(r"\b\d+(?:[.,]\d+)?(?:kv|kva|ka|a|mm2|mm|dn)\b", text))
    tokens.update(
        re.findall(
            r"\b\d+\s*x\s*(?:\(?\d+\s*[a-z]?\s*[-_/]?\s*)?"
            r"\d+(?:[.,]\d+)?\)?\b",
            text,
        )
    )
    # Add canonical structured markers so a verbose BOQ notation can retrieve
    # a concise catalog row (e.g. ``2x1C-2.5`` → a single-core 2.5 mm² item).
    attrs = technical_attributes(value)
    family = attrs.get("cable_family")
    if family:
        tokens.add(f"family:{normalize_text(family)}")
    voltage = attrs.get("voltage")
    if voltage:
        tokens.add(f"voltage:{normalize_text(voltage)}")
    voltage_class = attrs.get("voltage_class")
    if voltage_class:
        tokens.add(f"class:{normalize_text(voltage_class)}")
    base_cores = attrs.get("base_cores", attrs.get("cores"))
    if base_cores is not None:
        tokens.add(f"cores:{base_cores}")
    cross_section = attrs.get("cross_section_mm2")
    if cross_section is not None:
        tokens.add(f"section:{cross_section:g}")
    diameter = attrs.get("diameters_mm", attrs.get("diameter_mm"))
    if diameter is not None:
        if isinstance(diameter, list):
            tokens.add("diameters:" + "/".join(f"{float(v):g}" for v in diameter))
        else:
            tokens.add(f"diameter:{float(diameter):g}")
    return tokens


def _family_tokens(value: str) -> set[str]:
    text = normalize_text(value)
    families = (
        "adata", "adsta", "aswa", "cxv", "cvv", "cv", "dsta", "data",
        "swa", "cts", "cws", "xlpe", "pvc", "hdpe", "upvc", "frn",
        "fsn", "dvv", "axv", "acsr", "abc", "vctf", "vctfk", "vcmd",
    )
    return {
        family
        for family in families
        if re.search(rf"(?<![a-z0-9]){re.escape(family)}(?![a-z0-9])", text)
    }


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _family_compatible(left: Any, right: Any) -> bool:
    if left in (None, "") or right in (None, ""):
        return False
    a = normalize_text(left).replace("/", "-")
    b = normalize_text(right).replace("/", "-")
    if a == b:
        return True
    # A plain family can match a construction-qualified variant (e.g.
    # CXV ↔ CXV-CTS-W) only when neither side is a fire-retardant variant.
    fire_variants = ("fsn-", "frn-")
    if a.startswith(fire_variants) or b.startswith(fire_variants):
        return False
    return a.split("-")[0] == b.split("-")[0]


def _normalized_category(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return normalize_text(value).replace(" ", "_")


def _critical_missing(query: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Return decisive catalog attributes absent from the BOQ description."""

    critical = (
        "voltage",
        "voltage_class",
        "cable_family",
        "armour",
        "cores",
        "cross_section_mm2",
        "diameter_mm",
        "diameters_mm",
    )
    return [key for key in critical if key in candidate and key not in query]


def _shape_tokens(value: str) -> tuple[str, ...]:
    """Keep compound conductor shapes distinct during ambiguity checks."""

    text = normalize_text(value)
    return tuple(
        re.findall(
            r"\d+\s*x\s*(?:\(\s*)?\d+\s*[a-z]?\s*[-_/]?\s*\d+(?:[.,]\d+)?",
            text,
        )
    )


def _safe_float(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _attribute_score(query: dict[str, Any], candidate: dict[str, Any]) -> tuple[float, bool, list[str]]:
    if not query or not candidate:
        return 0.0, False, []
    keys = {
        "voltage",
        "voltage_class",
        "cable_family",
        "insulation",
        "armour",
        "conductor",
        "cores",
        "cross_section_mm2",
        "diameter_mm",
        "diameters_mm",
        "material",
    }
    compared = 0
    matched = 0
    conflicts: list[str] = []
    for key in keys:
        if key not in query:
            continue
        if key not in candidate:
            # If the BOQ explicitly states a decisive specification but the
            # catalog row does not, confidence must not be high enough for an
            # automatic application. This is especially important for LV/MV
            # voltage and cable armour.
            if key in {
                "voltage",
                "voltage_class",
                "cable_family",
                "armour",
                "cores",
                "cross_section_mm2",
                "diameter_mm",
                "diameters_mm",
            }:
                conflicts.append(key)
            continue
        compared += 1
        left, right = query[key], candidate[key]
        if key == "cores":
            # `12x1C-240` is a bundle of twelve single-core cables. Compare
            # the catalog core count with the base core count while retaining
            # the outer count for price scaling.
            left = query.get("base_cores", left)
            right = candidate.get("base_cores", right)
        if key == "cable_family":
            ok = _family_compatible(left, right)
        elif key == "diameters_mm":
            left_values = [float(v) for v in left] if isinstance(left, list) else [float(left)]
            right_values = [float(v) for v in right] if isinstance(right, list) else [float(right)]
            ok = len(left_values) == len(right_values) and all(
                abs(a - b) < 1e-6 for a, b in zip(left_values, right_values)
            )
        elif isinstance(left, (int, float)) and isinstance(right, (int, float)):
            ok = abs(float(left) - float(right)) < 1e-6
        else:
            ok = normalize_text(left) == normalize_text(right)
        if ok:
            matched += 1
        else:
            conflicts.append(key)
    score = matched / compared if compared else 0.0
    # A conflict on a safety-critical dimension should prevent auto approval.
    hard_conflict = any(
        key in conflicts
        for key in (
            "voltage",
            "voltage_class",
            "cable_family",
            "armour",
            "cores",
            "cross_section_mm2",
            "diameter_mm",
            "diameters_mm",
        )
    )
    return score, hard_conflict, conflicts


def _candidate_from_product(row: Any) -> Candidate:
    return Candidate(
        entity_id=int(row["id"]),
        name=row["normalized_name"] or "",
        code=row["product_code"],
        unit=row["unit"],
        brand=row["brand"],
        origin=row["origin"],
        attrs=loads(row["technical_attributes_json"], {}),
        score=0.0,
        components={},
        explanation="",
    )


def _candidate_from_labor(row: Any) -> Candidate:
    return Candidate(
        entity_id=int(row["id"]),
        name=row["normalized_name"] or "",
        code=row["code"],
        unit=row["unit"],
        brand=None,
        origin=None,
        attrs=loads(row["technical_attributes_json"], {}),
        score=0.0,
        components={},
        explanation="",
    )


def _score_candidate(
    description: str,
    code: str | None,
    unit: str | None,
    attrs: dict[str, Any],
    candidate: Candidate,
    *,
    candidate_norm: str | None = None,
    candidate_tokens: set[str] | None = None,
    candidate_family: set[str] | None = None,
) -> Candidate:
    query_norm = canonical_key(description)
    cand_norm = candidate_norm if candidate_norm is not None else canonical_key(candidate.name)
    query_tokens = _tokens(description)
    cand_tokens = candidate_tokens if candidate_tokens is not None else _tokens(candidate.name)
    components: dict[str, float] = {}
    if code and candidate.code and canonical_key(code) == canonical_key(candidate.code):
        components["exact_code"] = 1.0
    if query_norm and query_norm == cand_norm:
        components["exact_name"] = 1.0
    components["token_overlap"] = _jaccard(query_tokens, cand_tokens)
    components["sequence"] = SequenceMatcher(None, query_norm, cand_norm).ratio() if query_norm and cand_norm else 0.0
    components["family"] = _jaccard(
        _family_tokens(description),
        candidate_family if candidate_family is not None else _family_tokens(candidate.name),
    )
    if unit and candidate.unit and normalize_unit(unit) == normalize_unit(candidate.unit):
        components["unit"] = 1.0
    attr_score, hard_conflict, conflicts = _attribute_score(attrs, candidate.attrs)
    components["technical_attributes"] = attr_score
    # A long BOQ description commonly adds routing/location prose around a
    # concise cable or pipe specification. When the engineering signature is
    # exact, do not let that prose drag the match into a low-confidence review.
    query_category = _normalized_category(attrs.get("category"))
    candidate_category = _normalized_category(candidate.attrs.get("category"))
    category_conflict = bool(
        query_category and candidate_category and query_category != candidate_category
    )
    if query_category and candidate_category:
        components["category"] = 1.0 if not category_conflict else 0.0
    unit_match = bool(
        unit
        and candidate.unit
        and normalize_unit(unit) == normalize_unit(candidate.unit)
    )
    unit_conflict = bool(
        unit
        and candidate.unit
        and normalize_unit(unit) != normalize_unit(candidate.unit)
    )
    if unit_match:
        components["unit"] = 1.0
    if unit_conflict:
        components["unit_conflict"] = 1.0
    missing_critical = _critical_missing(attrs, candidate.attrs)
    if missing_critical:
        components["underspecified"] = 1.0
    structured = False
    if query_category == "cable" and candidate_category == "cable":
        required = ("cable_family", "cross_section_mm2", "cores")
        structured = all(key in attrs and key in candidate.attrs for key in required) and attr_score >= 0.8
    elif query_category == "pipe" and candidate_category == "pipe":
        structured = (
            ("diameters_mm" in attrs and "diameters_mm" in candidate.attrs and attr_score >= 0.8)
            or ("diameter_mm" in attrs and "diameter_mm" in candidate.attrs and attr_score >= 0.8)
        )
    if structured and not hard_conflict:
        components["structured_signature"] = 1.0

    if components.get("exact_code"):
        score = 0.995
    elif components.get("exact_name"):
        score = 0.975
    elif components.get("structured_signature"):
        score = 0.965
    else:
        score = (
            0.28 * components["token_overlap"]
            + 0.18 * components["sequence"]
            + 0.25 * components["technical_attributes"]
            + 0.17 * components["family"]
            + 0.12 * components.get("unit", 0.0)
        )
        # Exact technical agreement with a strong textual overlap is valuable.
        if components["technical_attributes"] >= 0.75 and components["family"] >= 0.5:
            score = min(0.97, score + 0.16)
    if hard_conflict or category_conflict or unit_conflict:
        score = min(score, 0.48)
    elif missing_critical and not components.get("exact_code"):
        # A catalog row with extra decisive specifications (e.g. MV voltage
        # or armour) is a valid review candidate, but the BOQ did not provide
        # enough information to auto-apply it safely.
        score = min(score, AUTO_THRESHOLD - 0.001)
    candidate.score = round(max(0.0, min(0.999, score)), 6)
    candidate.components = components
    if category_conflict:
        candidate.explanation = (
            f"Mâu thuẫn nhóm hàng: BOQ={query_category}, candidate={candidate_category}."
        )
    elif unit_conflict:
        candidate.explanation = (
            f"Mâu thuẫn đơn vị: BOQ={normalize_unit(unit)}, "
            f"candidate={normalize_unit(candidate.unit)}."
        )
    elif hard_conflict:
        candidate.explanation = f"Mâu thuẫn thuộc tính kỹ thuật: {', '.join(conflicts)}"
    elif missing_critical:
        candidate.explanation = (
            "Candidate có thuộc tính quyết định nhưng BOQ chưa nêu rõ: "
            + ", ".join(missing_critical)
        )
    elif components.get("exact_code"):
        candidate.explanation = "Khớp chính xác mã sản phẩm."
    elif components.get("exact_name"):
        candidate.explanation = "Khớp chính xác mô tả đã chuẩn hóa."
    elif components["technical_attributes"] >= 0.75:
        candidate.explanation = "Khớp từ khóa và thuộc tính kỹ thuật."
    else:
        candidate.explanation = "Khớp gần đúng theo từ khóa/mô tả."
    return candidate


def _alias_matches(conn, description: str) -> set[str]:
    aliases = conn.execute("SELECT canonical FROM aliases WHERE alias = ?", (canonical_key(description),)).fetchall()
    return {str(row["canonical"]) for row in aliases}


def _product_catalog(conn) -> list[Any]:
    return conn.execute("SELECT * FROM products").fetchall()


def _labor_catalog(conn) -> list[Any]:
    return conn.execute("SELECT * FROM labor_items").fetchall()


def _prepared_catalog(conn, kind: str) -> list[tuple[Any, str, set[str], set[str]]]:
    table = "products" if kind == "product" else "labor_items"
    row_count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    key = (_db_identity(conn), kind, row_count)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached
    rows = _product_catalog(conn) if kind == "product" else _labor_catalog(conn)
    prepared: list[tuple[Any, str, set[str], set[str]]] = []
    for row in rows:
        name = row["normalized_name"] or ""
        prepared.append(
            (
                row,
                canonical_key(name),
                _tokens(name),
                _technical_tokens(name),
            )
        )
    _CATALOG_CACHE[key] = prepared
    return prepared


def _prefilter_rows(
    rows: list[tuple[Any, str, set[str], set[str]]],
    description: str,
    code: str | None,
    limit: int = 260,
) -> list[tuple[Any, str, set[str], set[str]]]:
    """Cheap retrieval stage before expensive fuzzy scoring.

    It is intentionally deterministic: exact code/name first, then shared
    technical tokens and general token overlap. This reduces a 2,500×2,500
    benchmark from millions of SequenceMatcher calls without changing the
    candidate-scoring policy.
    """

    query_norm = canonical_key(description)
    query_tokens = _tokens(description)
    query_technical = _technical_tokens(description)
    code_norm = canonical_key(code or "")
    ranked: list[tuple[float, tuple[Any, str, set[str], set[str]]]] = []
    for prepared in rows:
        row, row_norm, row_tokens, row_technical = prepared
        row_code = canonical_key(
            row["product_code"] if "product_code" in row.keys() else row["code"] or ""
        )
        if code_norm and row_code == code_norm:
            retrieval = 10.0
        elif query_norm and row_norm == query_norm:
            retrieval = 9.0
        else:
            common_technical = len(query_technical & row_technical)
            common_tokens = len(query_tokens & row_tokens)
            if common_technical == 0 and common_tokens == 0:
                continue
            retrieval = common_technical * 2.4 + common_tokens / max(1, len(query_tokens))
        ranked.append((retrieval, prepared))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [prepared for _, prepared in ranked[:limit]]


def find_product_candidates(conn, description: str, code: str | None = None, unit: str | None = None, limit: int = 10) -> list[Candidate]:
    attrs = technical_attributes(description)
    aliases = _alias_matches(conn, description)
    candidates: list[Candidate] = []
    catalog = _prepared_catalog(conn, "product")
    for row, row_norm, row_tokens, row_technical in _prefilter_rows(catalog, description, code):
        candidate = _candidate_from_product(row)
        if (
            attrs.get("category")
            and candidate.attrs.get("category")
            and _normalized_category(attrs.get("category"))
            != _normalized_category(candidate.attrs.get("category"))
            and not (
                code
                and candidate.code
                and canonical_key(code) == canonical_key(candidate.code)
            )
        ):
            continue
        if aliases and canonical_key(candidate.name) not in aliases and canonical_key(candidate.code or "") not in aliases:
            # Keep normal candidates too; aliases receive a deterministic boost
            # below so a stale correction cannot hide new catalog entries.
            pass
        scored = _score_candidate(
            description,
            code,
            unit,
            attrs,
            candidate,
            candidate_norm=row_norm,
            candidate_tokens=row_tokens,
        )
        if aliases and (
            canonical_key(candidate.name) in aliases or canonical_key(candidate.code or "") in aliases
        ):
            scored.score = min(0.999, scored.score + 0.10)
            scored.components["learned_alias"] = 1.0
            scored.explanation = "Khớp theo quy tắc/alias đã được kỹ sư xác nhận."
        # Avoid flooding the top list with unrelated one-token matches.
        if scored.score >= REVIEW_THRESHOLD or scored.components.get("exact_code") or scored.components.get("exact_name"):
            candidates.append(scored)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:limit]


def find_labor_candidates(conn, description: str, code: str | None = None, unit: str | None = None, limit: int = 10) -> list[Candidate]:
    attrs = technical_attributes(description)
    aliases = _alias_matches(conn, description)
    candidates: list[Candidate] = []
    catalog = _prepared_catalog(conn, "labor")
    for row, row_norm, row_tokens, row_technical in _prefilter_rows(catalog, description, code):
        candidate = _candidate_from_labor(row)
        if (
            attrs.get("category")
            and candidate.attrs.get("category")
            and _normalized_category(attrs.get("category"))
            != _normalized_category(candidate.attrs.get("category"))
            and not (
                code
                and candidate.code
                and canonical_key(code) == canonical_key(candidate.code)
            )
        ):
            continue
        scored = _score_candidate(
            description,
            code,
            unit,
            attrs,
            candidate,
            candidate_norm=row_norm,
            candidate_tokens=row_tokens,
        )
        if aliases and (
            canonical_key(candidate.name) in aliases or canonical_key(candidate.code or "") in aliases
        ):
            scored.score = min(0.999, scored.score + 0.10)
            scored.components["learned_alias"] = 1.0
            scored.explanation = "Khớp theo quy tắc/alias đã được kỹ sư xác nhận."
        if scored.score >= REVIEW_THRESHOLD or scored.components.get("exact_code") or scored.components.get("exact_name"):
            candidates.append(scored)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:limit]


def _provenance(conn, table: str, row_id: int | None) -> dict[str, Any]:
    if not row_id:
        return {}
    if table == "product_prices":
        row = conn.execute(
            """
            SELECT pp.*, sf.filename, ss.sheet_name, sr.row_no
            FROM product_prices pp
            LEFT JOIN source_files sf ON sf.id=pp.source_file_id
            LEFT JOIN source_sheets ss ON ss.id=pp.source_sheet_id
            LEFT JOIN source_rows sr ON sr.id=pp.source_row_id
            WHERE pp.id=?
            """,
            (row_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT lr.*, sf.filename, ss.sheet_name, sr.row_no
            FROM labor_rates lr
            LEFT JOIN source_files sf ON sf.id=lr.source_file_id
            LEFT JOIN source_sheets ss ON ss.id=lr.source_sheet_id
            LEFT JOIN source_rows sr ON sr.id=lr.source_row_id
            WHERE lr.id=?
            """,
            (row_id,),
        ).fetchone()
    if not row:
        return {}
    result = dict(row)
    for key in ("calc_json", "policy_json"):
        if key in result:
            result[key.removesuffix("_json")] = loads(result.pop(key), {})
    return result


def choose_product_price(conn, product_id: int, quotation_date: str | None = None) -> tuple[float | None, dict[str, Any]]:
    cache_key = (_db_identity(conn), "product", int(product_id), quotation_date)
    if cache_key in _PRICE_CACHE:
        return _PRICE_CACHE[cache_key]
    rows = conn.execute(
        """
        SELECT pp.*
        FROM product_prices pp
        WHERE pp.product_id=? AND pp.is_approved=1
          AND (? IS NULL OR pp.effective_date IS NULL OR pp.effective_date <= ?)
        ORDER BY
          CASE WHEN pp.supplier IS NOT NULL AND pp.supplier <> 'Historical quotation' THEN 0 ELSE 1 END,
          CASE WHEN pp.effective_date IS NULL THEN 1 ELSE 0 END,
          pp.effective_date DESC, pp.id DESC
        """,
        (product_id, quotation_date, quotation_date),
    ).fetchall()
    if not rows:
        _PRICE_CACHE[cache_key] = (None, {})
        return _PRICE_CACHE[cache_key]
    row = rows[0]
    _PRICE_CACHE[cache_key] = (
        _safe_float(row["net_price"]),
        _provenance(conn, "product_prices", int(row["id"])),
    )
    return _PRICE_CACHE[cache_key]


def choose_labor_rate(conn, labor_item_id: int, quotation_date: str | None = None) -> tuple[float | None, dict[str, Any]]:
    cache_key = (_db_identity(conn), "labor", int(labor_item_id), quotation_date)
    if cache_key in _PRICE_CACHE:
        return _PRICE_CACHE[cache_key]
    rows = conn.execute(
        """
        SELECT lr.*
        FROM labor_rates lr
        WHERE lr.labor_item_id=?
          AND (? IS NULL OR lr.effective_date IS NULL OR lr.effective_date <= ?)
        ORDER BY
          CASE WHEN lr.effective_date IS NULL THEN 1 ELSE 0 END,
          lr.effective_date DESC, lr.id DESC
        """,
        (labor_item_id, quotation_date, quotation_date),
    ).fetchall()
    if not rows:
        _PRICE_CACHE[cache_key] = (None, {})
        return _PRICE_CACHE[cache_key]
    row = rows[0]
    _PRICE_CACHE[cache_key] = (
        _safe_float(row["rate"]),
        _provenance(conn, "labor_rates", int(row["id"])),
    )
    return _PRICE_CACHE[cache_key]


def _price_multiplier(description: str, candidate: Candidate | None) -> float:
    """Scale a single-core catalog price for explicit parallel cable notation."""

    if not candidate:
        return 1.0
    query_attrs = technical_attributes(description)
    query_parallel = _safe_float(query_attrs.get("parallel_runs"))
    if not query_parallel or query_parallel <= 1:
        return 1.0
    candidate_attrs = candidate.attrs or {}
    candidate_parallel = _safe_float(candidate_attrs.get("parallel_runs")) or 1.0
    candidate_cores = _safe_float(candidate_attrs.get("cores"))
    candidate_base_cores = _safe_float(candidate_attrs.get("base_cores")) or candidate_cores
    query_base_cores = _safe_float(query_attrs.get("base_cores")) or _safe_float(
        query_attrs.get("cores")
    )
    # Only scale when the matched catalog item is a single-core item. A
    # historical row that already says 12x1C carries its own aggregate price.
    if (
        candidate_parallel <= 1
        and candidate_base_cores is not None
        and query_base_cores is not None
        and candidate_base_cores == query_base_cores
    ):
        return query_parallel
    return 1.0


def _candidate_signature(candidate: Candidate | None) -> tuple[Any, ...]:
    if not candidate:
        return ()
    attrs = candidate.attrs or {}
    family = normalize_text(attrs.get("cable_family", ""))
    base_cores = attrs.get("base_cores", attrs.get("cores"))
    cross = attrs.get("cross_section_mm2")
    voltage = normalize_text(attrs.get("voltage", ""))
    voltage_class = normalize_text(attrs.get("voltage_class", ""))
    armour = normalize_text(attrs.get("armour", ""))
    diameter = attrs.get("diameters_mm", attrs.get("diameter_mm"))
    if isinstance(diameter, list):
        diameter = tuple(diameter)
    category = _normalized_category(attrs.get("category"))
    shape = _shape_tokens(candidate.name)
    # Include the full compound shape so ``4x10`` and ``4x10+1x6`` are not
    # silently treated as interchangeable just because their primary core
    # and section happen to agree.
    return (
        category,
        family,
        base_cores,
        cross,
        voltage,
        voltage_class,
        armour,
        diameter,
        shape,
    )


def _round_money(value: float | None) -> float | None:
    if value is None:
        return None
    # VND quotations are conventionally rounded to whole đồng.
    return float(round(value))


def _status_for(
    material_candidate: Candidate | None,
    material_price: float | None,
    labor_candidate: Candidate | None,
    labor_price: float | None,
    quantity: float | None,
) -> tuple[str, str, str]:
    if not material_candidate and not labor_candidate:
        return "NO_MATCH", "HIGH", "Không tìm thấy ứng viên vật tư hoặc nhân công."
    hard_uncertain = any(
        c and c.score < AUTO_THRESHOLD for c in (material_candidate, labor_candidate)
    )
    missing_price = (material_candidate is not None and material_price is None) or (
        labor_candidate is not None and labor_price is None
    )
    if missing_price and not hard_uncertain:
        return "NO_PRICE_FOUND", "HIGH", "Có ứng viên nhưng chưa có đơn giá có nguồn."
    # A row with only one side matched is not safe to auto-approve: the
    # platform is expected to surface the missing material/labor side for
    # human confirmation rather than implying a complete quotation.
    missing_candidate = material_candidate is None or labor_candidate is None
    if hard_uncertain or missing_price or missing_candidate or quantity is None:
        return "REVIEW_REQUIRED", "MEDIUM", "Cần kỹ sư kiểm tra ứng viên, thuộc tính hoặc nguồn giá."
    return "AUTO_APPROVED", "LOW", "Khớp độ tin cậy cao và đã truy xuất được nguồn giá."


def _candidate_json(candidate: Candidate | None, price: float | None, source: dict[str, Any]) -> dict[str, Any]:
    if not candidate:
        return {}
    return {
        "id": candidate.entity_id,
        "name": candidate.name,
        "code": candidate.code,
        "unit": candidate.unit,
        "brand": candidate.brand,
        "origin": candidate.origin,
        "score": candidate.score,
        "components": candidate.components,
        "explanation": candidate.explanation,
        "price": price,
        "source": source,
    }


def _policy_bool(value: Any, default: bool = False) -> bool:
    """Parse policy booleans without treating ``"false"`` as truthy."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return normalize_text(value) in {"1", "true", "yes", "on", "enabled"}


def _llm_ambiguous(
    candidates: list[Candidate],
    *,
    margin: float,
) -> bool:
    """Return whether the top two candidates warrant semantic reranking."""

    if len(candidates) < 2:
        return False
    first, second = candidates[0], candidates[1]
    if _candidate_signature(first) == _candidate_signature(second):
        # Duplicate technical signatures do not benefit from an LLM call and
        # should remain deterministic (price/provenance policy decides ties).
        return False
    try:
        delta = float(first.score) - float(second.score)
    except (TypeError, ValueError):
        return True
    return delta < max(0.0, float(margin))


def _reorder_candidates_from_llm(
    original: list[Candidate],
    result: RerankResult,
) -> tuple[list[Candidate], bool]:
    """Map untrusted model output back to existing Candidate objects by ID.

    The model can only select IDs from the projected prompt. We intentionally
    discard any model-created fields/scores and return the original objects.
    """

    by_id = {int(candidate.entity_id): candidate for candidate in original}
    ordered: list[Candidate] = []
    seen: set[int] = set()
    for value in result.candidates:
        if not isinstance(value, dict):
            continue
        raw_id = value.get("id", value.get("entity_id"))
        if isinstance(raw_id, bool):
            continue
        if isinstance(raw_id, float) and not raw_id.is_integer():
            continue
        try:
            candidate_id = int(str(raw_id).strip())
        except (TypeError, ValueError):
            continue
        if candidate_id in by_id and candidate_id not in seen:
            ordered.append(by_id[candidate_id])
            seen.add(candidate_id)
    if not ordered:
        return list(original), False
    ordered.extend(candidate for candidate in original if candidate.entity_id not in seen)
    return ordered, bool(result.applied and not result.needs_review)


def _new_model_usage(provider: AIProvider, enabled: bool) -> dict[str, Any]:
    """Create a secret-free per-run semantic usage accumulator."""

    return {
        "enabled": bool(enabled),
        "provider_configured": bool(provider.configured),
        "model": provider.model or None,
        "max_calls": provider.max_calls,
        "calls": 0,
        "rerank_attempts": 0,
        "rerank_applied": 0,
        "rerank_review": 0,
        "rerank_skipped": 0,
        "rerank_fallback": 0,
        "tokens": {},
        "latency_ms": 0.0,
        "reasons": {},
    }


def _record_model_usage(usage: dict[str, Any], result: RerankResult) -> None:
    """Aggregate safe diagnostics from one rerank result."""

    usage["rerank_attempts"] = int(usage.get("rerank_attempts", 0)) + 1
    if result.applied and not result.needs_review:
        usage["rerank_applied"] = int(usage.get("rerank_applied", 0)) + 1
    elif result.needs_review:
        usage["rerank_review"] = int(usage.get("rerank_review", 0)) + 1
    else:
        usage["rerank_fallback"] = int(usage.get("rerank_fallback", 0)) + 1
    reason = str(result.reason or "unknown")
    reasons = usage.setdefault("reasons", {})
    reasons[reason] = int(reasons.get(reason, 0)) + 1
    response = result.response
    if response is None:
        return
    latency = response.latency_ms
    if isinstance(latency, (int, float)):
        usage["latency_ms"] = round(float(usage.get("latency_ms", 0.0)) + float(latency), 3)
    token_usage = response.usage or {}
    if isinstance(token_usage, dict):
        aggregate = usage.setdefault("tokens", {})
        for key, value in token_usage.items():
            # Gateways normally expose token counters. Do not persist arbitrary
            # provider metadata (which could contain prices, credentials, or
            # provenance) in the run audit record.
            normalized_key = normalize_text(key).replace(" ", "_")
            if (
                normalized_key in {
                    "price",
                    "net_price",
                    "unit_price",
                    "rate",
                    "cost",
                    "total",
                    "source",
                    "provenance",
                    "api_key",
                    "authorization",
                }
                or not isinstance(value, (int, float))
            ):
                continue
            aggregate[normalized_key] = aggregate.get(normalized_key, 0) + value


def run_pricing(
    project_id: int,
    *,
    policy: dict[str, Any] | None = None,
    force: bool = False,
    llm_provider: AIProvider | None = None,
) -> dict[str, Any]:
    """Run deterministic hybrid matching for all BOQ items in a project."""

    policy = dict(policy or {
        "material": "latest_supplier_net_then_historical",
        "labor": "latest_historical_rate",
        "auto_threshold": AUTO_THRESHOLD,
        "review_threshold": REVIEW_THRESHOLD,
    })
    semantic_provider = llm_provider or ai_provider
    llm_requested = _policy_bool(
        policy.get("llm_enabled"),
        default=semantic_provider.configured,
    )
    llm_enabled = bool(llm_requested and semantic_provider.configured)
    try:
        llm_margin = max(
            0.0,
            float(policy.get("llm_rerank_margin", semantic_provider.rerank_margin)),
        )
    except (TypeError, ValueError):
        llm_margin = semantic_provider.rerank_margin
    try:
        llm_max_candidates = max(
            1,
            int(policy.get("llm_max_candidates", semantic_provider.max_candidates)),
        )
    except (TypeError, ValueError):
        llm_max_candidates = semantic_provider.max_candidates
    if llm_enabled:
        reset_budget = getattr(semantic_provider, "reset_budget", None)
        if callable(reset_budget):
            reset_budget()
    model_usage = _new_model_usage(semantic_provider, llm_enabled)
    model_usage["requested"] = bool(llm_requested)
    model_usage["rerank_margin"] = llm_margin
    model_usage["max_candidates"] = llm_max_candidates
    with db_session() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise KeyError(f"project:{project_id}")
        pcur = conn.execute(
            """
            INSERT INTO pricing_runs(project_id, policy_json, status, started_at, created_at)
            VALUES (?, ?, 'RUNNING', ?, ?)
            """,
            (project_id, dumps(policy), utc_now(), utc_now()),
        )
        run_id = int(pcur.lastrowid)
        # A project may be a historical workbook used as a holdout. Pricing
        # runs must still be able to evaluate those rows; the holdout guard is
        # enforced at ingest time by excluding its source prices.
        items = conn.execute(
            "SELECT * FROM boq_items WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
        counts = {
            "total_items": len(items),
            "auto_approved": 0,
            "review_required": 0,
            "no_match": 0,
            "no_price_found": 0,
            "material_matched": 0,
            "labor_matched": 0,
            "material_priced": 0,
            "labor_priced": 0,
        }
        quotation_date = project["quotation_date"]
        for item in items:
            description = item["raw_description"] or item["normalized_description"]
            product_candidates = find_product_candidates(
                conn, description, item["product_code"], item["unit"], limit=10
            )
            labor_candidates = find_labor_candidates(
                conn, description, item["product_code"], item["unit"], limit=10
            )
            product_rerank_applied = False
            labor_rerank_applied = False
            if llm_enabled:
                if _llm_ambiguous(product_candidates, margin=llm_margin):
                    try:
                        product_rerank = safe_rerank_sync(
                            description,
                            technical_attributes(description),
                            product_candidates,
                            provider=semantic_provider,
                            only_if_ambiguous=True,
                            ambiguity_margin=llm_margin,
                            max_candidates=llm_max_candidates,
                        )
                    except Exception as exc:
                        # A provider seam may be replaced by an integration
                        # test/client; never let an unexpected adapter error
                        # change deterministic pricing behavior.
                        product_rerank = RerankResult(
                            candidates=[],
                            applied=False,
                            needs_review=True,
                            explanation="LLM rerank lỗi; giữ kết quả deterministic.",
                            reason=f"adapter_error:{type(exc).__name__}",
                        )
                    _record_model_usage(model_usage, product_rerank)
                    product_candidates, product_rerank_applied = (
                        _reorder_candidates_from_llm(product_candidates, product_rerank)
                    )
                else:
                    model_usage["rerank_skipped"] = int(
                        model_usage.get("rerank_skipped", 0)
                    ) + 1
                if _llm_ambiguous(labor_candidates, margin=llm_margin):
                    try:
                        labor_rerank = safe_rerank_sync(
                            description,
                            technical_attributes(description),
                            labor_candidates,
                            provider=semantic_provider,
                            only_if_ambiguous=True,
                            ambiguity_margin=llm_margin,
                            max_candidates=llm_max_candidates,
                        )
                    except Exception as exc:
                        labor_rerank = RerankResult(
                            candidates=[],
                            applied=False,
                            needs_review=True,
                            explanation="LLM rerank lỗi; giữ kết quả deterministic.",
                            reason=f"adapter_error:{type(exc).__name__}",
                        )
                    _record_model_usage(model_usage, labor_rerank)
                    labor_candidates, labor_rerank_applied = (
                        _reorder_candidates_from_llm(labor_candidates, labor_rerank)
                    )
                else:
                    model_usage["rerank_skipped"] = int(
                        model_usage.get("rerank_skipped", 0)
                    ) + 1
            product = product_candidates[0] if product_candidates else None
            labor = labor_candidates[0] if labor_candidates else None
            # Close scores mean ambiguity, even if both are individually high.
            if (
                len(product_candidates) > 1
                and product
                and product.score - product_candidates[1].score < 0.045
                and _candidate_signature(product) != _candidate_signature(product_candidates[1])
                and not product_rerank_applied
            ):
                product = None
            if (
                len(labor_candidates) > 1
                and labor
                and labor.score - labor_candidates[1].score < 0.045
                and _candidate_signature(labor) != _candidate_signature(labor_candidates[1])
                and not labor_rerank_applied
            ):
                labor = None
            material_price, material_source = (
                choose_product_price(conn, product.entity_id, quotation_date) if product else (None, {})
            )
            labor_price, labor_source = (
                choose_labor_rate(conn, labor.entity_id, quotation_date) if labor else (None, {})
            )
            material_multiplier = _price_multiplier(description, product)
            labor_multiplier = _price_multiplier(description, labor)
            if material_price is not None and material_multiplier != 1:
                material_price = _round_money(material_price * material_multiplier)
                material_source = {
                    **material_source,
                    "calculation": f"base_price × {material_multiplier:g} parallel runs",
                    "multiplier": material_multiplier,
                }
            if labor_price is not None and labor_multiplier != 1:
                labor_price = _round_money(labor_price * labor_multiplier)
                labor_source = {
                    **labor_source,
                    "calculation": f"base_rate × {labor_multiplier:g} parallel runs",
                    "multiplier": labor_multiplier,
                }
            status, risk, explanation = _status_for(
                product, material_price, labor, labor_price, _safe_float(item["quantity"])
            )
            material_conf = product.score if product else None
            labor_conf = labor.score if labor else None
            qty = _safe_float(item["quantity"])
            material_total = _round_money(qty * material_price) if qty is not None and material_price is not None else None
            labor_total = _round_money(qty * labor_price) if qty is not None and labor_price is not None else None
            material_alternatives: list[dict[str, Any]] = []
            for candidate in product_candidates:
                candidate_price, candidate_source = choose_product_price(
                    conn, candidate.entity_id, quotation_date
                )
                if candidate_price is not None:
                    candidate_price = _round_money(
                        candidate_price * _price_multiplier(description, candidate)
                    )
                material_alternatives.append(
                    _candidate_json(candidate, candidate_price, candidate_source)
                )
            labor_alternatives: list[dict[str, Any]] = []
            for candidate in labor_candidates:
                candidate_rate, candidate_source = choose_labor_rate(
                    conn, candidate.entity_id, quotation_date
                )
                if candidate_rate is not None:
                    candidate_rate = _round_money(
                        candidate_rate * _price_multiplier(description, candidate)
                    )
                labor_alternatives.append(
                    _candidate_json(candidate, candidate_rate, candidate_source)
                )
            alternatives = {
                "material": material_alternatives,
                "labor": labor_alternatives,
            }
            conn.execute(
                """
                UPDATE boq_items
                SET pricing_run_id=?, matched_product_id=?, matched_labor_item_id=?,
                    material_price=?, labor_price=?, material_total=?, labor_total=?,
                    material_confidence=?, labor_confidence=?,
                    material_source_json=?, labor_source_json=?,
                    status=?, risk=?, explanation=?, alternatives_json=?
                WHERE id=?
                """,
                (
                    run_id,
                    product.entity_id if product else None,
                    labor.entity_id if labor else None,
                    material_price,
                    labor_price,
                    material_total,
                    labor_total,
                    material_conf,
                    labor_conf,
                    dumps(material_source),
                    dumps(labor_source),
                    status,
                    risk,
                    explanation,
                    dumps(alternatives),
                    item["id"],
                ),
            )
            for rank, candidate in enumerate(product_candidates, 1):
                conn.execute(
                    """
                    INSERT INTO match_candidates(
                        pricing_run_id, boq_item_id, candidate_type, candidate_id,
                        rank_no, score, score_components_json, explanation
                    ) VALUES (?, ?, 'material', ?, ?, ?, ?, ?)
                    """,
                    (run_id, item["id"], candidate.entity_id, rank, candidate.score, dumps(candidate.components), candidate.explanation),
                )
            for rank, candidate in enumerate(labor_candidates, 1):
                conn.execute(
                    """
                    INSERT INTO match_candidates(
                        pricing_run_id, boq_item_id, candidate_type, candidate_id,
                        rank_no, score, score_components_json, explanation
                    ) VALUES (?, ?, 'labor', ?, ?, ?, ?, ?)
                    """,
                    (run_id, item["id"], candidate.entity_id, rank, candidate.score, dumps(candidate.components), candidate.explanation),
                )
            counts["material_matched"] += int(product is not None)
            counts["labor_matched"] += int(labor is not None)
            counts["material_priced"] += int(material_price is not None)
            counts["labor_priced"] += int(labor_price is not None)
            key = {
                "AUTO_APPROVED": "auto_approved",
                "REVIEW_REQUIRED": "review_required",
                "NO_MATCH": "no_match",
                "NO_PRICE_FOUND": "no_price_found",
            }.get(status)
            if key:
                counts[key] += 1
        if counts["total_items"]:
            counts["auto_coverage"] = round(counts["auto_approved"] / counts["total_items"], 4)
            counts["material_coverage"] = round(counts["material_priced"] / counts["total_items"], 4)
            counts["labor_coverage"] = round(counts["labor_priced"] / counts["total_items"], 4)
        else:
            counts.update(auto_coverage=0.0, material_coverage=0.0, labor_coverage=0.0)
        # Persist only aggregate semantic telemetry. No prompt, API key,
        # candidate prices, or provenance are stored in model_usage_json.
        try:
            model_usage["calls"] = int(semantic_provider.calls_made)
            model_usage["remaining_calls"] = int(semantic_provider.remaining_calls)
        except (AttributeError, TypeError, ValueError):
            model_usage["calls"] = int(model_usage.get("calls", 0))
        counts.update(
            llm_enabled=bool(llm_enabled),
            llm_requested=bool(llm_requested),
            llm_calls=int(model_usage.get("calls", 0)),
            llm_rerank_attempts=int(model_usage.get("rerank_attempts", 0)),
            llm_rerank_applied=int(model_usage.get("rerank_applied", 0)),
            llm_rerank_review=int(model_usage.get("rerank_review", 0)),
            llm_rerank_fallback=int(model_usage.get("rerank_fallback", 0)),
            llm_rerank_skipped=int(model_usage.get("rerank_skipped", 0)),
        )
        conn.execute(
            """
            UPDATE pricing_runs
            SET status='COMPLETED', metrics_json=?, model_usage_json=?, finished_at=?
            WHERE id=?
            """,
            (dumps(counts), dumps(model_usage), utc_now(), run_id),
        )
        conn.execute(
            """
            INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at)
            VALUES ('PRICING_COMPLETED', 'pricing_run', ?, ?, ?)
            """,
            (
                run_id,
                dumps({"metrics": counts, "model_usage": model_usage}),
                utc_now(),
            ),
        )
        return {
            "run_id": run_id,
            "project_id": project_id,
            "status": "COMPLETED",
            "metrics": counts,
            "model_usage": model_usage,
        }


def serialize_boq_item(conn, row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in ("raw_cells_json", "material_source_json", "labor_source_json", "alternatives_json"):
        out_key = key.removesuffix("_json")
        result[out_key] = loads(result.pop(key), {} if "source" in key else [])
    result["technical_attributes"] = technical_attributes(result.get("raw_description") or "")
    if result.get("matched_product_id"):
        p = conn.execute("SELECT * FROM products WHERE id=?", (result["matched_product_id"],)).fetchone()
        result["matched_product"] = (
            {
                "id": p["id"],
                "name": p["normalized_name"],
                "code": p["product_code"],
                "unit": p["unit"],
                "attrs": loads(p["technical_attributes_json"], {}),
            }
            if p
            else None
        )
    else:
        result["matched_product"] = None
    if result.get("matched_labor_item_id"):
        l = conn.execute("SELECT * FROM labor_items WHERE id=?", (result["matched_labor_item_id"],)).fetchone()
        result["matched_labor"] = (
            {"id": l["id"], "name": l["normalized_name"], "code": l["code"], "unit": l["unit"]}
            if l
            else None
        )
    else:
        result["matched_labor"] = None
    # Stable API aliases consumed by the review UI and useful to external
    # clients. Keep the richer material/labor split while also exposing a flat
    # candidate list for generic review tooling.
    result["material_source"] = result.get("material_source") or {}
    result["labor_source"] = result.get("labor_source") or {}
    alternatives = result.get("alternatives") or {}
    material_candidates = alternatives.get("material", []) if isinstance(alternatives, dict) else []
    labor_candidates = alternatives.get("labor", []) if isinstance(alternatives, dict) else []
    result["candidates"] = [
        {**candidate, "candidate_type": "material"} for candidate in material_candidates
    ] + [
        {**candidate, "candidate_type": "labor"} for candidate in labor_candidates
    ]
    # The current selected product is the recommendation; preserve candidate
    # IDs so approval works even when no alternatives were returned.
    if result.get("matched_product"):
        result["recommended_match"] = {
            **result["matched_product"],
            "id": result["matched_product"]["id"],
            "score": result.get("material_confidence"),
            "candidate_type": "material",
        }
    elif result.get("matched_labor"):
        result["recommended_match"] = {
            **result["matched_labor"],
            "id": result["matched_labor"]["id"],
            "score": result.get("labor_confidence"),
            "candidate_type": "labor",
        }
    else:
        result["recommended_match"] = None
    return result


def get_project_result(project_id: int, run_id: int | None = None) -> dict[str, Any]:
    with db_session() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise KeyError(f"project:{project_id}")
        if run_id is None:
            run = conn.execute(
                "SELECT * FROM pricing_runs WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)
            ).fetchone()
        else:
            run = conn.execute("SELECT * FROM pricing_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
        query = "SELECT * FROM boq_items WHERE project_id=?"
        params: list[Any] = [project_id]
        if run:
            query += " AND pricing_run_id=?"
            params.append(run["id"])
        rows = [serialize_boq_item(conn, r) for r in conn.execute(query + " ORDER BY id", params).fetchall()]
        metrics = loads(run["metrics_json"], {}) if run else {}
        return {
            "project": dict(project),
            "run": dict(run) if run else None,
            "metrics": metrics,
            "items": rows,
        }


def review_item(
    item_id: int,
    *,
    selected_product_id: int | None = None,
    selected_labor_item_id: int | None = None,
    material_price: float | None = None,
    labor_price: float | None = None,
    material_source: dict[str, Any] | None = None,
    labor_source: dict[str, Any] | None = None,
    status: str | None = None,
    add_alias: bool = False,
    created_by: str = "engineer",
) -> dict[str, Any]:
    with db_session() as conn:
        item = conn.execute("SELECT * FROM boq_items WHERE id=?", (item_id,)).fetchone()
        if not item:
            raise KeyError(f"boq_item:{item_id}")
        old_product = item["matched_product_id"]
        old_labor = item["matched_labor_item_id"]
        final_material_price = _safe_float(item["material_price"])
        final_labor_price = _safe_float(item["labor_price"])
        final_material_source = loads(item["material_source_json"], {})
        final_labor_source = loads(item["labor_source_json"], {})

        if selected_product_id is not None:
            price, source = choose_product_price(conn, selected_product_id)
            # A newly selected candidate replaces the old product price.  In
            # particular, do not leave a stale price attached to a candidate
            # for which no sourced price exists.
            final_material_price = (
                _safe_float(material_price) if material_price is not None else price
            )
            final_material_source = material_source or source or {}
        elif material_price is not None:
            final_material_price = _safe_float(material_price)
            final_material_source = material_source or {
                "type": "manual",
                "entered_by": created_by,
                "entered_at": utc_now(),
            }

        if selected_labor_item_id is not None:
            price, source = choose_labor_rate(conn, selected_labor_item_id)
            final_labor_price = (
                _safe_float(labor_price) if labor_price is not None else price
            )
            final_labor_source = labor_source or source or {}
        elif labor_price is not None:
            final_labor_price = _safe_float(labor_price)
            final_labor_source = labor_source or {
                "type": "manual",
                "entered_by": created_by,
                "entered_at": utc_now(),
            }

        qty = _safe_float(item["quantity"])
        material_total = (
            _round_money(qty * final_material_price)
            if qty is not None and final_material_price is not None
            else None
        )
        labor_total = (
            _round_money(qty * final_labor_price)
            if qty is not None and final_labor_price is not None
            else None
        )
        selected_side_without_price = (
            selected_product_id is not None and final_material_price is None
        ) or (
            selected_labor_item_id is not None and final_labor_price is None
        )
        final_status = status or (
            "AUTO_APPROVED"
            if final_material_price is not None or final_labor_price is not None
            else "REVIEW_REQUIRED"
        )
        # Never let an explicit "approve" action turn a newly selected,
        # unsourced candidate into an apparently complete quotation.
        if final_status == "AUTO_APPROVED" and (
            selected_side_without_price
            or (final_material_price is None and final_labor_price is None)
        ):
            final_status = "REVIEW_REQUIRED"
        risk = (
            "LOW"
            if final_status == "AUTO_APPROVED"
            else "HIGH"
            if final_status in {"NO_MATCH", "NO_PRICE_FOUND", "NEEDS_SUPPLIER_QUOTATION"}
            else "MEDIUM"
        )
        explanation = (
            "Đã được kỹ sư review/xác nhận."
            if final_status == "AUTO_APPROVED"
            else "Đã review nhưng vẫn cần bổ sung candidate hoặc đơn giá có nguồn."
        )

        # Build the update list explicitly so selecting a candidate can clear
        # stale price/provenance values while an unrelated manual action keeps
        # untouched fields intact.
        assignments = [
            "material_total=?",
            "labor_total=?",
            "status=?",
            "risk=?",
            "explanation=?",
            "reviewed=1",
        ]
        parameters: list[Any] = [
            material_total,
            labor_total,
            final_status,
            risk,
            explanation,
        ]
        if selected_product_id is not None:
            assignments.extend(
                ["matched_product_id=?", "material_price=?", "material_source_json=?"]
            )
            parameters.extend(
                [
                    selected_product_id,
                    final_material_price,
                    dumps(final_material_source or {}),
                ]
            )
        elif material_price is not None:
            assignments.extend(["material_price=?", "material_source_json=?"])
            parameters.extend(
                [final_material_price, dumps(final_material_source or {})]
            )
        if selected_labor_item_id is not None:
            assignments.extend(
                ["matched_labor_item_id=?", "labor_price=?", "labor_source_json=?"]
            )
            parameters.extend(
                [
                    selected_labor_item_id,
                    final_labor_price,
                    dumps(final_labor_source or {}),
                ]
            )
        elif labor_price is not None:
            assignments.extend(["labor_price=?", "labor_source_json=?"])
            parameters.extend([final_labor_price, dumps(final_labor_source or {})])
        parameters.append(item_id)
        conn.execute(
            f"UPDATE boq_items SET {', '.join(assignments)} WHERE id=?",
            tuple(parameters),
        )
        selected_parts: list[str] = []
        alias_targets: list[str] = []
        if selected_product_id is not None:
            selected_parts.append(f"product:{selected_product_id}")
            selected_row = conn.execute(
                "SELECT normalized_name, product_code FROM products WHERE id=?",
                (selected_product_id,),
            ).fetchone()
            if selected_row:
                alias_targets.append(
                    canonical_key(selected_row["product_code"] or selected_row["normalized_name"])
                )
        if selected_labor_item_id is not None:
            selected_parts.append(f"labor:{selected_labor_item_id}")
            selected_row = conn.execute(
                "SELECT normalized_name, code FROM labor_items WHERE id=?",
                (selected_labor_item_id,),
            ).fetchone()
            if selected_row:
                alias_targets.append(
                    canonical_key(selected_row["code"] or selected_row["normalized_name"])
                )
        selected_text = ",".join(selected_parts)
        old_parts = []
        if old_product is not None:
            old_parts.append(f"product:{old_product}")
        if old_labor is not None:
            old_parts.append(f"labor:{old_labor}")
        old_text = ",".join(old_parts)
        conn.execute(
            """
            INSERT INTO corrections(
                boq_item_id, raw_description, context_json, old_candidate,
                selected_candidate, rule_type, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                item["raw_description"],
                dumps({"unit": item["unit"], "brand": item["brand"], "origin": item["origin"]}),
                old_text,
                selected_text,
                "manual_selection",
                created_by,
                utc_now(),
            ),
        )
        if add_alias and alias_targets:
            for alias_target in alias_targets:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO aliases(alias, canonical, rule_type, context_json, created_at)
                    VALUES (?, ?, 'manual_alias', ?, ?)
                    """,
                    (
                        canonical_key(item["raw_description"]),
                        alias_target,
                        dumps(
                            {
                                "item_id": item_id,
                                "selected_product_id": selected_product_id,
                                "selected_labor_item_id": selected_labor_item_id,
                            }
                        ),
                        utc_now(),
                    ),
                )
        result = conn.execute("SELECT * FROM boq_items WHERE id=?", (item_id,)).fetchone()
        return serialize_boq_item(conn, result)


def catalog_stats() -> dict[str, Any]:
    with db_session() as conn:
        products = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
        prices = conn.execute("SELECT COUNT(*) AS n FROM product_prices").fetchone()["n"]
        labor_items = conn.execute("SELECT COUNT(*) AS n FROM labor_items").fetchone()["n"]
        labor_rates = conn.execute("SELECT COUNT(*) AS n FROM labor_rates").fetchone()["n"]
        files = conn.execute("SELECT COUNT(*) AS n FROM source_files").fetchone()["n"]
        projects = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        return {
            "source_files": files,
            "products": products,
            "product_prices": prices,
            "labor_items": labor_items,
            "labor_rates": labor_rates,
            "projects": projects,
        }
