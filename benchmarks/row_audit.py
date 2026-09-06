from __future__ import annotations

"""Generic BOQ row classification helpers used by the holdout audit.

The ingestion parser intentionally keeps its small ``data/section/empty``
vocabulary because that vocabulary is useful for all workbook families.  A
benchmark, however, needs a more explicit explanation of which rows are
actually priceable.  This module adds that audit layer without changing the
pricing engine or dropping any rows from the benchmark.

Classification is based on row shape, technical/product signals, structural
language and the parsed sheet type.  It does not inspect the holdout filename,
project name or fixed row numbers.
"""

import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from app.normalize import normalize_text

ROW_CLASSES = (
    "PRICEABLE_LINE_ITEM",
    "SECTION",
    "SUBSECTION",
    "NOTE",
    "SUBTOTAL",
    "TOTAL",
    "HEADER",
    "NON_PRICEABLE_REFERENCE",
    "UNKNOWN",
)

_BOQ_SHEET_TYPES = {"BOQ", "HISTORICAL_BOQ", "PANEL_BOM"}

# These are deliberately token/phrase based rather than project-specific
# strings.  They are used only to explain the classification, not to alter
# matching or pricing.
_TOTAL_PHRASES = (
    "tong cong a+b",
    "tong cong a",
    "tong cong b",
    "tong cong",
    "tong so",
    "subtotal",
    "grand total",
    "total",
)
_NOTE_PHRASES = (
    "ghi chu",
    "note",
    "remark",
    "lưu ý",
    "luu y",
)
_REFERENCE_PHRASES = (
    "tham khao",
    "reference",
    "xem ban ve",
    "theo ban ve",
    "chi tiet ban ve",
    "khong bao gia",
    "khong tinh",
)
_STRUCTURAL_PHRASES = (
    "he thong",
    "tu dien",
    "chiếu sáng",
    "chieu sang",
    "tiep dia",
    "chong set",
    "cap nguon",
    "chuong heo",
    "khu ",
    "dau vao",
    "dau ra",
    "khoang ",
    "vat tu nhan cong hoan thien",
    "thiet bi tu dien",
    "hang muc phu",
    "hang muc chinh",
    "cong tac khac",
    "vat tu khac",
)
_UNIT_LIKE = {
    "m",
    "m2",
    "m3",
    "mm",
    "kg",
    "bo",
    "bộ",
    "cai",
    "cái",
    "con",
    "coc",
    "cột",
    "cot",
    "cay",
    "cây",
    "goi",
    "gói",
    "ho",
    "hố",
    "lo",
    "lô",
    "thanh",
    "tru",
    "trụ",
    "tu",
    "tủ",
    "set",
    "lot",
    "system",
    "he thong",
}


def _contains_phrase(text: str, phrases: Iterable[str]) -> str | None:
    for phrase in phrases:
        if phrase in text:
            return phrase
    return None


def _has_number(text: str) -> bool:
    return bool(re.search(r"\d", text))


def _contains_marker(text: str, marker: str) -> bool:
    """Match short marker tokens without matching inside longer words."""

    raw_marker = str(marker)
    marker = normalize_text(marker)
    if raw_marker.endswith(" ") or len(marker) <= 4:
        token = marker.strip()
        return bool(
            token
            and re.search(rf"(?<![a-z0-9]){re.escape(token)}(?=\s|$)", text)
        )
    return marker in text


def _is_upper_title(raw: Any, normalized: str) -> bool:
    value = str(raw or "").strip()
    letters = [char for char in value if char.isalpha()]
    return bool(letters) and sum(char.isupper() for char in letters) / len(letters) >= 0.78


def _looks_like_header(description: str, raw_cells: Mapping[str, Any]) -> bool:
    text = " ".join(
        [description, *(normalize_text(value) for value in raw_cells.values())]
    )
    header_markers = (
        "noi dung cong viec",
        "dien giai",
        "mo ta description",
        "mo ta",
        "hang muc linh phu kien",
        "khoi luong",
        "so luong",
        "don gia vt",
        "don gia nhan cong",
        "thanh tien",
        "vat tu",
        "nhan cong",
        "don vi",
        "dvt",
    )
    marker_count = sum(marker in text for marker in header_markers)
    # A repeated header usually spans multiple cells. Requiring more than one
    # populated raw cell prevents a subsection such as "Vật tư nhân công hoàn
    # thiện" from being mistaken for a header merely because it contains two
    # header-like words.
    return (
        marker_count >= 2 and len(raw_cells) >= 2
    ) or description in {"stt", "tt"}


def _description_signal(text: str) -> bool:
    """Return whether a description has a product/work-item signal."""

    if not text:
        return False
    if _contains_phrase(text, _STRUCTURAL_PHRASES):
        # Structural phrases can still be part of a product name
        # ("cáp ... từ tủ điện ...").  Technical markers take precedence.
        if any(
            _contains_marker(text, marker)
            for marker in (
                "cap ",
                "day ",
                "ong ",
                "den ",
                "mcb",
                "mccb",
                "rccb",
                "rcbo",
                "acb",
                "aptomat",
                "thang cap",
                "mang cap",
                "kim thu",
                "coc tiep dia",
                "van ",
                "bom ",
                "quat ",
                "cong tac",
                "o cam",
                "vo tu",
                "tu dien msb",
            )
        ):
            return True
        return False
    # Common engineering product/work markers, including generic lines such
    # as "vật tư phụ" and "nhân công" which are still priceable bundle rows.
    return any(
        _contains_marker(text, marker)
        for marker in (
            "cap ",
            "day ",
            "ong ",
            "den ",
            "mcb",
            "mccb",
            "rccb",
            "rcbo",
            "acb",
            "aptomat",
            "thang cap",
            "mang cap",
            "kim thu",
            "tiep dia",
            "coc ",
            "thanh ",
            "tu dien",
            "vo tu",
            "vat tu",
            "nhan cong",
            "cong tac",
            "o cam",
            "cong tac",
            "hộp",
            "hop ",
            "mong ",
            "tru ",
            "cot ",
            "bom ",
            "quat ",
            "bien ap",
            "dong ho",
            "chuyen mach",
            "bien dong",
            "cau chi",
            "den bao",
            "phu kien",
            "kẹp ",
            "kep ",
            "han hoa nhiet",
            "dao va lap",
        )
    ) or _has_number(text)


def classify_row(
    row: Mapping[str, Any],
    *,
    sheet_type: str,
    previous_class: str | None = None,
) -> dict[str, Any]:
    """Classify one parsed row and return a reasoned, serializable record.

    ``row`` is the dictionary emitted by :func:`app.excel.extract_rows`.
    ``previous_class`` is optional context used only to distinguish a nested
    heading from a top-level section; it never changes whether a product row is
    priceable.
    """

    fields = row.get("fields") or {}
    raw_cells = row.get("raw_cells") or {}
    raw_description = fields.get("description")
    description = normalize_text(raw_description)
    unit_raw = fields.get("unit")
    unit = normalize_text(unit_raw)
    row_text = normalize_text(
        " ".join(
            [
                description,
                *(
                    str(value)
                    for value in raw_cells.values()
                    if value not in (None, "")
                ),
            ]
        )
    )
    quantity = fields.get("quantity")
    material_price = fields.get("material_price")
    labor_price = fields.get("labor_price")
    list_price = fields.get("list_price")
    vat_price = fields.get("vat_price")
    total = fields.get("total")
    parser_kind = str(row.get("row_kind") or "")

    if not description and not raw_cells:
        return {
            "classification": "UNKNOWN",
            "reason": "blank_row",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    # A row carrying an operational unit/quantity is usually a real BOM line,
    # even when its bundle description contains words such as "vật tư" and
    # "nhân công".  Restrict repeated-header detection to parser structural
    # rows; this keeps those bundle lines priceable.
    if parser_kind != "data" and _looks_like_header(description, raw_cells):
        return {
            "classification": "HEADER",
            "reason": "repeated_header_labels",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    total_phrase = _contains_phrase(row_text, _TOTAL_PHRASES)
    if total_phrase:
        classification = "TOTAL" if "grand" in total_phrase or "a+b" in total_phrase else "SUBTOTAL"
        return {
            "classification": classification,
            "reason": f"total_label:{total_phrase}",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    note_phrase = _contains_phrase(row_text, _NOTE_PHRASES)
    if note_phrase and not _description_signal(description):
        return {
            "classification": "NOTE",
            "reason": f"note_label:{note_phrase}",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    reference_phrase = _contains_phrase(row_text, _REFERENCE_PHRASES)
    if reference_phrase and not (material_price or labor_price or list_price or vat_price):
        return {
            "classification": "NON_PRICEABLE_REFERENCE",
            "reason": f"reference_label:{reference_phrase}",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    has_price = any(
        value is not None and float(value) > 0
        for value in (material_price, labor_price, list_price, vat_price, total)
        if isinstance(value, (int, float))
    )
    has_quantity = isinstance(quantity, (int, float))
    quantity_positive = has_quantity and float(quantity) > 0
    quantity_zero = has_quantity and float(quantity) == 0
    unit_like = unit in _UNIT_LIKE or bool(re.search(r"^[a-z]{1,5}\d*$", unit))
    structural_phrase = _contains_phrase(row_text, _STRUCTURAL_PHRASES)
    product_signal = _description_signal(description)

    # Parser sections often carry a section total in the amount columns but
    # no item unit/quantity. Preserve them as subtotal rows rather than
    # silently counting them as priced products.
    if not unit and not has_quantity and has_price:
        return {
            "classification": "SUBTOTAL",
            "reason": "amount_without_item_unit_or_quantity",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    # Explicit parser sections and title-like descriptions are structural.
    # Numeric units (e.g. "2", "16") in some merged BOQ rows are section
    # indices, not physical units.
    if parser_kind in {"empty", "section"} and not product_signal:
        if not description:
            return {
                "classification": "UNKNOWN",
                "reason": "blank_or_parser_section_without_description",
                "priceable": False,
                "parser_row_kind": parser_kind,
            }
        nested = (
            previous_class in {"SECTION", "SUBSECTION"}
            or description in {"dau vao", "dau ra"}
            or description.startswith(("khoang ", "chuong heo ", "msb-", "mdb-", "db-"))
        )
        return {
            "classification": "SUBSECTION" if nested else "SECTION",
            "reason": "structural_heading" if structural_phrase else "parser_section",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    # A heading may have a physical-looking unit accidentally mapped from a
    # neighboring merged column.  Structural language wins when there is no
    # quantity and no price.
    if (
        description
        and not has_quantity
        and not has_price
        and (not unit_like or unit.isdigit())
        and structural_phrase
        and not product_signal
    ):
        nested = previous_class in {"SECTION", "SUBSECTION"} or description in {
            "dau vao",
            "dau ra",
        }
        return {
            "classification": "SUBSECTION" if nested else "SECTION",
            "reason": "structural_description_without_pricing_fields",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    # Product/work rows are priceable even if a historical workbook carries a
    # zero quantity or omits quantity.  Those conditions are surfaced in the
    # reason so downstream reports can separate parser/data quality from
    # matcher failures.
    if description and (unit_like or has_quantity or has_price) and product_signal:
        if not has_quantity:
            reason = "item_signal_unit_or_price_but_quantity_missing"
        elif quantity_zero:
            reason = "item_signal_zero_quantity"
        elif not unit:
            reason = "item_signal_quantity_but_unit_missing"
        else:
            reason = "item_signal_with_operational_fields"
        return {
            "classification": "PRICEABLE_LINE_ITEM",
            "reason": reason,
            "priceable": True,
            "parser_row_kind": parser_kind,
        }

    # Rows with a clear physical unit and quantity are likely line items even
    # when the description uses an unfamiliar product name. Keep them
    # priceable, but mark the weaker textual signal for review.
    if description and (unit_like and (has_quantity or has_price)):
        return {
            "classification": "PRICEABLE_LINE_ITEM",
            "reason": "operational_unit_and_quantity_or_price",
            "priceable": True,
            "parser_row_kind": parser_kind,
        }

    if description and reference_phrase:
        return {
            "classification": "NON_PRICEABLE_REFERENCE",
            "reason": "reference_like_description",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    if description:
        return {
            "classification": "UNKNOWN",
            "reason": "ambiguous_description_or_missing_operational_fields",
            "priceable": False,
            "parser_row_kind": parser_kind,
        }

    return {
        "classification": "UNKNOWN",
        "reason": "unclassified_row",
        "priceable": False,
        "parser_row_kind": parser_kind,
    }


def classify_parsed_workbook(
    parsed: Mapping[str, Any],
    *,
    include_other_sheets: bool = False,
    sample_limit: int = 8,
) -> dict[str, Any]:
    """Build a generic row classification report for a parsed workbook."""

    counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    by_sheet: dict[str, dict[str, Any]] = {}
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parser_kinds: Counter[str] = Counter()
    parser_data_classes: Counter[str] = Counter()
    included_rows = 0
    excluded_rows = 0
    priceable_rows = 0
    parser_data_rows = 0
    parser_data_priceable_rows = 0
    parser_data_non_priceable_rows = 0

    for sheet in parsed.get("sheets") or []:
        if hasattr(sheet, "detected_type"):
            sheet_type = str(getattr(sheet, "detected_type", "") or "")
            sheet_name = str(
                getattr(getattr(sheet, "snapshot", None), "name", None) or ""
            )
            rows = getattr(sheet, "rows", None) or []
        else:
            sheet_type = str(sheet.get("detected_type", "") or "")
            sheet_name = str(sheet.get("name", "") or "")
            rows = sheet.get("rows", []) or []
        if not include_other_sheets and sheet_type not in _BOQ_SHEET_TYPES:
            excluded_rows += len(rows)
            continue
        sheet_counts: Counter[str] = Counter()
        previous: str | None = None
        for row in rows:
            result = classify_row(row, sheet_type=sheet_type, previous_class=previous)
            classification = result["classification"]
            reason = result["reason"]
            previous = classification
            included_rows += 1
            if result["priceable"]:
                priceable_rows += 1
            counts[classification] += 1
            sheet_counts[classification] += 1
            reason_counts[reason] += 1
            parser_kinds[str(result.get("parser_row_kind") or "UNKNOWN")] += 1
            if result.get("parser_row_kind") == "data":
                parser_data_rows += 1
                parser_data_classes[classification] += 1
                if result["priceable"]:
                    parser_data_priceable_rows += 1
                else:
                    parser_data_non_priceable_rows += 1
            if len(samples[classification]) < sample_limit:
                fields = row.get("fields") or {}
                samples[classification].append(
                    {
                        "sheet": sheet_name,
                        "row_no": row.get("row_no"),
                        "description": fields.get("description"),
                        "unit": fields.get("unit"),
                        "quantity": fields.get("quantity"),
                        "material_price": fields.get("material_price"),
                        "labor_price": fields.get("labor_price"),
                        "reason": reason,
                    }
                )
        by_sheet[sheet_name] = {
            "sheet_type": sheet_type,
            "rows": sum(sheet_counts.values()),
            "priceable_rows": sum(
                value
                for key, value in sheet_counts.items()
                if key == "PRICEABLE_LINE_ITEM"
            ),
            "counts": dict(sorted(sheet_counts.items())),
        }

    return {
        "schema_version": "1.0",
        "workbook": parsed.get("filename"),
        "workbook_type": parsed.get("workbook_type"),
        "included_sheet_types": sorted(_BOQ_SHEET_TYPES),
        "included_rows": included_rows,
        "excluded_other_sheet_rows": excluded_rows,
        "priceable_rows": priceable_rows,
        "non_priceable_rows": included_rows - priceable_rows,
        "parser_data_rows": parser_data_rows,
        "parser_data_priceable_rows": parser_data_priceable_rows,
        "parser_data_non_priceable_rows": parser_data_non_priceable_rows,
        "parser_data_classification_counts": {
            key: parser_data_classes.get(key, 0) for key in ROW_CLASSES
        },
        "counts": {key: counts.get(key, 0) for key in ROW_CLASSES},
        "reason_counts": dict(sorted(reason_counts.items())),
        "parser_row_kind_counts": dict(sorted(parser_kinds.items())),
        "by_sheet": by_sheet,
        "samples": {key: samples.get(key, []) for key in ROW_CLASSES},
    }


def markdown_report(report: Mapping[str, Any]) -> str:
    """Render a compact, human-auditable classification report."""

    counts = report.get("counts") or {}
    lines = [
        "# BOQ row classification audit",
        "",
        f"- Workbook: `{report.get('workbook')}`",
        f"- Workbook type: `{report.get('workbook_type')}`",
        f"- Included parsed BOQ/PANEL rows: **{report.get('included_rows', 0)}**",
        f"- Priceable line items: **{report.get('priceable_rows', 0)}**",
        f"- Non-priceable/uncertain rows: **{report.get('non_priceable_rows', 0)}**",
        f"- Parser `data` rows: **{report.get('parser_data_rows', 0)}** "
        f"(priceable {report.get('parser_data_priceable_rows', 0)}, "
        f"non-priceable {report.get('parser_data_non_priceable_rows', 0)})",
        f"- Rows on excluded `OTHER` sheets: **{report.get('excluded_other_sheet_rows', 0)}**",
        "",
        "## Classification counts",
        "",
        "| Class | Rows |",
        "| --- | ---: |",
    ]
    lines.extend(f"| `{key}` | {counts.get(key, 0)} |" for key in ROW_CLASSES)
    lines.extend(["", "## By sheet", "", "| Sheet | Type | Rows | Priceable | Breakdown |", "| --- | --- | ---: | ---: | --- |"])
    for sheet, data in (report.get("by_sheet") or {}).items():
        breakdown = ", ".join(
            f"{key}={value}" for key, value in sorted((data.get("counts") or {}).items())
        )
        lines.append(
            f"| {sheet} | {data.get('sheet_type')} | {data.get('rows', 0)} | "
            f"{data.get('priceable_rows', 0)} | {breakdown} |"
        )
    lines.extend(["", "## Sample rows", ""])
    for classification in ROW_CLASSES:
        samples = report.get("samples", {}).get(classification) or []
        if not samples:
            continue
        lines.extend([f"### `{classification}`", "", "| Sheet | Row | Description | Unit | Qty | Reason |", "| --- | ---: | --- | --- | ---: | --- |"])
        for sample in samples:
            description = str(sample.get("description") or "").replace("|", "\\|")
            lines.append(
                f"| {sample.get('sheet')} | {sample.get('row_no')} | {description} | "
                f"{sample.get('unit') or ''} | {sample.get('quantity') if sample.get('quantity') is not None else ''} | "
                f"{sample.get('reason')} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Interpretation",
            "",
            "The classification is an audit view over parsed rows; it does not "
            "remove rows from the leakage-safe benchmark. `PRICEABLE_LINE_ITEM` "
            "means the row has an item/work signal plus an operational unit, "
            "quantity or price. Zero or missing quantities are retained and "
            "flagged in the reason so parser/data-quality gaps remain visible.",
            "",
        ]
    )
    return "\n".join(lines)
