from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import db_session, dumps, utc_now
from app.ingest import ingest_workbook
from app.main import app
from tests.conftest import find_input_file


def _seed_source_graph() -> int:
    with db_session() as conn:
        source = conn.execute(
            """
            INSERT INTO source_files(
                filename, storage_key, sha256, content_sha256, extension,
                size_bytes, detected_type, metadata_json, processing_status,
                lifecycle_status, uploaded_at
            ) VALUES ('catalog.xlsx', 'unused', 'sha', 'sha', '.xlsx', 1,
                      'SUPPLIER_PRICE', ?, 'COMPLETED', 'ACTIVE', ?)
            """,
            (dumps({"warnings": ["header_guess"]}), utc_now()),
        )
        source_id = int(source.lastrowid)
        sheet = conn.execute(
            """
            INSERT INTO source_sheets(
                source_file_id, sheet_name, sheet_index, detected_type,
                header_row, mapping_json, metadata_json
            ) VALUES (?, 'TONG', 0, 'SUPPLIER_PRICE', 2, ?, ?)
            """,
            (source_id, dumps({"description": 1, "list_price": 2}), dumps({"warnings": []})),
        )
        sheet_id = int(sheet.lastrowid)
        row = conn.execute(
            """
            INSERT INTO source_rows(
                source_sheet_id, row_no, raw_cells_json, row_kind, parse_warnings_json
            ) VALUES (?, 3, ?, 'data', '[]')
            """,
            (sheet_id, dumps({"values": ["P-1", "Cable", 100]})),
        )
        row_id = int(row.lastrowid)
        product = conn.execute(
            """
            INSERT INTO products(
                canonical_key, normalized_name, product_code, category, unit,
                technical_attributes_json, aliases_json, created_at
            ) VALUES ('p-1', 'Cable', 'P-1', 'Cable', 'm', '{}', '[]', ?)
            """,
            (utc_now(),),
        )
        product_id = int(product.lastrowid)
        conn.execute(
            """
            INSERT INTO catalog_source_links(
                entity_type, product_id, source_file_id, source_sheet_id,
                source_row_id, relation_type, created_at
            ) VALUES ('product', ?, ?, ?, ?, 'price_observation', ?)
            """,
            (product_id, source_id, sheet_id, row_id, utc_now()),
        )
        conn.execute(
            """
            INSERT INTO product_prices(
                product_id, source_file_id, source_sheet_id, source_row_id,
                supplier, net_price, effective_date, created_at
            ) VALUES (?, ?, ?, ?, 'Supplier', 100, '2026-01-01', ?)
            """,
            (product_id, source_id, sheet_id, row_id, utc_now()),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, source_sheet_id, source_row_id,
                supplier, observation_type, net_price, effective_date, created_at
            ) VALUES (?, ?, ?, ?, 'Supplier', 'supplier_list', 100, '2026-01-01', ?)
            """,
            (product_id, source_id, sheet_id, row_id, utc_now()),
        )
        return source_id


def test_source_detail_archive_and_safe_delete(isolated_db):
    source_id = _seed_source_graph()
    with TestClient(app) as client:
        detail = client.get(f"/api/sources/{source_id}").json()
        assert detail["summary"]["products"] == 1
        assert detail["summary"]["prices"] == 1
        assert detail["summary"]["price_observations"] == 1
        assert detail["sheets"][0]["mapping"]["description"] == 1
        assert detail["products"][0]["provenance"][0]["file_id"] == source_id
        assert "storage_key" not in detail
        assert detail["raw_available"] is True
        assert "original_path" not in detail["metadata"]

        archived = client.post(f"/api/sources/{source_id}/archive").json()
        assert archived["lifecycle"]["status"] == "ARCHIVED"
        conflict = client.delete(f"/api/sources/{source_id}")
        assert conflict.status_code == 409
        forced = client.delete(f"/api/sources/{source_id}?force=true").json()
        assert forced["archived"] is True

        restored = client.post(f"/api/sources/{source_id}/restore").json()
        assert restored["lifecycle"]["status"] == "ACTIVE"

    with db_session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM product_prices WHERE source_file_id=?", (source_id,)
        ).fetchone()[0] == 1


def test_source_raw_row_limit_is_workbook_wide(isolated_db):
    """Raw preview budget must not multiply by the number of workbook sheets."""

    source_id = _seed_source_graph()
    with db_session() as conn:
        sheet = conn.execute(
            """
            INSERT INTO source_sheets(
                source_file_id, sheet_name, sheet_index, detected_type,
                header_row, mapping_json, metadata_json
            ) VALUES (?, 'SECOND', 1, 'SUPPLIER_PRICE', 1, '{}', '{}')
            """,
            (source_id,),
        )
        second_sheet_id = int(sheet.lastrowid)
        for row_no in range(1, 5):
            conn.execute(
                """
                INSERT INTO source_rows(
                    source_sheet_id, row_no, raw_cells_json, row_kind,
                    parse_warnings_json
                ) VALUES (?, ?, '{}', 'data', '[]')
                """,
                (second_sheet_id, row_no),
            )
        first_sheet_id = int(
            conn.execute(
                """
                SELECT id FROM source_sheets
                WHERE source_file_id=? AND sheet_index=0
                """,
                (source_id,),
            ).fetchone()[0]
        )
        for row_no in range(4, 8):
            conn.execute(
                """
                INSERT INTO source_rows(
                    source_sheet_id, row_no, raw_cells_json, row_kind,
                    parse_warnings_json
                ) VALUES (?, ?, '{}', 'data', '[]')
                """,
                (first_sheet_id, row_no),
            )

    with TestClient(app) as client:
        detail = client.get(
            f"/api/sources/{source_id}",
            params={"include_rows": True, "limit": 3},
        ).json()

    returned_rows = sum(len(sheet.get("rows") or []) for sheet in detail["sheets"])
    assert returned_rows == 3
    assert [len(sheet.get("rows") or []) for sheet in detail["sheets"]] == [2, 1]


def test_catalog_list_and_product_detail(isolated_db):
    source_id = _seed_source_graph()
    with TestClient(app) as client:
        response = client.get(
            "/api/catalog/items",
            params={"kind": "product", "source_id": source_id},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["sources"][0]["link_count"] == 1
        item = payload["items"][0]
        assert item["current_price"] == 100
        assert item["source"]["file_id"] == source_id

        detail = client.get(f"/api/catalog/products/{item['id']}").json()
        assert len(detail["prices"]) == 1
        assert len(detail["observations"]) == 1
        assert detail["provenance"][0]["filename"] == "catalog.xlsx"

        archived = client.post(
            f"/api/catalog/items/product/{item['id']}/archive"
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "ARCHIVED"
        assert client.delete(f"/api/catalog/items/product/{item['id']}").status_code == 409


def test_catalog_current_price_prefers_immutable_observation_over_legacy_price(
    isolated_db,
):
    """The catalog read model must expose the same price the engine selects."""

    source_id = _seed_source_graph()
    with db_session() as conn:
        product_id = int(
            conn.execute(
                "SELECT product_id FROM price_observations WHERE source_file_id=? LIMIT 1",
                (source_id,),
            ).fetchone()[0]
        )
        # Leave an old compatibility projection at a deliberately different
        # value.  A newer immutable supplier observation is authoritative.
        conn.execute(
            "UPDATE product_prices SET net_price=900 WHERE product_id=?",
            (product_id,),
        )
        conn.execute(
            """
            INSERT INTO price_observations(
                product_id, source_file_id, supplier, observation_type,
                net_price, tax_mode, price_basis, effective_date, created_at
            ) VALUES (?, ?, 'Supplier', 'supplier_list', 250,
                      'ex_vat', 'net', '2026-02-01', ?)
            """,
            (product_id, source_id, utc_now()),
        )

    with TestClient(app) as client:
        item = client.get(
            "/api/catalog/items",
            params={"kind": "product", "source_id": source_id},
        ).json()["items"][0]
        detail = client.get(f"/api/catalog/products/{product_id}").json()

    assert item["current_price"] == 250
    assert item["price"]["record_type"] == "price_observation"
    assert item["price"]["source_tier"] == "current_supplier_net"
    assert item["observation_count"] == 2
    assert detail["current_price"] == 250
    assert detail["price"]["id"] == item["price"]["id"]


def test_catalog_all_pagination_is_global_and_stable(isolated_db):
    """kind=all applies one page window across products and labor records."""

    _seed_source_graph()
    with db_session() as conn:
        for index, name in enumerate(("Alpha cable", "Gamma cable", "Omega cable"), start=1):
            conn.execute(
                """
                INSERT INTO products(
                    canonical_key, normalized_name, product_code, category,
                    unit, technical_attributes_json, aliases_json, created_at
                ) VALUES (?, ?, ?, 'Cable', 'm', '{}', '[]', ?)
                """,
                (f"page-product-{index}", name, f"PX-{index}", utc_now()),
            )
        for index, name in enumerate(("Beta installer", "Delta installer"), start=1):
            conn.execute(
                """
                INSERT INTO labor_items(
                    canonical_key, normalized_name, code, category, unit,
                    technical_attributes_json, created_at
                ) VALUES (?, ?, ?, 'Labor', 'h', '{}', ?)
                """,
                (f"page-labor-{index}", name, f"LX-{index}", utc_now()),
            )

    with TestClient(app) as client:
        full = client.get(
            "/api/catalog/items",
            params={"kind": "all", "status": "ALL", "limit": 50, "offset": 0},
        ).json()
        first = client.get(
            "/api/catalog/items",
            params={"kind": "all", "status": "ALL", "limit": 2, "offset": 0},
        ).json()
        second = client.get(
            "/api/catalog/items",
            params={"kind": "all", "status": "ALL", "limit": 2, "offset": 2},
        ).json()

    assert full["total"] == 6
    assert first["total"] == second["total"] == full["total"]
    assert len(first["items"]) == len(second["items"]) == 2
    full_keys = [(item["kind"], item["id"]) for item in full["items"]]
    paged_keys = [
        (item["kind"], item["id"])
        for item in [*first["items"], *second["items"]]
    ]
    assert paged_keys == full_keys[:4]
    assert len(set(paged_keys)) == 4


def test_source_detail_uses_legacy_direct_provenance_when_bridge_is_missing(isolated_db):
    """Upgraded databases still show the source row behind an old price row."""

    source_id = _seed_source_graph()
    with db_session() as conn:
        conn.execute(
            "DELETE FROM catalog_source_links WHERE source_file_id=?", (source_id,)
        )

    with TestClient(app) as client:
        detail = client.get(f"/api/sources/{source_id}").json()
        catalog = client.get(
            "/api/catalog/items",
            params={"kind": "product", "source_id": source_id},
        ).json()

    assert detail["products"][0]["provenance"]
    pointer = detail["products"][0]["provenance"][0]
    assert pointer["file_id"] == source_id
    assert pointer["relation_type"] == "price"
    # Facets must remain meaningful for databases created before the bridge
    # table existed; direct price/rate provenance still counts as a link.
    assert catalog["sources"][0]["link_count"] == 1


def test_force_reimport_versions_source_without_breaking_provenance(isolated_db):
    """A forced import must retire the old source, not delete its graph."""

    workbook = find_input_file("BG. HT TT")
    first = ingest_workbook(workbook)
    old_id = int(first["source_file_id"])
    assert first["product_prices"] > 0
    assert first["price_observations"] > 0
    assert first["labor_rates"] > 0
    assert first["boq_items"] > 0

    with db_session() as conn:
        old_counts = {
            "prices": conn.execute(
                "SELECT COUNT(*) FROM product_prices WHERE source_file_id=?",
                (old_id,),
            ).fetchone()[0],
            "observations": conn.execute(
                "SELECT COUNT(*) FROM price_observations WHERE source_file_id=?",
                (old_id,),
            ).fetchone()[0],
            "rates": conn.execute(
                "SELECT COUNT(*) FROM labor_rates WHERE source_file_id=?",
                (old_id,),
            ).fetchone()[0],
            "boq": conn.execute(
                "SELECT COUNT(*) FROM boq_items WHERE source_file_id=?",
                (old_id,),
            ).fetchone()[0],
        }

    second = ingest_workbook(workbook, force=True)
    new_id = int(second["source_file_id"])
    assert new_id != old_id

    with db_session() as conn:
        old = conn.execute(
            """
            SELECT lifecycle_status, version_no, content_sha256,
                   superseded_by_source_file_id
            FROM source_files WHERE id=?
            """,
            (old_id,),
        ).fetchone()
        new = conn.execute(
            """
            SELECT lifecycle_status, version_no, content_sha256,
                   supersedes_source_file_id
            FROM source_files WHERE id=?
            """,
            (new_id,),
        ).fetchone()
        assert old["lifecycle_status"] == "SUPERSEDED"
        assert old["superseded_by_source_file_id"] == new_id
        assert new["lifecycle_status"] == "ACTIVE"
        assert new["supersedes_source_file_id"] == old_id
        assert new["version_no"] == old["version_no"] + 1
        assert new["content_sha256"] == old["content_sha256"]

        # Existing derived rows continue to point at the immutable old source.
        assert conn.execute(
            "SELECT COUNT(*) FROM product_prices WHERE source_file_id=?",
            (old_id,),
        ).fetchone()[0] == old_counts["prices"]
        assert conn.execute(
            "SELECT COUNT(*) FROM price_observations WHERE source_file_id=?",
            (old_id,),
        ).fetchone()[0] == old_counts["observations"]
        assert conn.execute(
            "SELECT COUNT(*) FROM labor_rates WHERE source_file_id=?",
            (old_id,),
        ).fetchone()[0] == old_counts["rates"]
        assert conn.execute(
            "SELECT COUNT(*) FROM boq_items WHERE source_file_id=?",
            (old_id,),
        ).fetchone()[0] == old_counts["boq"]

        # The new import has its own source pointers, so both versions are
        # auditable and can be compared without NULL provenance.
        assert conn.execute(
            "SELECT COUNT(*) FROM price_observations WHERE source_file_id=?",
            (new_id,),
        ).fetchone()[0] == second["price_observations"]
        assert conn.execute(
            "SELECT COUNT(*) FROM labor_rates WHERE source_file_id=?",
            (new_id,),
        ).fetchone()[0] == second["labor_rates"]
