from __future__ import annotations

from pathlib import Path

from app import db
from app.pricing import run_pricing
from benchmarks.row_audit import classify_row


def _row(
    description: str | None,
    *,
    unit: str | None = None,
    quantity: float | None = None,
    material_price: float | None = None,
    row_kind: str = "data",
    raw_cells: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "row_kind": row_kind,
        "raw_cells": raw_cells or {},
        "fields": {
            "description": description,
            "unit": unit,
            "quantity": quantity,
            "material_price": material_price,
            "labor_price": None,
            "list_price": None,
            "vat_price": None,
            "total": None,
        },
    }


def test_row_audit_distinguishes_priceable_item_from_heading() -> None:
    item = classify_row(
        _row(
            "Cáp CXV 4C_50mm2",
            unit="m",
            quantity=25,
        ),
        sheet_type="HISTORICAL_BOQ",
    )
    heading = classify_row(
        _row("TỦ ĐIỆN PHÂN PHỐI CHÍNH MSB", row_kind="section"),
        sheet_type="HISTORICAL_BOQ",
    )

    assert item["classification"] == "PRICEABLE_LINE_ITEM"
    assert item["priceable"] is True
    assert heading["classification"] in {"SECTION", "SUBSECTION"}
    assert heading["priceable"] is False


def test_row_audit_reads_total_label_from_unmapped_raw_cell() -> None:
    result = classify_row(
        _row(
            None,
            row_kind="section",
            raw_cells={"1": "TỔNG CỘNG A+B (trước VAT)", "8": 123456},
        ),
        sheet_type="HISTORICAL_BOQ",
    )

    assert result["classification"] == "TOTAL"
    assert result["priceable"] is False


def test_row_audit_does_not_treat_single_cell_subsection_as_header() -> None:
    result = classify_row(
        _row(
            "Vật tư nhân công hoàn thiện",
            row_kind="section",
            raw_cells={"2": "Vật tư nhân công hoàn thiện"},
        ),
        sheet_type="PANEL_BOM",
        previous_class="SECTION",
    )

    assert result["classification"] == "SUBSECTION"
    assert result["priceable"] is False


def test_row_audit_keeps_missing_quantity_visible_as_priceable() -> None:
    result = classify_row(
        _row(
            "Cáp Cu/PVC 1C-1.5mm²",
            unit="m",
            material_price=12000,
        ),
        sheet_type="HISTORICAL_BOQ",
    )

    assert result["classification"] == "PRICEABLE_LINE_ITEM"
    assert result["reason"] == "item_signal_unit_or_price_but_quantity_missing"


def test_pricing_ignores_explicit_structural_rows(isolated_db: Path) -> None:
    with db.db_session() as conn:
        project = conn.execute(
            """
            INSERT INTO projects(project_name, metadata_json, created_at)
            VALUES ('classification-test', '{}', datetime('now'))
            """
        )
        project_id = int(project.lastrowid)
        conn.execute(
            """
            INSERT INTO boq_items(
                project_id, raw_description, normalized_description, unit,
                quantity, line_class, status_reason, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', datetime('now'))
            """,
            (
                project_id,
                "TỦ ĐIỆN PHÂN PHỐI CHÍNH",
                "tu dien phan phoi chinh",
                None,
                None,
                "SECTION",
                "structural_heading",
            ),
        )
        conn.execute(
            """
            INSERT INTO boq_items(
                project_id, raw_description, normalized_description, unit,
                quantity, line_class, status_reason, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', datetime('now'))
            """,
            (
                project_id,
                "Cáp Cu/PVC 1C-1.5mm²",
                "cap cu/pvc 1c-1.5mm2",
                "m",
                10,
                "PRICEABLE_LINE_ITEM",
                "item_signal_with_operational_fields",
            ),
        )

    result = run_pricing(project_id, force=True)

    assert result["metrics"]["total_items"] == 2
    assert result["metrics"]["priceable_items"] == 1
    assert result["metrics"]["non_priceable_items"] == 1
    with db.db_session() as conn:
        ignored = conn.execute(
            "SELECT status, status_reason FROM boq_items WHERE line_class='SECTION'"
        ).fetchone()
        assert ignored["status"] == "IGNORED"
        assert ignored["status_reason"].startswith("row_class:section")
