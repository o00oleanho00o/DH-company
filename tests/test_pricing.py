from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import db
from app.ingest import ingest_workbook
from app.pricing import (
    AUTO_THRESHOLD,
    Candidate,
    choose_labor_rate,
    choose_product_price,
    find_labor_candidates,
    find_product_candidates,
    get_project_result,
    _price_multiplier,
    _status_for,
    run_pricing,
)

from .conftest import find_input_file


def _count(conn, table: str, where: str = "", params: tuple[object, ...] = ()) -> int:
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    return int(conn.execute(query, params).fetchone()[0])


def test_holdout_ingest_never_publishes_holdout_prices(isolated_db: Path) -> None:
    """The holdout's prices remain ground truth, not searchable knowledge."""

    supplier = find_input_file("1. Bảng giá CÁP HẠ")
    # The labor master is intentionally allowed to contain blank rates; use
    # the An Khánh historical quotation as a positive-rate training source.
    labor = find_input_file("BG. HT")
    holdout = find_input_file("BOQ-")

    supplier_stats = ingest_workbook(supplier)
    labor_stats = ingest_workbook(labor)
    holdout_stats = ingest_workbook(holdout, exclude_prices=True)

    assert supplier_stats["product_prices"] > 0
    assert labor_stats["labor_rates"] > 0
    assert holdout_stats["boq_items"] > 0
    assert holdout_stats["project_id"] is not None

    with db.db_session() as conn:
        holdout_id = int(
            conn.execute(
                "SELECT id FROM source_files WHERE filename = ?", (holdout.name,)
            ).fetchone()[0]
        )
        # No product/labor price row may cite the holdout source.
        assert _count(conn, "product_prices", "source_file_id = ?", (holdout_id,)) == 0
        assert _count(conn, "labor_rates", "source_file_id = ?", (holdout_id,)) == 0

        # Training data is still available, proving the test did not simply
        # disable pricing globally.
        assert _count(conn, "product_prices") > 0
        assert _count(conn, "labor_rates") > 0

        # The raw source and BOQ rows are retained for ground-truth comparison.
        assert _count(conn, "source_rows", "source_sheet_id IN (SELECT id FROM source_sheets WHERE source_file_id = ?)", (holdout_id,)) > 0
        assert _count(conn, "boq_items", "project_id = ?", (holdout_stats["project_id"],)) > 0


def test_ingest_is_idempotent_for_same_checksum(isolated_db: Path) -> None:
    path = find_input_file("Bảng giá nhân công")

    first = ingest_workbook(path)
    second = ingest_workbook(path)

    first_id = first.get("source_file_id", first.get("id"))
    second_id = second.get("source_file_id", second.get("id"))
    assert first_id == second_id
    assert second["skipped_duplicate"] is True

    with db.db_session() as conn:
        sha256 = conn.execute(
            "SELECT sha256 FROM source_files WHERE id = ?", (first_id,)
        ).fetchone()[0]
        assert _count(conn, "source_files", "sha256 = ?", (sha256,)) == 1


def test_allow_duplicate_creates_distinct_source_and_project(
    isolated_db: Path,
) -> None:
    """Quotation uploads may reuse bytes without violating checksum uniqueness."""

    path = find_input_file("BG. HT")
    first = ingest_workbook(path)
    duplicate = ingest_workbook(
        path,
        confirmed_type="NEW_BOQ",
        exclude_prices=True,
        allow_duplicate=True,
    )

    assert first["source_file_id"] != duplicate["source_file_id"]
    assert duplicate["project_id"] is not None

    with db.db_session() as conn:
        rows = conn.execute(
            """
            SELECT id, sha256, metadata_json
            FROM source_files
            WHERE filename=?
            ORDER BY id
            """,
            (path.name,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["sha256"] != rows[1]["sha256"]
        metadata = json.loads(rows[1]["metadata_json"])
        assert metadata["content_sha256"] == rows[0]["sha256"]
        assert metadata["duplicate_of_source_file_id"] == rows[0]["id"]
        # The explicit NEW_BOQ duplicate is holdout-safe.
        assert _count(
            conn,
            "product_prices",
            "source_file_id = ?",
            (rows[1]["id"],),
        ) == 0
        assert _count(
            conn,
            "labor_rates",
            "source_file_id = ?",
            (rows[1]["id"],),
        ) == 0


def test_price_rows_have_complete_provenance(isolated_db: Path) -> None:
    supplier = find_input_file("1. Bảng giá CÁP HẠ")
    labor = find_input_file("BG. HT")
    ingest_workbook(supplier)
    ingest_workbook(labor)

    with db.db_session() as conn:
        missing_material_source = conn.execute(
            """
            SELECT COUNT(*)
            FROM product_prices pp
            LEFT JOIN source_files sf ON sf.id = pp.source_file_id
            LEFT JOIN source_sheets ss ON ss.id = pp.source_sheet_id
            LEFT JOIN source_rows sr ON sr.id = pp.source_row_id
            WHERE pp.source_file_id IS NULL
               OR sf.id IS NULL
               OR ss.id IS NULL
               OR sr.id IS NULL
            """
        ).fetchone()[0]
        missing_labor_source = conn.execute(
            """
            SELECT COUNT(*)
            FROM labor_rates lr
            LEFT JOIN source_files sf ON sf.id = lr.source_file_id
            LEFT JOIN source_sheets ss ON ss.id = lr.source_sheet_id
            LEFT JOIN source_rows sr ON sr.id = lr.source_row_id
            WHERE lr.source_file_id IS NULL
               OR sf.id IS NULL
               OR ss.id IS NULL
               OR sr.id IS NULL
            """
        ).fetchone()[0]

    assert missing_material_source == 0
    assert missing_labor_source == 0


def test_holdout_metadata_explicitly_records_exclusion(isolated_db: Path) -> None:
    holdout = find_input_file("BOQ-")
    stats = ingest_workbook(holdout, exclude_prices=True)

    with db.db_session() as conn:
        source = conn.execute(
            "SELECT metadata_json FROM source_files WHERE id = ?", (stats["source_file_id"],)
        ).fetchone()
    metadata = json.loads(source[0])

    assert metadata["excluded_from_knowledge"] is True
    assert metadata["stats"]["boq_items"] == stats["boq_items"]


def test_candidate_search_prefers_exact_normalized_name_and_keeps_explanation(
    isolated_db: Path,
) -> None:
    """Candidate retrieval is deterministic and exposes score components."""

    training = find_input_file("BG. HT")
    ingest_workbook(training)

    with db.db_session() as conn:
        product = conn.execute(
            """
            SELECT p.*
            FROM products p
            JOIN product_prices pp ON pp.product_id = p.id
            WHERE p.normalized_name <> '' AND pp.net_price > 0
            ORDER BY pp.id
            LIMIT 1
            """
        ).fetchone()
        assert product is not None
        query = product["normalized_name"]
        candidates = find_product_candidates(
            conn,
            query,
            unit=product["unit"],
            limit=5,
        )

    assert candidates
    assert candidates[0].entity_id == product["id"]
    assert candidates[0].score >= AUTO_THRESHOLD
    assert candidates[0].components.get("exact_name") == 1.0
    assert candidates[0].explanation


def test_run_pricing_applies_only_sourced_prices_and_calculates_totals(
    isolated_db: Path,
) -> None:
    """End-to-end pricing test against a real historical quotation workbook."""

    training = find_input_file("BG. HT")
    stats = ingest_workbook(training)
    assert stats["project_id"] is not None

    with db.db_session() as conn:
        product = conn.execute(
            """
            SELECT p.id, p.normalized_name, p.unit, pp.net_price
            FROM products p
            JOIN product_prices pp ON pp.product_id = p.id
            WHERE p.normalized_name <> '' AND pp.net_price > 0
            ORDER BY pp.id
            LIMIT 1
            """
        ).fetchone()
        labor = conn.execute(
            """
            SELECT li.id, li.normalized_name, li.unit, lr.rate
            FROM labor_items li
            JOIN labor_rates lr ON lr.labor_item_id = li.id
            WHERE li.normalized_name <> '' AND lr.rate > 0
            ORDER BY lr.id
            LIMIT 1
            """
        ).fetchone()
        assert product is not None
        assert labor is not None

        # Create a tiny new quote using descriptions drawn from the imported
        # workbook.  This exercises the same matching path as a new BOQ while
        # keeping the assertion independent of a particular row number.
        project_cur = conn.execute(
            """
            INSERT INTO projects(project_name, metadata_json, created_at)
            VALUES (?, '{}', datetime('now'))
            """,
            ("pricing-test-project",),
        )
        project_id = int(project_cur.lastrowid)
        conn.execute(
            """
            INSERT INTO boq_items(
                project_id, raw_description, normalized_description,
                unit, quantity, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', datetime('now'))
            """,
            (
                project_id,
                product["normalized_name"],
                product["normalized_name"],
                product["unit"] or "m",
                2,
            ),
        )

    result = run_pricing(project_id)

    assert result["status"] == "COMPLETED"
    assert result["metrics"]["total_items"] == 1

    with db.db_session() as conn:
        row = conn.execute(
            "SELECT * FROM boq_items WHERE project_id = ?", (project_id,)
        ).fetchone()
        assert row is not None
        assert row["material_price"] is not None
        assert row["material_total"] == pytest.approx(row["material_price"] * 2)
        assert row["material_source_json"] not in {"", "{}", "null"}
        # A material row with no labor candidate is still reviewable; the
        # engine must never fabricate a labor price.
        if row["labor_price"] is None:
            assert row["labor_source_json"] in {"", "{}", "null"}


def test_candidate_conflict_is_not_auto_approved(isolated_db: Path) -> None:
    """A hard technical conflict must lower confidence before application."""

    # Medium-voltage cable rows contain an explicit voltage attribute, making
    # it possible to construct a deterministic hard-conflict query.
    training = find_input_file("2. Bảng giá CÁP TRUNG")
    ingest_workbook(training)

    with db.db_session() as conn:
        # Select a cable with a known cross-section and ask for an incompatible
        # voltage/armour combination.  The matcher may return a candidate for
        # review, but it must not score it as an automatic match.
        product = None
        for candidate in conn.execute(
            """
            SELECT p.*
            FROM products p
            WHERE p.category = 'cable'
              AND p.technical_attributes_json LIKE '%voltage%'
            ORDER BY p.id
            """
        ).fetchall():
            product = candidate
            break
        assert product is not None
        # Replace, rather than append, the voltage so technical extraction
        # sees only the conflicting value.
        query = re.sub(
            r"\b\d+(?:[./]\d+)?kV\b",
            "999/998kV",
            product["normalized_name"],
            count=1,
            flags=re.IGNORECASE,
        )
        candidates = find_product_candidates(conn, query, unit=product["unit"], limit=5)

    if candidates:
        assert candidates[0].score < AUTO_THRESHOLD
        assert "Mâu thuẫn" in candidates[0].explanation or candidates[0].components.get(
            "technical_attributes", 0
        ) < 1.0


def test_result_endpoint_shape_contains_items_and_metrics(isolated_db: Path) -> None:
    training = find_input_file("BG. HT")
    stats = ingest_workbook(training)
    assert stats["project_id"] is not None

    result = run_pricing(int(stats["project_id"]), force=True)
    payload = get_project_result(int(stats["project_id"]), result["run_id"])

    assert payload["project"]["id"] == stats["project_id"]
    assert payload["run"]["id"] == result["run_id"]
    assert payload["metrics"]["total_items"] == len(payload["items"])
    assert all("technical_attributes" in item for item in payload["items"])


def test_parallel_bundle_scales_matching_multi_core_catalog_item() -> None:
    candidate = Candidate(
        entity_id=1,
        name="abc 4x185",
        code=None,
        unit="m",
        brand=None,
        origin=None,
        attrs={
            "category": "cable",
            "cable_family": "abc",
            "cores": 4,
            "cross_section_mm2": 185.0,
        },
        score=0.965,
        components={},
        explanation="",
    )

    assert _price_multiplier("Cáp LV ABC 2x(4x185mm2)", candidate) == 2


def test_price_drift_warning_is_actionable_and_not_auto_approved() -> None:
    candidate = Candidate(
        entity_id=1,
        name="cáp cxv 1x240",
        code=None,
        unit="m",
        brand=None,
        origin=None,
        attrs={"category": "cable", "cable_family": "cxv"},
        score=0.98,
        components={},
        explanation="exact identity",
    )

    status, risk, explanation = _status_for(
        candidate,
        125000.0,
        None,
        None,
        10.0,
        material_source={
            "source_type": "historical_exact",
            "warnings": ["PRICE_DRIFT_HIGH"],
            "reason_code": "PRICE_DRIFT_HIGH",
        },
    )

    assert status == "PRICE_DRIFT_WARNING"
    assert risk == "HIGH"
    assert "PRICE_DRIFT_HIGH" in explanation
