from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from app.excel import (
    detect_document_type,
    find_header_row,
    load_workbook_snapshots,
    map_columns,
    parse_workbook,
)

from .conftest import INPUT_DIR, find_input_file


SAMPLE_PREFIXES = (
    "BG. HT",
    "BOQ-",
    "1. Bảng giá CÁP HẠ",
    "2. Bảng giá CÁP TRUNG",
    "Bảng giá nhân công",
    "Báo giá hạ tầng",
)


@pytest.mark.parametrize("prefix", SAMPLE_PREFIXES)
def test_real_sample_workbooks_parse(prefix: str) -> None:
    path = find_input_file(prefix)
    parsed = parse_workbook(path)

    assert parsed["filename"] == path.name
    assert len(parsed["sha256"]) == 64
    assert parsed["extension"] in {".xls", ".xlsx"}
    assert parsed["sheets"], "at least one sheet should be inspected"
    assert parsed["total_data_rows"] > 0, "sample workbook should yield usable rows"

    # Every extracted data row retains raw cells and a human-readable
    # description.  Empty/section/subtotal rows are intentionally allowed.
    data_rows = [
        row
        for sheet in parsed["sheets"]
        for row in sheet.rows
        if row["row_kind"] == "data"
    ]
    assert data_rows
    assert all("raw_cells" in row and "fields" in row for row in data_rows)
    assert any(row["fields"].get("description") for row in data_rows)


def test_real_directory_covers_both_excel_extensions() -> None:
    suffixes = {
        path.suffix.lower()
        for path in INPUT_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in {".xls", ".xlsx"}
    }
    assert {".xls", ".xlsx"} <= suffixes


def test_expected_document_classification_signals() -> None:
    cable = parse_workbook(find_input_file("1. Bảng giá CÁP HẠ"))
    labor = parse_workbook(find_input_file("Bảng giá nhân công"))
    historical = parse_workbook(find_input_file("BOQ-"))

    assert cable["workbook_type"] == "SUPPLIER_PRICE"
    assert labor["workbook_type"] == "LABOR"
    assert historical["workbook_type"] == "HISTORICAL_BOQ"

    assert any(sheet.detected_type == "SUPPLIER_PRICE" for sheet in cable["sheets"])
    assert any(sheet.detected_type == "LABOR" for sheet in labor["sheets"])
    assert any(
        sheet.detected_type in {"HISTORICAL_BOQ", "BOQ", "LABOR", "PANEL_BOM"}
        for sheet in historical["sheets"]
    )


def test_parser_handles_title_rows_reordered_columns_and_formula_cells(tmp_path: Path) -> None:
    """Template-generalization regression using a generated workbook.

    The header is deliberately moved to row 4 and columns are reordered.  No
    production code should rely on a fixed row number or fixed column letter.
    """

    path = tmp_path / "generalized-boq.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "BOQ"
    sheet["A1"] = "CÔNG TY DEMO"
    sheet["A2"] = "Bảng khối lượng - template biến thể"
    sheet.merge_cells("A2:F2")
    # Explicit title rows ensure openpyxl keeps the intended row number even
    # when an empty append would otherwise be elided.
    sheet["A3"] = "Mã hồ sơ: DEMO-001"
    sheet["A4"] = "Ngày lập: 05/09/2026"
    # Keep the header at row 5.  The column labels are deliberately mixed
    # Vietnamese/English and reordered; there is no fixed row/column contract.
    sheet.append(
        [
            "STT",
            "KHỐI LƯỢNG",
            "NỘI DUNG CÔNG VIỆC",
            "ĐVT",
            "Labor Price",
            "Material Price",
        ]
    )
    sheet.append([1, "2,5", "Cáp CXV 3x240", "m", 120000, 4344600])
    sheet["G6"] = "=B5*F5"
    workbook.save(path)

    snapshots = load_workbook_snapshots(path)
    assert len(snapshots) == 1
    assert snapshots[0].formulas[5][6] == "=B5*F5"

    parsed = parse_workbook(path)
    parsed_sheet = parsed["sheets"][0]
    assert parsed_sheet.header_row == 5
    assert parsed_sheet.mapping["description"] == 2
    assert parsed_sheet.mapping["quantity"] == 1
    assert parsed_sheet.mapping["unit"] == 3
    assert parsed_sheet.mapping["material_price"] == 5
    assert parsed_sheet.mapping["labor_price"] == 4

    rows = [row for row in parsed_sheet.rows if row["row_kind"] == "data"]
    assert len(rows) == 1
    assert rows[0]["fields"]["description"] == "Cáp CXV 3x240"
    assert rows[0]["fields"]["quantity"] == pytest.approx(2.5)
    assert rows[0]["fields"]["material_price"] == pytest.approx(4344600)
    assert rows[0]["fields"]["labor_price"] == pytest.approx(120000)


def test_header_detector_and_column_mapper_are_not_fixed_to_first_rows(tmp_path: Path) -> None:
    path = tmp_path / "late-header.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Estimate"
    for row in range(1, 8):
        sheet.cell(row=row, column=1, value=f"Title {row}")
    sheet.cell(row=8, column=1, value="STT")
    sheet.cell(row=8, column=2, value="Mô tả")
    sheet.cell(row=8, column=3, value="Đơn vị")
    sheet.cell(row=8, column=4, value="Số lượng")
    sheet.cell(row=8, column=5, value="Đơn giá VT")
    sheet.cell(row=9, column=1, value=1)
    sheet.cell(row=9, column=2, value="Ống HDPE D195/150")
    sheet.cell(row=9, column=3, value="m")
    sheet.cell(row=9, column=4, value="10")
    sheet.cell(row=9, column=5, value="120.400")
    workbook.save(path)

    snapshot = load_workbook_snapshots(path)[0]
    header_row = find_header_row(snapshot, "BOQ")
    assert header_row == 8
    mapping = map_columns(snapshot, header_row, "BOQ")
    assert mapping["description"] == 1
    assert mapping["unit"] == 2
    assert mapping["quantity"] == 3
    assert mapping["material_price"] == 4
