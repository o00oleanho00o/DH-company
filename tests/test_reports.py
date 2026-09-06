from __future__ import annotations

import json
from pathlib import Path

from app import db
from benchmarks.reports import (
    build_data_gap_report,
    build_data_requirements,
    build_failure_analysis,
    normalize_category,
    render_failure_markdown,
    write_report_files,
)


def _insert_product(conn, name: str, category: str, price: float) -> int:
    product_id = int(
        conn.execute(
            """
            INSERT INTO products(
                canonical_key, normalized_name, category, unit,
                technical_attributes_json, created_at
            ) VALUES (?, ?, ?, 'm', '{}', ?)
            RETURNING id
            """,
            (name.replace(" ", "-"), name, category, "2026-09-01T00:00:00+00:00"),
        ).fetchone()[0]
    )
    conn.execute(
        """
        INSERT INTO product_prices(product_id, net_price, confidence, created_at)
        VALUES (?, ?, 1.0, ?)
        """,
        (product_id, price, "2026-09-01T00:00:00+00:00"),
    )
    return product_id


def test_normalize_category_is_stable_and_generic() -> None:
    assert normalize_category("Cáp Cu/PVC 1x10") == "Cable"
    assert normalize_category("Đèn LED 50W") == "Lighting"
    assert normalize_category("Ống luồn dây PVC D25") == "Conduit"
    assert normalize_category("Hạng mục lạ không rõ") == "Unknown"


def test_data_gap_and_failure_reports_separate_source_gap_from_match_failure(
    isolated_db: Path,
) -> None:
    with db.db_session() as conn:
        product_id = _insert_product(conn, "cáp cu pvc 1x10", "cable", 100.0)
        project_id = int(
            conn.execute(
                "INSERT INTO projects(project_name, metadata_json, created_at) VALUES (?, '{}', ? ) RETURNING id",
                ("report-test", "2026-09-01T00:00:00+00:00"),
            ).fetchone()[0]
        )
        common = {
            "project_id": project_id,
            "normalized_description": "",
            "raw_cells_json": "{}",
            "status": "NO_MATCH",
            "created_at": "2026-09-01T00:00:00+00:00",
        }
        conn.execute(
            """
            INSERT INTO boq_items(
                project_id, raw_description, normalized_description, unit, quantity,
                matched_product_id, material_price, status, created_at
            ) VALUES (?, ?, ?, 'm', 1, ?, ?, 'AUTO_APPROVED', ?)
            """,
            (
                project_id,
                "Cáp Cu/PVC 1x10",
                "cap cu pvc 1x10",
                product_id,
                100.0,
                "2026-09-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO boq_items(
                project_id, raw_description, normalized_description, unit, quantity,
                status, created_at
            ) VALUES (?, ?, ?, 'm', 1, 'NO_MATCH', ?)
            """,
            (
                project_id,
                "Cáp Cu/PVC 1x16",
                "cap cu pvc 1x16",
                "2026-09-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO boq_items(
                project_id, raw_description, normalized_description, unit, quantity,
                status, created_at
            ) VALUES (?, ?, ?, 'cái', 2, 'NO_MATCH', ?)
            """,
            (
                project_id,
                "Đèn LED 50W",
                "den led 50w",
                "2026-09-01T00:00:00+00:00",
            ),
        )
        gap = build_data_gap_report(conn, project_id)
        failures = build_failure_analysis(conn, project_id)

    assert gap["priceable_items"] == 3
    assert gap["source_supported"] == 2
    assert gap["matched"] == 1
    assert gap["priced"] == 1
    assert gap["engine_capture_rate"] == 0.5
    by_category = {item["category"]: item for item in gap["categories"]}
    assert by_category["Cable"]["main_gap"] == "MATCH_FAILURE"
    assert by_category["Cable"]["source_supported"] == 2
    assert by_category["Lighting"]["main_gap"] == "MISSING_SOURCE_DATA"
    assert failures["failure_counts"]["MATCH_FAILURE"] >= 1
    assert failures["failure_counts"]["MISSING_SOURCE_DATA"] >= 1
    assert all("technical_attributes" in item for item in failures["failures"])
    assert "material_failures" in failures
    assert "labor_failures" in failures
    rendered = render_failure_markdown(failures)
    assert "Top material failures" in rendered
    assert "Top labor failures" in rendered


def test_data_requirement_writer_creates_required_artifacts(tmp_path: Path) -> None:
    data_gap = {
        "project_id": 7,
        "priceable_items": 10,
        "source_supported": 3,
        "matched": 3,
        "priced": 2,
        "source_coverage": 0.3,
        "engine_capture_rate": 1.0,
        "priced_capture_rate": 0.6667,
        "priced_coverage": 0.2,
        "categories": [
            {
                "category": "Lighting",
                "priceable_items": 7,
                "source_supported": 0,
                "matched": 0,
                "priced": 0,
                "source_coverage": 0.0,
                "capture_rate": 0.0,
                "main_gap": "MISSING_SOURCE_DATA",
            }
        ],
    }
    requirements = build_data_requirements(data_gap)
    assert requirements["potential_unlock_items"] == 7
    assert requirements["categories"][0]["category"] == "Lighting"
    paths = write_report_files(
        tmp_path,
        data_gap=data_gap,
        failure_analysis={"failure_counts": {"MISSING_SOURCE_DATA": 7}},
        data_requirements=requirements,
    )
    assert set(paths) == {
        "data_gap_json",
        "data_gap_markdown",
        "failure_analysis_markdown",
        "data_requirements_markdown",
    }
    assert Path(paths["data_gap_json"]).exists()
    assert "Data gap" in Path(paths["data_gap_markdown"]).read_text(encoding="utf-8")
    assert "Lighting" in Path(paths["data_requirements_markdown"]).read_text(encoding="utf-8")
    payload = json.loads(Path(paths["data_gap_json"]).read_text(encoding="utf-8"))
    assert payload["project_id"] == 7
