from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import load_workbook
import xlrd

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


def map_columns(snapshot: SheetSnapshot, header_row: int | None, sheet_type: str) -> dict[str, int]:
    if header_row is None:
        return {}
    idx = header_row - 1
    # Combine up to three header rows; merged cells often put semantics above
    # the leaf header.
    combined: list[str] = []
    width = min(snapshot.max_col, 80)
    for col in range(width):
        parts = []
        for row_idx in range(idx, min(idx + 3, len(snapshot.rows))):
            value = snapshot.rows[row_idx][col] if col < len(snapshot.rows[row_idx]) else None
            if value not in (None, ""):
                parts.append(str(value))
        combined.append(normalize_text(" ".join(parts)))
    mapping: dict[str, int] = {}
    for field, synonyms in HEADER_SYNONYMS.items():
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


def extract_rows(snapshot: SheetSnapshot, sheet_type: str, header_row: int | None, mapping: dict[str, int]) -> list[dict[str, Any]]:
    if header_row is None:
        return []
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


def parse_workbook(path: Path) -> dict[str, Any]:
    snapshots = load_workbook_snapshots(path)
    workbook_type = detect_document_type(path.name, snapshots)
    parsed_sheets: list[ParsedSheet] = []
    total_data = 0
    warnings: list[str] = []
    for snapshot in snapshots:
        sheet_type = detect_sheet_type(snapshot, workbook_type)
        header_row = find_header_row(snapshot, sheet_type)
        mapping = map_columns(snapshot, header_row, sheet_type)
        rows = extract_rows(snapshot, sheet_type, header_row, mapping)
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
