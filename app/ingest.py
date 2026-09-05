from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import RAW_DIR, ensure_directories
from .db import db_session, dumps, row_dict, utc_now
from .excel import ParsedSheet, parse_workbook
from .normalize import canonical_key, normalize_text, normalize_unit, technical_attributes


def _source_file_record(conn, sha256: str) -> dict[str, Any] | None:
    return row_dict(conn.execute("SELECT * FROM source_files WHERE sha256 = ?", (sha256,)).fetchone())


def _upsert_product(
    conn,
    description: str,
    code: str | None,
    brand: str | None,
    origin: str | None,
    unit: str | None,
    category: str | None = None,
) -> int:
    attrs = technical_attributes(description)
    product_key = canonical_key(code) if code else canonical_key(description)
    # Include unit only if it prevents clearly different operational items
    # from collapsing onto one key.
    if unit:
        product_key = f"{product_key}|{normalize_unit(unit)}"
    existing = conn.execute("SELECT id FROM products WHERE canonical_key = ?", (product_key,)).fetchone()
    if existing:
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO products(
            canonical_key, normalized_name, product_code, category, brand,
            origin, unit, technical_attributes_json, aliases_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)
        """,
        (
            product_key,
            normalize_text(description),
            str(code).strip() if code not in (None, "") else None,
            category or attrs.get("category"),
            str(brand).strip() if brand not in (None, "") else None,
            str(origin).strip() if origin not in (None, "") else None,
            normalize_unit(unit),
            dumps(attrs),
            utc_now(),
        ),
    )
    return int(cur.lastrowid)


def _upsert_labor_item(conn, description: str, code: str | None, unit: str | None, category: str | None = None) -> int:
    key = canonical_key(code) if code else canonical_key(description)
    if unit:
        key = f"{key}|{normalize_unit(unit)}"
    existing = conn.execute("SELECT id FROM labor_items WHERE canonical_key = ?", (key,)).fetchone()
    if existing:
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO labor_items(
            canonical_key, normalized_name, code, category, unit,
            technical_attributes_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            normalize_text(description),
            str(code).strip() if code not in (None, "") else None,
            category or technical_attributes(description).get("category"),
            normalize_unit(unit),
            dumps(technical_attributes(description)),
            utc_now(),
        ),
    )
    return int(cur.lastrowid)


def _project_name_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    for prefix in ("BOQ-", "BG. ", "Báo giá ", "Bao gia "):
        if stem.lower().startswith(prefix.lower()):
            stem = stem[len(prefix):]
            break
    return stem.strip()


def ingest_workbook(
    path: Path,
    *,
    source_filename: str | None = None,
    confirmed_type: str | None = None,
    exclude_prices: bool = False,
    force: bool = False,
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """Parse and normalize a workbook into the operational database.

    `exclude_prices` is the critical holdout guard: source rows and BOQ ground
    truth can still be inspected, but their prices are never inserted into
    product_prices/labor_rates.
    """

    ensure_directories()
    display_filename = Path(source_filename).name if source_filename else path.name
    parsed = parse_workbook(path)
    with db_session() as conn:
        existing = _source_file_record(conn, parsed["sha256"])
        if existing and not force and not allow_duplicate:
            stats = source_file_detail(conn, int(existing["id"]))
            stats["skipped_duplicate"] = True
            return stats
        if existing and force:
            conn.execute("DELETE FROM source_files WHERE id = ?", (existing["id"],))

        # ``source_files.sha256`` is intentionally unique for ordinary
        # idempotent imports.  A quotation upload may explicitly request a
        # second project from the same bytes (``allow_duplicate=True``); in
        # that case retain the content checksum in metadata while assigning a
        # unique record checksum so the database constraint is respected.
        content_sha256 = parsed["sha256"]
        duplicate_of_source_file_id: int | None = None
        record_sha256 = content_sha256
        if existing and allow_duplicate and not force:
            duplicate_of_source_file_id = int(existing["id"])
            record_sha256 = hashlib.sha256(
                f"dh-source-duplicate:{content_sha256}:{uuid.uuid4().hex}".encode(
                    "utf-8"
                )
            ).hexdigest()

        storage_name = f"{record_sha256[:12]}-{display_filename}"
        raw_path = RAW_DIR / storage_name
        if path.resolve() != raw_path.resolve():
            shutil.copy2(path, raw_path)

        metadata = {
            "effective_date": parsed["effective_date"],
            "effective_date_inferred": parsed["effective_date_inferred"],
            "warnings": parsed["warnings"],
            "excluded_from_knowledge": exclude_prices,
            "original_path": str(path.resolve()),
            "original_filename": display_filename,
            "content_sha256": content_sha256,
            "duplicate_of_source_file_id": duplicate_of_source_file_id,
        }
        cur = conn.execute(
            """
            INSERT INTO source_files(
                filename, storage_key, sha256, extension, size_bytes,
                detected_type, confirmed_type, metadata_json, parsing_version,
                processing_status, uploaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PROCESSING', ?)
            """,
            (
                display_filename,
                str(raw_path),
                record_sha256,
                parsed["extension"],
                path.stat().st_size,
                parsed["workbook_type"],
                confirmed_type,
                dumps(metadata),
                parsed["parsing_version"],
                utc_now(),
            ),
        )
        source_file_id = int(cur.lastrowid)

        project_id: int | None = None
        workbook_type = confirmed_type or parsed["workbook_type"]
        has_operational_sheets = any(
            sheet.detected_type in {"HISTORICAL_BOQ", "BOQ", "PANEL_BOM"}
            for sheet in parsed["sheets"]
        )
        if workbook_type in {"HISTORICAL_BOQ", "BOQ", "NEW_BOQ", "MIXED"} or has_operational_sheets:
            pcur = conn.execute(
                """
                INSERT INTO projects(project_name, source_file_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    _project_name_from_filename(display_filename),
                    source_file_id,
                    dumps({"document_type": workbook_type, "holdout": exclude_prices}),
                    utc_now(),
                ),
            )
            project_id = int(pcur.lastrowid)

        stats = {
            "source_file_id": source_file_id,
            "filename": display_filename,
            "detected_type": parsed["workbook_type"],
            "confirmed_type": confirmed_type,
            "sheets": 0,
            "source_rows": 0,
            "data_rows": 0,
            "products": 0,
            "product_prices": 0,
            "labor_items": 0,
            "labor_rates": 0,
            "boq_items": 0,
            "warnings": list(parsed["warnings"]),
            "project_id": project_id,
        }

        # TONG is the canonical supplier-price sheet; detail sheets repeat the
        # same products with richer specs. Avoid double-counting their prices.
        supplier_has_tong = any(
            sheet.snapshot.name.strip().upper() == "TONG"
            and sheet.detected_type == "SUPPLIER_PRICE"
            for sheet in parsed["sheets"]
        )

        for sheet in parsed["sheets"]:
            stats["sheets"] += 1
            scur = conn.execute(
                """
                INSERT INTO source_sheets(
                    source_file_id, sheet_name, sheet_index, detected_type,
                    header_row, table_range, mapping_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_file_id,
                    sheet.snapshot.name,
                    sheet.snapshot.index,
                    sheet.detected_type,
                    sheet.header_row,
                    f"A1:{sheet.snapshot.max_col}x{sheet.snapshot.max_row}",
                    dumps(sheet.mapping),
                    dumps(
                        {
                            "max_row": sheet.snapshot.max_row,
                            "max_col": sheet.snapshot.max_col,
                            "merged_ranges": sheet.snapshot.merged_ranges,
                            "warnings": sheet.warnings,
                        }
                    ),
                ),
            )
            source_sheet_id = int(scur.lastrowid)
            is_canonical_price_sheet = not supplier_has_tong or sheet.snapshot.name.strip().upper() == "TONG"

            for row in sheet.rows:
                stats["source_rows"] += 1
                if row["row_kind"] == "data":
                    stats["data_rows"] += 1
                rcur = conn.execute(
                    """
                    INSERT INTO source_rows(
                        source_sheet_id, row_no, raw_cells_json, row_kind,
                        parse_warnings_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source_sheet_id,
                        row["row_no"],
                        dumps({"values": row["raw_cells"], "formulas": row["formula_cells"]}),
                        row["row_kind"],
                        dumps(row["warnings"]),
                    ),
                )
                source_row_id = int(rcur.lastrowid)
                if row["row_kind"] != "data":
                    continue

                fields = row["fields"]
                description = str(fields.get("description") or "").strip()
                if not description:
                    continue
                sheet_type = sheet.detected_type
                if sheet_type == "SUPPLIER_PRICE" and is_canonical_price_sheet:
                    net_price = fields.get("list_price")
                    if net_price is None or net_price < 0:
                        continue
                    product_id = _upsert_product(
                        conn,
                        description,
                        fields.get("code"),
                        "CADI-SUN" if "cáp" in path.name.lower() or "cap" in normalize_text(path.name) else fields.get("brand"),
                        fields.get("origin"),
                        fields.get("unit") or "m",
                        "cable" if "cap" in normalize_text(path.name) else None,
                    )
                    stats["products"] += 1
                    if not exclude_prices:
                        conn.execute(
                            """
                            INSERT INTO product_prices(
                                product_id, source_file_id, source_sheet_id,
                                source_row_id, supplier, list_price, discount,
                                net_price, effective_date, confidence, calc_json,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                            """,
                            (
                                product_id,
                                source_file_id,
                                source_sheet_id,
                                source_row_id,
                                "CADI-SUN",
                                net_price,
                                net_price,
                                parsed["effective_date"],
                                0.98 if parsed["effective_date"] else 0.9,
                                dumps(
                                    {
                                        "formula": "net_price = source ex-VAT list price",
                                        "effective_date_inferred": parsed["effective_date_inferred"],
                                    }
                                ),
                                utc_now(),
                            ),
                        )
                        stats["product_prices"] += 1
                elif sheet_type == "LABOR":
                    item_id = _upsert_labor_item(
                        conn,
                        description,
                        fields.get("code"),
                        fields.get("unit"),
                        sheet.snapshot.name,
                    )
                    stats["labor_items"] += 1
                    rate = fields.get("labor_price")
                    if rate is not None and rate > 0 and not exclude_prices:
                        conn.execute(
                            """
                            INSERT INTO labor_rates(
                                labor_item_id, source_file_id, source_sheet_id,
                                source_row_id, rate, effective_date, confidence,
                                policy_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 0.95, ?, ?)
                            """,
                            (
                                item_id,
                                source_file_id,
                                source_sheet_id,
                                source_row_id,
                                rate,
                                parsed["effective_date"],
                                dumps({"source_type": "labor_master"}),
                                utc_now(),
                            ),
                        )
                        stats["labor_rates"] += 1
                elif sheet_type in {"HISTORICAL_BOQ", "BOQ", "PANEL_BOM"} and project_id:
                    attrs = technical_attributes(description)
                    is_new_boq = workbook_type == "NEW_BOQ"
                    bcur = conn.execute(
                        """
                        INSERT INTO boq_items(
                            project_id, source_file_id, source_sheet_id,
                            source_row_id, section, raw_description,
                            normalized_description, raw_cells_json,
                            product_code, brand, origin, unit, quantity,
                            material_price, labor_price, status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            project_id,
                            source_file_id,
                            source_sheet_id,
                            source_row_id,
                            sheet.snapshot.name,
                            description,
                            normalize_text(description),
                            dumps(row["raw_cells"]),
                            fields.get("code"),
                            fields.get("brand"),
                            fields.get("origin"),
                            fields.get("unit"),
                            fields.get("quantity"),
                            None if is_new_boq else fields.get("material_price"),
                            None if is_new_boq else fields.get("labor_price"),
                            "HISTORICAL" if workbook_type == "HISTORICAL_BOQ" else "PENDING",
                            utc_now(),
                        ),
                    )
                    stats["boq_items"] += 1
                    boq_id = int(bcur.lastrowid)
                    if not exclude_prices and not is_new_boq:
                        material_price = fields.get("material_price")
                        if material_price is not None and material_price > 0:
                            product_id = _upsert_product(
                                conn,
                                description,
                                fields.get("code"),
                                fields.get("brand"),
                                fields.get("origin"),
                                fields.get("unit"),
                                attrs.get("category"),
                            )
                            conn.execute(
                                """
                                INSERT INTO product_prices(
                                    product_id, source_file_id, source_sheet_id,
                                    source_row_id, supplier, net_price,
                                    effective_date, confidence, calc_json,
                                    created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.82, ?, ?)
                                """,
                                (
                                    product_id,
                                    source_file_id,
                                    source_sheet_id,
                                    source_row_id,
                                    "Historical quotation",
                                    material_price,
                                    parsed["effective_date"],
                                    dumps({"source_type": "historical_boq", "boq_item_id": boq_id}),
                                    utc_now(),
                                ),
                            )
                            stats["products"] += 1
                            stats["product_prices"] += 1
                        labor_price = fields.get("labor_price")
                        if labor_price is not None and labor_price > 0:
                            labor_id = _upsert_labor_item(
                                conn,
                                description,
                                fields.get("code"),
                                fields.get("unit"),
                                attrs.get("category"),
                            )
                            conn.execute(
                                """
                                INSERT INTO labor_rates(
                                    labor_item_id, source_project_id,
                                    source_file_id, source_sheet_id, source_row_id,
                                    rate, effective_date, confidence, policy_json,
                                    created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.82, ?, ?)
                                """,
                                (
                                    labor_id,
                                    project_id,
                                    source_file_id,
                                    source_sheet_id,
                                    source_row_id,
                                    labor_price,
                                    parsed["effective_date"],
                                    dumps({"source_type": "historical_boq", "boq_item_id": boq_id}),
                                    utc_now(),
                                ),
                            )
                            stats["labor_items"] += 1
                            stats["labor_rates"] += 1

        conn.execute(
            "UPDATE source_files SET processing_status = 'COMPLETED', metadata_json = ? WHERE id = ?",
            (dumps({**metadata, "stats": stats}), source_file_id),
        )
        conn.execute(
            """
            INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at)
            VALUES ('IMPORT_COMPLETED', 'source_file', ?, ?, ?)
            """,
            (source_file_id, dumps(stats), utc_now()),
        )
        return stats


def source_file_detail(conn, source_file_id: int) -> dict[str, Any]:
    source = conn.execute("SELECT * FROM source_files WHERE id = ?", (source_file_id,)).fetchone()
    if not source:
        raise KeyError(source_file_id)
    sheets = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ss.*, COUNT(sr.id) AS row_count,
                   SUM(CASE WHEN sr.row_kind='data' THEN 1 ELSE 0 END) AS data_row_count
            FROM source_sheets ss
            LEFT JOIN source_rows sr ON sr.source_sheet_id=ss.id
            WHERE ss.source_file_id=?
            GROUP BY ss.id ORDER BY ss.sheet_index
            """,
            (source_file_id,),
        ).fetchall()
    ]
    result = dict(source)
    result["metadata"] = result.pop("metadata_json")
    result["sheets"] = sheets
    return result


def ingest_directory(
    input_dir: Path,
    *,
    holdout_filename: str | None = None,
    skip_holdout: bool = False,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Ingest all supported workbooks in a directory.

    ``holdout_filename`` normally keeps the selected workbook available for
    inspection while excluding its prices from the searchable catalog.  Seed
    commands can additionally set ``skip_holdout=True`` so a benchmark file
    never enters the operational database at all.
    """

    results = []
    for path in sorted(input_dir.iterdir()):
        if path.suffix.lower() not in {".xls", ".xlsx"}:
            continue
        is_holdout = holdout_filename is not None and path.name == holdout_filename
        if is_holdout and skip_holdout:
            continue
        results.append(
            ingest_workbook(
                path,
                exclude_prices=is_holdout,
                force=force,
            )
        )
    return results
