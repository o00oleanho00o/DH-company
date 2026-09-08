from __future__ import annotations

import io

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.db import db_session, loads
from app.ingest import ingest_workbook
from app.main import app
from app.pricing import catalog_stats
from tests.conftest import find_input_file


def test_quotation_input_is_hidden_from_warehouse_and_catalog_stats(isolated_db) -> None:
    reference = ingest_workbook(find_input_file("Bảng giá nhân công"))
    quotation = ingest_workbook(
        find_input_file("BG. HT"),
        confirmed_type="NEW_BOQ",
        exclude_prices=True,
        allow_duplicate=True,
    )
    quotation_source_id = int(quotation["source_file_id"])

    with db_session() as conn:
        row = conn.execute("SELECT metadata_json FROM source_files WHERE id=?", (quotation_source_id,)).fetchone()
        assert loads(row["metadata_json"], {})["source_role"] == "QUOTATION_INPUT"
        assert conn.execute(
            "SELECT COUNT(*) FROM product_prices WHERE source_file_id=?", (quotation_source_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM labor_rates WHERE source_file_id=?", (quotation_source_id,)
        ).fetchone()[0] == 0

    with TestClient(app) as client:
        listed = client.get("/api/sources").json()
        assert int(reference["source_file_id"]) in [item["id"] for item in listed]
        assert quotation_source_id not in [item["id"] for item in listed]
        assert client.get(f"/api/sources/{quotation_source_id}").status_code == 404

    assert catalog_stats()["source_files"] == 1


def test_generated_export_is_rejected_from_warehouse_import(isolated_db) -> None:
    workbook = Workbook()
    workbook.active.title = "BOQ kết quả"
    workbook.create_sheet("AI Audit")
    content = io.BytesIO()
    workbook.save(content)

    with TestClient(app) as client:
        preview = client.post(
            "/api/import/preview",
            files={"file": ("quotation-99-latest.xlsx", content.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert preview.status_code == 200
        assert preview.json()["import_allowed"] is False

        rejected = client.post(
            "/api/import",
            files={"files": ("quotation-99-latest.xlsx", content.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert rejected.status_code == 400
        assert "AI Audit" in rejected.json()["detail"]
