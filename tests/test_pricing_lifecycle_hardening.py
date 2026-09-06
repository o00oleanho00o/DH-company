from __future__ import annotations

import json

from app import db
from app.price_policy import normalize_price_basis, normalize_tax_mode
from app.pricing import choose_product_price, clear_runtime_caches, review_item, run_pricing


def _source(conn, filename: str, status: str = "ACTIVE") -> int:
    row = conn.execute(
        """
        INSERT INTO source_files(
            filename, storage_key, sha256, content_sha256, extension,
            size_bytes, detected_type, metadata_json, processing_status,
            lifecycle_status, uploaded_at
        ) VALUES (?, 'unused', ?, ?, '.xlsx', 1, 'SUPPLIER_PRICE',
                  '{}', 'COMPLETED', ?, datetime('now'))
        """,
        (filename, filename, filename, status),
    )
    return int(row.lastrowid)


def test_product_selection_uses_ex_vat_observation_for_drift(isolated_db) -> None:
    clear_runtime_caches()
    with db.db_session() as conn:
        current_source = _source(conn, "current.xlsx")
        historical_source = _source(conn, "history.xlsx")
        conn.execute(
            """
            INSERT INTO products(
                canonical_key, normalized_name, category, unit,
                technical_attributes_json, aliases_json, created_at
            ) VALUES ('p-1', 'Cable CXV 1x10', 'cable', 'm', '{}', '[]', datetime('now'))
            """
        )
        product_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO product_prices(
                product_id, source_file_id, supplier, net_price, tax_mode,
                effective_date, calc_json, created_at
            ) VALUES (?, ?, 'Current supplier', 200, 'ex_vat', '2026-01-01',
                      '{}', datetime('now'))
            """,
            (product_id, current_source),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date, created_at
            ) VALUES (?, ?, 'Current supplier', 'supplier_list', 200,
                      'ex_vat', 'net', '2026-01-01', datetime('now'))
            """,
            (product_id, current_source),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date, created_at
            ) VALUES (?, ?, 'Historical quotation', 'historical_boq', 100,
                      'ex_vat', 'net', '2025-01-01', datetime('now'))
            """,
            (product_id, historical_source),
        )
        selected, source = choose_product_price(
            conn, product_id, "2026-09-06", drift_threshold=0.25
        )

    assert selected == 200
    assert source["price_drift_ratio"] == 1.0
    assert source["reason_code"] == "PRICE_DRIFT_HIGH"
    assert source["historical_reference_price"] == 100


def test_selected_supplier_tier_wins_and_tax_labels_are_normalized(
    isolated_db,
) -> None:
    """Do not choose the last inserted discount/VAT sibling by row ID."""

    clear_runtime_caches()
    with db.db_session() as conn:
        source_id = _source(conn, "tier-selection.xlsx")
        product_id = int(
            conn.execute(
                """
                INSERT INTO products(
                    canonical_key, normalized_name, category, unit,
                    technical_attributes_json, aliases_json, created_at
                ) VALUES ('p-tier', 'Cable tier 1x10', 'cable', 'm', '{}', '[]',
                          datetime('now'))
                """
            ).lastrowid
        )
        # Insert the authoritative base ex-VAT observation first, then sibling
        # observations that would otherwise win by their larger IDs.
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date,
                context_json, calc_json, created_at
            ) VALUES (?, ?, 'Supplier', 'supplier_list', 100,
                      'EX VAT', 'NET PRICE', '2026-01-01',
                      ?, ?, datetime('now'))
            """,
            (
                product_id,
                source_id,
                json.dumps({"selected": True}),
                json.dumps({"selection": "base_list"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date,
                context_json, created_at
            ) VALUES (?, ?, 'Supplier', 'supplier_discounted', 70,
                      'ex_vat', 'net', '2026-01-01', ?, datetime('now'))
            """,
            (product_id, source_id, json.dumps({"selected": False})),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date,
                context_json, created_at
            ) VALUES (?, ?, 'Supplier', 'supplier_list', 108,
                      'INC-VAT', 'gross', '2026-01-01', ?, datetime('now'))
            """,
            (product_id, source_id, json.dumps({"selected": False})),
        )
        value, source = choose_product_price(
            conn, product_id, "2026-09-06", drift_threshold=0.25
        )

    assert normalize_tax_mode("EX VAT") == "ex_vat"
    assert normalize_tax_mode("VAT price") == "inc_vat"
    assert normalize_price_basis("NET PRICE") == "net"
    assert normalize_price_basis("gross-price") == "gross"
    assert value == 100
    assert source["observation_type"] == "supplier_list"
    assert source["tax_mode"] == "EX VAT"
    assert source["selection_explicit"] is True
    assert "TAX_BASIS_FALLBACK" not in source.get("warnings", [])


def test_current_observation_wins_over_legacy_historical_operational_row(
    isolated_db,
) -> None:
    """Immutable current observations must not be shadowed by old product_prices."""

    clear_runtime_caches()
    with db.db_session() as conn:
        current_source = _source(conn, "current-observation.xlsx")
        historical_source = _source(conn, "legacy-history.xlsx")
        product_id = int(
            conn.execute(
                """
                INSERT INTO products(
                    canonical_key, normalized_name, category, unit,
                    technical_attributes_json, aliases_json, created_at
                ) VALUES ('p-observation-priority', 'Cable priority 1x10',
                          'cable', 'm', '{}', '[]', datetime('now'))
                """
            ).lastrowid
        )
        # This row is retained for legacy compatibility, but must not win over
        # the immutable observation below.
        conn.execute(
            """
            INSERT INTO product_prices(
                product_id, source_file_id, supplier, net_price, tax_mode,
                effective_date, calc_json, created_at
            ) VALUES (?, ?, 'Historical quotation', 100, 'ex_vat',
                      '2025-01-01', ?, datetime('now'))
            """,
            (product_id, historical_source, json.dumps({"source_type": "historical_boq"})),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date, created_at
            ) VALUES (?, ?, 'Current supplier', 'supplier_list', 200,
                      'ex_vat', 'net', '2026-01-01', datetime('now'))
            """,
            (product_id, current_source),
        )
        value, source = choose_product_price(
            conn, product_id, "2026-09-06", drift_threshold=0.25
        )

    assert value == 200
    assert source["source_tier"] == "current_supplier_net"


def test_zero_drift_threshold_is_preserved_and_flags_any_difference(
    isolated_db,
) -> None:
    """A zero threshold is meaningful and must not fall back to 25%."""

    clear_runtime_caches()
    with db.db_session() as conn:
        current_source = _source(conn, "zero-current.xlsx")
        historical_source = _source(conn, "zero-history.xlsx")
        product_id = int(
            conn.execute(
                """
                INSERT INTO products(
                    canonical_key, normalized_name, category, unit,
                    technical_attributes_json, aliases_json, created_at
                ) VALUES ('p-zero-threshold', 'Cable zero 1x6',
                          'cable', 'm', '{}', '[]', datetime('now'))
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date, created_at
            ) VALUES (?, ?, 'Current supplier', 'supplier_list', 101,
                      'ex_vat', 'net', '2026-01-01', datetime('now'))
            """,
            (product_id, current_source),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date, created_at
            ) VALUES (?, ?, 'Historical quotation', 'historical_boq', 100,
                      'ex_vat', 'net', '2025-01-01', datetime('now'))
            """,
            (product_id, historical_source),
        )
        value, source = choose_product_price(
            conn, product_id, "2026-09-06", drift_threshold={"drift_threshold": 0}
        )

    assert value == 101
    assert source["price_drift_threshold"] == 0
    assert source["price_drift_ratio"] == 0.01
    assert source["reason_code"] == "PRICE_DRIFT_HIGH"


def test_archived_source_is_not_selected_for_new_price(isolated_db) -> None:
    clear_runtime_caches()
    with db.db_session() as conn:
        source_id = _source(conn, "old.xlsx", "ARCHIVED")
        conn.execute(
            """
            INSERT INTO products(
                canonical_key, normalized_name, category, unit,
                technical_attributes_json, aliases_json, created_at
            ) VALUES ('p-2', 'Cable CV 1x6', 'cable', 'm', '{}', '[]', datetime('now'))
            """
        )
        product_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO product_prices(
                product_id, source_file_id, supplier, net_price, tax_mode,
                effective_date, created_at
            ) VALUES (?, ?, 'Old supplier', 321, 'ex_vat', '2026-01-01', datetime('now'))
            """,
            (product_id, source_id),
        )
        selected, source = choose_product_price(conn, product_id, "2026-09-06")

    assert selected is None
    assert source == {}


def test_manual_review_is_persisted_and_survives_default_rerun(isolated_db) -> None:
    clear_runtime_caches()
    with db.db_session() as conn:
        project = conn.execute(
            """
            INSERT INTO projects(project_name, quotation_date, metadata_json, created_at)
            VALUES ('manual-review', '2026-09-06', '{}', datetime('now'))
            """
        )
        project_id = int(project.lastrowid)
        product = conn.execute(
            """
            INSERT INTO products(
                canonical_key, normalized_name, category, unit,
                technical_attributes_json, aliases_json, created_at
            ) VALUES ('p-3', 'Cable Manual 1x4', 'cable', 'm', '{}', '[]', datetime('now'))
            """
        )
        product_id = int(product.lastrowid)
        item = conn.execute(
            """
            INSERT INTO boq_items(
                project_id, raw_description, normalized_description, unit, quantity,
                matched_product_id, status, created_at
            ) VALUES (?, 'Cable Manual 1x4', 'cable manual 1x4', 'm', 2, ?,
                      'REVIEW_REQUIRED', datetime('now'))
            """,
            (project_id, product_id),
        )
        item_id = int(item.lastrowid)

    reviewed = review_item(
        item_id,
        material_price=123,
        material_source={
            "type": "manual",
            "note": "Email NCC ngày 06/09/2026",
            "entered_by": "tester",
            "entered_at": "2026-09-06T00:00:00+00:00",
        },
        status="AUTO_APPROVED",
    )
    assert reviewed["status"] == "AUTO_APPROVED"
    assert reviewed["status_reason"] == "manual_engineer_review"

    with db.db_session() as conn:
        observations = conn.execute(
            """
            SELECT observation_type, net_price, context_json
            FROM price_observations WHERE product_id=?
            """,
            (product_id,),
        ).fetchall()
    assert len(observations) == 1
    assert observations[0]["observation_type"] == "manual_review"
    assert observations[0]["net_price"] == 123
    assert json.loads(observations[0]["context_json"])["boq_item_id"] == item_id

    rerun = run_pricing(project_id, policy={"llm_enabled": False})
    assert rerun["status"] == "COMPLETED"
    with db.db_session() as conn:
        row = conn.execute(
            "SELECT material_price, reviewed, pricing_run_id FROM boq_items WHERE id=?",
            (item_id,),
        ).fetchone()
    assert row["material_price"] == 123
    assert row["reviewed"] == 1
    assert row["pricing_run_id"] == rerun["run_id"]


def test_selected_candidate_with_explicit_price_creates_manual_observation(
    isolated_db,
) -> None:
    """A manual amount must not inherit the selected candidate's source."""

    clear_runtime_caches()
    with db.db_session() as conn:
        project = conn.execute(
            """
            INSERT INTO projects(project_name, quotation_date, metadata_json, created_at)
            VALUES ('manual-candidate-price', '2026-09-06', '{}', datetime('now'))
            """
        )
        project_id = int(project.lastrowid)
        product = conn.execute(
            """
            INSERT INTO products(
                canonical_key, normalized_name, category, unit,
                technical_attributes_json, aliases_json, created_at
            ) VALUES ('p-4', 'Cable Candidate 1x6', 'cable', 'm', '{}', '[]',
                      datetime('now'))
            """
        )
        product_id = int(product.lastrowid)
        source_id = _source(conn, "candidate-price.xlsx")
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date, created_at
            ) VALUES (?, ?, 'Supplier', 'supplier_list', 100,
                      'ex_vat', 'net', '2026-01-01', datetime('now'))
            """,
            (product_id, source_id),
        )
        item = conn.execute(
            """
            INSERT INTO boq_items(
                project_id, raw_description, normalized_description, unit, quantity,
                status, created_at
            ) VALUES (?, 'Cable Candidate 1x6', 'cable candidate 1x6', 'm', 2,
                      'REVIEW_REQUIRED', datetime('now'))
            """,
            (project_id,),
        )
        item_id = int(item.lastrowid)

    reviewed = review_item(
        item_id,
        selected_product_id=product_id,
        material_price=321,
        status="AUTO_APPROVED",
    )
    assert reviewed["material_price"] == 321
    assert reviewed["material_source"]["type"] == "manual"

    with db.db_session() as conn:
        observations = conn.execute(
            """
            SELECT observation_type, net_price, context_json
            FROM price_observations WHERE product_id=?
            ORDER BY id
            """,
            (product_id,),
        ).fetchall()
    assert len(observations) == 2
    assert observations[-1]["observation_type"] == "manual_review"
    assert observations[-1]["net_price"] == 321
    assert json.loads(observations[-1]["context_json"])["boq_item_id"] == item_id
