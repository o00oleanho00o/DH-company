from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from openpyxl import load_workbook
import xlrd

from .ai import ai_provider, safe_map_columns_sync
from .normalize import normalize_text, normalize_unit, parse_number


PARSING_VERSION = "1.0"


@dataclass
class SheetSnapshot:
    name: str
    index: int
    rows: list[list[Any]]
    formulas: list[list[Any]]
    max_row: int
    max_col: int
    merged_ranges: list[str] = field(default_factory=list)


@dataclass
class ParsedSheet:
    snapshot: SheetSnapshot
    detected_type: str
    header_row: int | None
    mapping: dict[str, int]
    rows: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    # Context extracted from the title/header area.  Supplier workbooks often
    # keep construction, voltage and price-tier semantics outside the leaf
    # table columns; retaining it here prevents ingestion from losing those
    # facts while keeping the parser generic.
    metadata: dict[str, Any] = field(default_factory=dict)


HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "description": (
        "mo ta", "mô tả", "nội dung", "noi dung", "diễn giải", "dien giai",
        "hạng mục", "hang muc", "ten san pham", "tên sản phẩm",
        "work description", "description", "item", "linh phụ kiện",
    ),
    "code": ("mã hiệu", "ma hieu", "mã sản phẩm", "ma san pham", "code", "sku", "model"),
    "unit": ("đơn vị", "don vi", "đvt", "dvt", "unit", "đơn / vị"),
    "quantity": ("khối lượng", "khoi luong", "số lượng", "so luong", "quantity", "qty", "khối / lượng"),
    "material_price": ("đơn giá vt", "don gia vt", "vật tư", "vat tu", "material", "material price", "giá vật liệu"),
    "labor_price": ("đơn giá nhân công", "don gia nhan cong", "nhân công", "nhan cong", "labor", "labor price", "nhân công lắp đặt"),
    "list_price": ("đơn giá", "don gia", "list price", "giá niêm yết", "chưa vat"),
    "vat_price": ("có vat", "co vat", "vat price"),
    "total": ("thành tiền", "thanh tien", "total", "amount"),
    "brand": ("hãng sản xuất", "hang san xuat", "hãng/model", "hang/model", "maker", "brand", "nhãn hiệu"),
    "origin": ("xuất xứ", "xuat xu", "origin"),
    "discount": ("chiết khấu", "chiet khau", "discount"),
    "note": ("ghi chú", "ghi chu", "remark", "note"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_effective_date(filename: str) -> tuple[str | None, bool]:
    patterns = [
        (r"(?<!\d)(\d{2})[-/.](\d{2})[-/.](\d{4})(?!\d)", "%d-%m-%Y"),
        (r"(?<!\d)(\d{4})[-/.](\d{2})[-/.](\d{2})(?!\d)", "%Y-%m-%d"),
    ]
    for pattern, fmt in patterns:
        match = re.search(pattern, filename)
        if match:
            raw = "-".join(match.groups())
            try:
                return datetime.strptime(raw, fmt).date().isoformat(), True
            except ValueError:
                continue
    return None, False


def _safe_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def load_workbook_snapshots(path: Path) -> list[SheetSnapshot]:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        wb_values = load_workbook(path, read_only=False, data_only=True)
        wb_formulas = load_workbook(path, read_only=False, data_only=False)
        snapshots: list[SheetSnapshot] = []
        for idx, ws in enumerate(wb_values.worksheets):
            formula_ws = wb_formulas[ws.title]
            rows: list[list[Any]] = []
            formulas: list[list[Any]] = []
            for r in range(1, ws.max_row + 1):
                rows.append([_safe_cell(ws.cell(r, c).value) for c in range(1, ws.max_column + 1)])
                formulas.append([_safe_cell(formula_ws.cell(r, c).value) for c in range(1, ws.max_column + 1)])
            snapshots.append(
                SheetSnapshot(
                    name=ws.title,
                    index=idx,
                    rows=rows,
                    formulas=formulas,
                    max_row=ws.max_row,
                    max_col=ws.max_column,
                    merged_ranges=[str(rng) for rng in ws.merged_cells.ranges],
                )
            )
        return snapshots
    if suffix == ".xls":
        book_values = xlrd.open_workbook(str(path), formatting_info=False)
        snapshots = []
        for idx, sh in enumerate(book_values.sheets()):
            rows = [[_safe_cell(sh.cell_value(r, c)) for c in range(sh.ncols)] for r in range(sh.nrows)]
            snapshots.append(
                SheetSnapshot(
                    name=sh.name,
                    index=idx,
                    rows=rows,
                    formulas=[list(row) for row in rows],
                    max_row=sh.nrows,
                    max_col=sh.ncols,
                    merged_ranges=[],
                )
            )
        return snapshots
    raise ValueError(f"Unsupported workbook extension: {path.suffix}")


def _row_text(row: Iterable[Any]) -> str:
    return " ".join(normalize_text(v) for v in row if v not in (None, ""))


def detect_document_type(filename: str, snapshots: list[SheetSnapshot]) -> str:
    name = normalize_text(filename)
    preview_text = " ".join(
        f"{normalize_text(s.name)} {_row_text(row)}"
        for s in snapshots[:20]
        for row in s.rows[:14]
    )
    if any(x in name for x in ("nhan cong", "nhancong")):
        return "LABOR"
    if "bang gia cap" in name or "price" in name or "bang gia" in name:
        return "SUPPLIER_PRICE"
    if any(x in name for x in ("boq", "bao gia", "du toan", "quotation")):
        return "HISTORICAL_BOQ"
    if "mo ta thiet bi" in preview_text and "so luong" in preview_text:
        return "PANEL_BOM"
    if "hang muc linh phu kien" in preview_text:
        return "LABOR"
    if (
        ("noi dung cong viec" in preview_text or "dien giai" in preview_text or "mo ta desciption" in preview_text)
        and ("khoi luong" in preview_text or "so luong" in preview_text or "don gia" in preview_text)
    ):
        return "HISTORICAL_BOQ"
    return "UNKNOWN"


def detect_sheet_type(snapshot: SheetSnapshot, workbook_type: str) -> str:
    # Use the title/header zone only. Looking too far into data rows causes
    # historical BOQs containing the words "nhân công" to be misclassified as
    # labor master sheets.
    text = normalize_text(snapshot.name) + " " + " ".join(_row_text(r) for r in snapshot.rows[:14])
    if "tu dien chi tiet" in text or "mo ta thiet bi" in text or "ma san pham" in text:
        return "PANEL_BOM"
    if "ten san pham" in text and ("chiet khau" in text or "don gia" in text):
        return "SUPPLIER_PRICE"
    if "hang muc linh phu kien" in text:
        return "LABOR"
    if any(x in text for x in ("noi dung cong viec", "dien giai", "mo ta desciption")):
        if any(x in text for x in ("don gia vt", "don gia nhan cong", "khoi luong", "so luong", "don gia")):
            return "HISTORICAL_BOQ" if workbook_type in {"HISTORICAL_BOQ", "LABOR", "UNKNOWN"} else "BOQ"
        return "BOQ"
    if workbook_type == "LABOR":
        # Fallback for sheets whose title/header is sparse but still belongs to
        # the labor master workbook.
        return "LABOR"
    if workbook_type == "SUPPLIER_PRICE":
        return "SUPPLIER_PRICE"
    return "OTHER"


def find_header_row(snapshot: SheetSnapshot, sheet_type: str) -> int | None:
    best_row: int | None = None
    best_score = 0
    scan = min(len(snapshot.rows), 60)
    for idx in range(scan):
        row = snapshot.rows[idx]
        text = _row_text(row)
        if not text:
            continue
        score = 0
        for synonyms in HEADER_SYNONYMS.values():
            if any(normalize_text(s) in text for s in synonyms):
                score += 1
        # Strong signals for common sheet families.
        if "stt" in text or re.search(r"\btt\b", text):
            score += 1
        if sheet_type == "SUPPLIER_PRICE" and "ten san pham" in text:
            score += 3
        if sheet_type in {"BOQ", "HISTORICAL_BOQ"} and ("noi dung cong viec" in text or "dien giai" in text):
            score += 3
        if sheet_type == "LABOR" and "hang muc" in text:
            score += 3
        if score > best_score:
            best_score = score
            best_row = idx + 1
    return best_row if best_score >= 2 else None


def _combined_header_texts(snapshot: SheetSnapshot, header_row: int) -> list[str]:
    """Normalized header text per column, merging up to 3 rows below it.

    Merged cells often put semantics above the leaf header, so a column's
    real label is frequently split across the header row and the one or two
    rows beneath it. Shared by :func:`map_columns` and the optional AI
    column-mapping fallback so both see identical column text.
    """

    idx = header_row - 1
    combined: list[str] = []
    width = min(snapshot.max_col, 80)
    for col in range(width):
        parts = []
        for row_idx in range(idx, min(idx + 3, len(snapshot.rows))):
            value = snapshot.rows[row_idx][col] if col < len(snapshot.rows[row_idx]) else None
            if value not in (None, ""):
                parts.append(str(value))
        combined.append(normalize_text(" ".join(parts)))
    return combined


def _match_header_synonyms(
    combined: list[str],
    extra_synonyms: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, int]:
    """Column index per canonical field, matched purely from header text.

    Deliberately excludes the sheet-type positional fallbacks applied later
    in :func:`map_columns` — this is the honest signal of what the header
    text actually recognized, used to decide whether the optional AI
    column-mapping fallback should even run (see
    :func:`_missing_required_fields`).

    ``extra_synonyms`` layers in header text an AI call previously confirmed
    for this workbook's sheet type (loaded by :mod:`app.ingest` from the
    ``learned_header_synonyms`` table) on top of the static
    ``HEADER_SYNONYMS`` — a header seen and confirmed once is recognized
    instantly next time, with no AI call needed.
    """

    synonyms_by_field: dict[str, tuple[str, ...]] = dict(HEADER_SYNONYMS)
    if extra_synonyms:
        for field, values in extra_synonyms.items():
            synonyms_by_field[field] = synonyms_by_field.get(field, ()) + tuple(values)
    mapping: dict[str, int] = {}
    for field, synonyms in synonyms_by_field.items():
        best_col, best_score = None, 0
        for col, text in enumerate(combined):
            if not text:
                continue
            score = 0
            for synonym in synonyms:
                syn = normalize_text(synonym)
                if text == syn:
                    score = max(score, 10)
                elif syn in text:
                    score = max(score, 5)
            if score > best_score:
                best_col, best_score = col, score
        if best_col is not None:
            mapping[field] = best_col
    return mapping


def map_columns(
    snapshot: SheetSnapshot,
    header_row: int | None,
    sheet_type: str,
    extra_synonyms: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, int]:
    if header_row is None:
        return {}
    idx = header_row - 1
    width = min(snapshot.max_col, 80)
    combined = _combined_header_texts(snapshot, header_row)
    mapping = _match_header_synonyms(combined, extra_synonyms)

    # Context-specific fallbacks based on observed workbook families.
    if sheet_type == "SUPPLIER_PRICE":
        mapping.setdefault("description", 0)
        mapping.setdefault("list_price", 1)
        # Generic "Có VAT" matching may select a discount-tier VAT column.
        # The canonical base VAT price is always the third column in these
        # supplier TONG tables. Detail sheets expose the same pair near the
        # "Đơn giá" group, so retain the heuristic mapping there.
        if snapshot.name.strip().upper() == "TONG":
            mapping["vat_price"] = 2
    elif sheet_type in {"BOQ", "HISTORICAL_BOQ"}:
        mapping.setdefault("description", 1)
        # Material/labor often share a parent "Đơn giá" cell. Detect the leaf
        # cells in the one or two following header rows explicitly.
        leaf_rows = snapshot.rows[idx:min(idx + 3, len(snapshot.rows))]
        for leaf_row in leaf_rows[1:]:
            for col, value in enumerate(leaf_row[:width]):
                leaf = normalize_text(value)
                if any(token in leaf for token in ("vat tu", "vat lieu", "material")):
                    mapping.setdefault("material_price", col)
                if any(token in leaf for token in ("nhan cong", "labor")):
                    mapping.setdefault("labor_price", col)
        # Historical BOQ sheets commonly use E/F/G or F/G/H depending on
        # whether a code column exists. Only use these fallbacks after scanning
        # the actual header text.
        if "unit" not in mapping:
            mapping["unit"] = 5 if snapshot.max_col > 6 else 2
        if "quantity" not in mapping:
            mapping["quantity"] = 6 if snapshot.max_col > 7 else 3
    elif sheet_type == "LABOR":
        mapping.setdefault("description", 1)
        mapping.setdefault("unit", 4)
        # H is labor price in the DH master (G=material, H=labor).
        if "code" in mapping:
            code_text = combined[mapping["code"]]
            if (
                "hang/model" in code_text
                or "hang san xuat" in code_text
                or ("model" in code_text and "ma" not in code_text)
            ):
                mapping.pop("code", None)
        mapping["labor_price"] = 7
    return mapping


# Fields a sheet type needs at least one real (synonym-matched) hit for, to
# ever classify a row as "data" in ``classify_row``. Each inner tuple is an
# OR-group: the group is satisfied if any one of its fields was matched.
# Deliberately checked against ``_match_header_synonyms`` output, not the
# final ``map_columns`` result — SUPPLIER_PRICE/LABOR/BOQ all carry hardcoded
# positional fallbacks (e.g. ``mapping.setdefault("description", 0)``) that
# would otherwise mask a genuine header-matching miss.
_REQUIRED_FIELD_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "SUPPLIER_PRICE": (("description",), ("list_price",)),
    "LABOR": (("description",), ("unit", "labor_price")),
}
_DEFAULT_REQUIRED_FIELD_GROUPS: tuple[tuple[str, ...], ...] = (
    ("description",),
    ("quantity", "material_price", "labor_price", "unit"),
)


def _missing_required_fields(sheet_type: str, synonym_mapping: dict[str, int]) -> list[str]:
    groups = _REQUIRED_FIELD_GROUPS.get(sheet_type, _DEFAULT_REQUIRED_FIELD_GROUPS)
    missing: list[str] = []
    for group in groups:
        if not any(field in synonym_mapping for field in group):
            missing.extend(field for field in group if field not in missing)
    return missing


def _sheet_has_data_rows(snapshot: SheetSnapshot, header_row: int) -> bool:
    """Whether any non-empty cell exists below the header row.

    Guards the AI column-mapping fallback against genuinely empty sheets
    (cover pages, section dividers) where there is nothing to map.
    """

    for row in snapshot.rows[header_row : header_row + 20]:
        if any(value not in (None, "") for value in row):
            return True
    return False


def _header_number(value: Any) -> float | None:
    """Return a normalized percentage header, or ``None``.

    Supplier ``TONG`` sheets encode discount headers as either numeric
    fractions (``0.10``) or human-readable percentages (``10%``).  Treating
    arbitrary numeric cells as discounts would misclassify ordinary price
    columns, so values outside the practical 0..100% range are ignored.
    """

    number = parse_number(value)
    if number is None:
        return None
    if number > 1.0:
        number /= 100.0
    if 0.0 <= number <= 1.0:
        return round(number, 8)
    return None


def detect_price_columns(
    snapshot: SheetSnapshot,
    header_row: int | None,
    sheet_type: str,
) -> dict[str, Any]:
    """Infer base and tiered price columns from multi-row headers.

    The result uses zero-based column indexes (matching ``mapping``) and is
    deliberately descriptive rather than selecting a business policy.  For
    example, a ``TONG`` sheet exposes the base ex-VAT/VAT pair plus all
    discount tiers; the pricing policy can later choose one without losing
    observations.
    """

    if header_row is None or sheet_type != "SUPPLIER_PRICE":
        return {}
    idx = header_row - 1
    rows = snapshot.rows
    if idx < 0 or idx >= len(rows):
        return {}
    width = min(snapshot.max_col, 120)
    base: dict[str, int] = {}
    discount_tiers: list[dict[str, Any]] = []

    # Most supplier sheets use the row immediately below the first header row
    # for tax labels.  Scan a small window so shifted/merged headers continue
    # to work.
    tax_row_indexes = range(idx, min(idx + 4, len(rows)))
    for col in range(width):
        labels = " ".join(
            normalize_text(rows[r][col])
            for r in tax_row_indexes
            if col < len(rows[r]) and rows[r][col] not in (None, "")
        )
        if not labels:
            continue
        is_ex = (
            "chua vat" in labels
            or "ex vat" in labels
            or "without vat" in labels
        )
        is_inc = (
            "co vat" in labels
            or "inc vat" in labels
            or "vat included" in labels
        )
        if is_ex and "ex_vat" not in base:
            base["ex_vat"] = col
        if is_inc and "inc_vat" not in base:
            base["inc_vat"] = col

    # A discount marker is normally in the row immediately beneath the
    # ``Chiết khấu`` group heading.  Search the first three header rows and
    # pair the marker column with its adjacent VAT column.
    for discount_row in range(idx, min(idx + 3, len(rows))):
        for col in range(width):
            value = rows[discount_row][col] if col < len(rows[discount_row]) else None
            rate = _header_number(value)
            if rate is None:
                continue
            # Avoid interpreting a quantity/price in a normal leaf header as a
            # tier: the same column must have an ex-VAT/VAT tax label nearby.
            pair: dict[str, Any] = {"discount_rate": rate}
            for candidate_col in (col, col + 1):
                if candidate_col >= width:
                    continue
                labels = " ".join(
                    normalize_text(rows[r][candidate_col])
                    for r in range(idx, min(idx + 4, len(rows)))
                    if candidate_col < len(rows[r]) and rows[r][candidate_col] not in (None, "")
                )
                if "chua vat" in labels or "ex vat" in labels:
                    pair["ex_vat"] = candidate_col
                if "co vat" in labels or "inc vat" in labels or "vat included" in labels:
                    pair["inc_vat"] = candidate_col
            if "ex_vat" in pair or "inc_vat" in pair:
                # Numeric markers can appear in both a merged cell and a leaf
                # cell.  De-duplicate by rate/column pair.
                signature = (
                    pair["discount_rate"],
                    pair.get("ex_vat"),
                    pair.get("inc_vat"),
                )
                if not any(
                    (
                        tier["discount_rate"],
                        tier.get("ex_vat"),
                        tier.get("inc_vat"),
                    )
                    == signature
                    for tier in discount_tiers
                ):
                    discount_tiers.append(pair)
    discount_tiers.sort(key=lambda tier: tier["discount_rate"])
    result: dict[str, Any] = {}
    if base:
        result["base"] = base
    if discount_tiers:
        result["discount_tiers"] = discount_tiers
    return result


def _context_value(rows: list[list[Any]], labels: tuple[str, ...]) -> str | None:
    """Find a value following a key label in title/context rows."""

    normalized_labels = tuple(normalize_text(label) for label in labels)
    for row in rows:
        cells = [str(value).strip() for value in row if value not in (None, "")]
        for index, cell in enumerate(cells):
            normalized = normalize_text(cell)
            if any(label in normalized for label in normalized_labels):
                # ``Cấp điện áp: 0.6/1kV`` and ``Cấp điện áp:`` + next cell
                # are both common in the source files.
                if ":" in cell:
                    value = cell.split(":", 1)[1].strip()
                    if value:
                        return value
                if index + 1 < len(cells):
                    return cells[index + 1]
    return None


def detect_sheet_metadata(
    snapshot: SheetSnapshot,
    header_row: int | None,
    sheet_type: str,
) -> dict[str, Any]:
    """Extract stable sheet context and price-column semantics."""

    context_rows = (
        snapshot.rows[: max(0, (header_row or 1) - 1)]
        if header_row is not None
        else snapshot.rows[:20]
    )
    metadata: dict[str, Any] = {
        "price_columns": detect_price_columns(snapshot, header_row, sheet_type),
    }
    standard = _context_value(
        context_rows,
        ("tieu chuan ap dung", "standard", "specification"),
    )
    construction = _context_value(
        context_rows,
        ("quy cach san pham", "construction", "cable construction"),
    )
    voltage = _context_value(
        context_rows,
        ("cap dien ap", "dien ap", "voltage"),
    )
    if standard:
        metadata["standard"] = standard
    if construction:
        metadata["construction"] = construction
    if voltage:
        metadata["voltage"] = voltage
    # Keep the first non-empty title as a human-readable family context.  Do
    # not treat generic company/banner text as a product name.
    for row in context_rows:
        values = [str(value).strip() for value in row if value not in (None, "")]
        if not values:
            continue
        candidate = " ".join(values)
        normalized = normalize_text(candidate)
        if any(
            token in normalized
            for token in ("bang gia", "cap ", "day ", "cable", "san pham")
        ):
            metadata.setdefault("title", candidate)
            break
    return metadata


def _cell_at(row: list[Any], col: int | None) -> Any:
    if col is None or col < 0 or col >= len(row):
        return None
    return row[col]


def classify_row(row: list[Any], fields: dict[str, Any], sheet_type: str) -> str:
    values = [v for v in row if v not in (None, "")]
    if not values:
        return "empty"
    desc = normalize_text(fields.get("description"))
    # Totals and notes should never become products/BOQ lines.
    if any(token in desc for token in ("tong cong", "cộng", "subtotal", "total", "ghi chu", "note")):
        return "subtotal" if any(token in desc for token in ("tong", "cong", "subtotal", "total")) else "note"
    if sheet_type == "SUPPLIER_PRICE":
        return "data" if desc and parse_number(fields.get("list_price")) is not None else "section"
    if sheet_type == "LABOR":
        if desc and (fields.get("unit") or parse_number(fields.get("labor_price")) is not None):
            return "data"
        return "section"
    # A BOQ line should have description plus quantity or an existing price;
    # keep description-only headings as section rows.
    if desc and (
        parse_number(fields.get("quantity")) is not None
        or parse_number(fields.get("material_price")) is not None
        or parse_number(fields.get("labor_price")) is not None
        or fields.get("unit")
    ):
        return "data"
    return "section"


def extract_rows(
    snapshot: SheetSnapshot,
    sheet_type: str,
    header_row: int | None,
    mapping: dict[str, int],
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if header_row is None:
        return []
    metadata = metadata or {}
    price_columns = metadata.get("price_columns") or {}
    base_price_columns = price_columns.get("base") or {}
    discount_tiers = price_columns.get("discount_tiers") or []
    result: list[dict[str, Any]] = []
    for row_no in range(header_row + 1, len(snapshot.rows) + 1):
        row = snapshot.rows[row_no - 1]
        formula_row = snapshot.formulas[row_no - 1] if row_no - 1 < len(snapshot.formulas) else row
        raw_cells = {str(i + 1): value for i, value in enumerate(row) if value not in (None, "")}
        fields = {
            "description": _cell_at(row, mapping.get("description")),
            "code": _cell_at(row, mapping.get("code")),
            "unit": normalize_unit(_cell_at(row, mapping.get("unit"))),
            "quantity": parse_number(_cell_at(row, mapping.get("quantity"))),
            "material_price": parse_number(_cell_at(row, mapping.get("material_price"))),
            "labor_price": parse_number(_cell_at(row, mapping.get("labor_price"))),
            "list_price": parse_number(_cell_at(row, mapping.get("list_price"))),
            "vat_price": parse_number(_cell_at(row, mapping.get("vat_price"))),
            "total": parse_number(_cell_at(row, mapping.get("total"))),
            "brand": _cell_at(row, mapping.get("brand")),
            "origin": _cell_at(row, mapping.get("origin")),
            "discount": parse_number(_cell_at(row, mapping.get("discount"))),
            "note": _cell_at(row, mapping.get("note")),
        }
        # ``TONG`` discount columns are a matrix of observations, not a
        # single scalar discount.  Preserve every ex-VAT/VAT amount and leave
        # selection to an explicit pricing policy.  The legacy ``discount``
        # mapping points at the first tier's amount; exposing that as a scalar
        # would silently mislabel a price as a percentage.
        price_observations: list[dict[str, Any]] = []
        base_ex_col = base_price_columns.get("ex_vat", mapping.get("list_price"))
        base_inc_col = base_price_columns.get("inc_vat", mapping.get("vat_price"))
        base_ex = parse_number(_cell_at(row, base_ex_col))
        base_inc = parse_number(_cell_at(row, base_inc_col))
        if base_ex is not None:
            price_observations.append(
                {
                    "price_type": "supplier_list",
                    "tax_mode": "ex_vat",
                    "amount": base_ex,
                    "discount_rate": 0.0,
                    "column": base_ex_col + 1 if base_ex_col is not None else None,
                }
            )
        if base_inc is not None:
            price_observations.append(
                {
                    "price_type": "supplier_list",
                    "tax_mode": "inc_vat",
                    "amount": base_inc,
                    "discount_rate": 0.0,
                    "column": base_inc_col + 1 if base_inc_col is not None else None,
                }
            )
        for tier in discount_tiers:
            rate = tier.get("discount_rate")
            ex_col = tier.get("ex_vat")
            inc_col = tier.get("inc_vat")
            ex_amount = parse_number(_cell_at(row, ex_col))
            inc_amount = parse_number(_cell_at(row, inc_col))
            if ex_amount is not None:
                price_observations.append(
                    {
                        "price_type": "supplier_discounted",
                        "tax_mode": "ex_vat",
                        "amount": ex_amount,
                        "discount_rate": rate,
                        "column": ex_col + 1 if ex_col is not None else None,
                    }
                )
            if inc_amount is not None:
                price_observations.append(
                    {
                        "price_type": "supplier_discounted",
                        "tax_mode": "inc_vat",
                        "amount": inc_amount,
                        "discount_rate": rate,
                        "column": inc_col + 1 if inc_col is not None else None,
                    }
                )
        if price_observations:
            # Explicitly represent an unknown/unspecified tier as ``None``.
            # This is safer than carrying the first discounted amount from the
            # old ``discount`` column mapping.
            fields["discount"] = None
        fields["price_observations"] = price_observations
        # For price tables the list price is usually mapped from "Đơn giá";
        # for BOQs material_price may be inferred from a generic price column.
        if sheet_type in {"BOQ", "HISTORICAL_BOQ"} and fields["material_price"] is None:
            generic = fields["list_price"]
            if generic is not None:
                fields["material_price"] = generic
        if sheet_type == "LABOR" and fields["labor_price"] is None:
            # Some master sheets put labor in H, but occasionally G is the only
            # populated price column.
            fields["labor_price"] = fields["list_price"]
        row_kind = classify_row(row, fields, sheet_type)
        warnings: list[str] = []
        if row_kind == "data":
            if not fields["description"]:
                warnings.append("MISSING_DESCRIPTION")
            if fields["unit"] == "":
                warnings.append("MISSING_UNIT")
            if sheet_type in {"BOQ", "HISTORICAL_BOQ"} and fields["quantity"] is None:
                warnings.append("MISSING_QUANTITY")
        result.append(
            {
                "row_no": row_no,
                "raw_cells": raw_cells,
                "formula_cells": {str(i + 1): value for i, value in enumerate(formula_row) if value not in (None, "")},
                "fields": fields,
                "row_kind": row_kind,
                "warnings": warnings,
            }
        )
    return result


def parse_workbook(
    path: Path,
    *,
    learned_synonyms: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
    on_ai_column_mapped: Callable[[str, str, str], None] | None = None,
) -> dict[str, Any]:
    """Parse a workbook into per-sheet header mapping + rows.

    Pure and DB-free by default (identical to before these two keyword-only
    parameters existed) — everything below is skipped unless a caller
    explicitly supplies one:

    * ``learned_synonyms``: ``{sheet_type: {field: (header_text, ...)}}``,
      layered on top of the static ``HEADER_SYNONYMS`` before deciding
      whether a sheet needs the AI fallback at all. :mod:`app.ingest` loads
      this from the ``learned_header_synonyms`` table so a header text an
      earlier AI call already confirmed is recognized instantly next time.
    * ``on_ai_column_mapped(sheet_type, field, header_text)``: called once
      per field the AI fallback actually resolved, so the caller can persist
      it as a new learned synonym. This module never touches the database
      itself — :mod:`app.ingest` owns that.
    """

    snapshots = load_workbook_snapshots(path)
    workbook_type = detect_document_type(path.name, snapshots)
    parsed_sheets: list[ParsedSheet] = []
    total_data = 0
    warnings: list[str] = []
    # One fresh AI call budget per imported file, not per sheet — a workbook
    # can hold dozens of sheets, and this keeps a single import's worst-case
    # AI cost bounded by settings.llm_max_calls regardless of sheet count.
    if ai_provider.configured:
        ai_provider.reset_budget()
    for snapshot in snapshots:
        sheet_type = detect_sheet_type(snapshot, workbook_type)
        header_row = find_header_row(snapshot, sheet_type)
        extra_synonyms = (learned_synonyms or {}).get(sheet_type)
        mapping = map_columns(snapshot, header_row, sheet_type, extra_synonyms)
        if header_row is not None and ai_provider.configured:
            combined = _combined_header_texts(snapshot, header_row)
            synonym_mapping = _match_header_synonyms(combined, extra_synonyms)
            missing_fields = _missing_required_fields(sheet_type, synonym_mapping)
            if missing_fields and _sheet_has_data_rows(snapshot, header_row):
                ai_result = safe_map_columns_sync(
                    combined, sheet_type=sheet_type, missing_fields=missing_fields
                )
                filled = []
                for field, col in ai_result.mapping.items():
                    # Guard against synonym_mapping, not the final `mapping`:
                    # a field present only via a sheet-type positional guess
                    # (e.g. LABOR's unconditional labor_price=7) was never
                    # actually confirmed by header text, so AI may still
                    # correct it. A real HEADER_SYNONYMS hit is never touched.
                    if field not in synonym_mapping:
                        mapping[field] = col
                        filled.append(field)
                        if on_ai_column_mapped is not None and combined[col]:
                            on_ai_column_mapped(sheet_type, field, combined[col])
                if filled:
                    warnings.append(f"{snapshot.name}:AI_COLUMN_MAPPING:{','.join(sorted(filled))}")
        metadata = detect_sheet_metadata(snapshot, header_row, sheet_type)
        rows = extract_rows(snapshot, sheet_type, header_row, mapping, metadata)
        total_data += sum(1 for row in rows if row["row_kind"] == "data")
        if snapshot.max_row and header_row is None and any(_row_text(r) for r in snapshot.rows[:20]):
            warnings.append(f"{snapshot.name}:HEADER_NOT_FOUND")
        parsed_sheets.append(
            ParsedSheet(
                snapshot=snapshot,
                detected_type=sheet_type,
                header_row=header_row,
                mapping=mapping,
                rows=rows,
                warnings=[],
                metadata=metadata,
            )
        )
    effective_date, inferred = infer_effective_date(path.name)
    return {
        "filename": path.name,
        "sha256": sha256_file(path),
        "extension": path.suffix.lower(),
        "workbook_type": workbook_type,
        "effective_date": effective_date,
        "effective_date_inferred": inferred,
        "sheets": parsed_sheets,
        "total_data_rows": total_data,
        "warnings": warnings,
        "parsing_version": PARSING_VERSION,
    }
