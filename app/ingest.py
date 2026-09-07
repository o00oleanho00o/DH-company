from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import RAW_DIR, ensure_directories
from .db import db_session, dumps, loads, row_dict, utc_now
from .excel import ParsedSheet, parse_workbook
from .normalize import canonical_key, normalize_text, normalize_unit, technical_attributes
from .price_policy import (
    load_manual_pricing_rules,
    select_manual_pricing_rule,
    select_price_observation,
)

_BOQ_SHEET_TYPES = {"HISTORICAL_BOQ", "BOQ", "PANEL_BOM"}


def _load_learned_header_synonyms() -> dict[str, dict[str, tuple[str, ...]]]:
    """Header text an earlier AI column-mapping call confirmed, by sheet type.

    Feeds :func:`app.excel.parse_workbook`'s ``learned_synonyms`` so a header
    seen once is recognized deterministically next time — no AI call.
    """

    with db_session() as conn:
        rows = conn.execute(
            "SELECT sheet_type, field, header_text FROM learned_header_synonyms"
        ).fetchall()
    grouped: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        by_field = grouped.setdefault(row["sheet_type"], {})
        by_field.setdefault(row["field"], []).append(row["header_text"])
    return {
        sheet_type: {field: tuple(values) for field, values in by_field.items()}
        for sheet_type, by_field in grouped.items()
    }


def _record_learned_header_synonyms(conn: Any, learned: list[tuple[str, str, str]]) -> None:
    now = utc_now()
    for sheet_type, field, header_text in learned:
        conn.execute(
            """
            INSERT INTO learned_header_synonyms
                (sheet_type, field, header_text, hit_count, created_at, last_used_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(sheet_type, field, header_text)
            DO UPDATE SET hit_count = hit_count + 1, last_used_at = excluded.last_used_at
            """,
            (sheet_type, field, header_text, now, now),
        )


def _classify_boq_row(
    row: dict[str, Any],
    sheet_type: str,
    previous_class: str | None,
) -> dict[str, Any]:
    """Load the shared audit classifier lazily to avoid an app import cycle."""

    # The benchmark classifier is intentionally pure and has no database
    # dependency. Keeping this import lazy lets lightweight catalog imports
    # start even when benchmark-only tooling is not imported yet.
    from benchmarks.row_audit import classify_row

    return classify_row(
        row,
        sheet_type=sheet_type,
        previous_class=previous_class,
    )


def _source_file_record(conn, sha256: str) -> dict[str, Any] | None:
    # ``content_sha256`` is stable across versions while ``sha256`` may be a
    # unique record checksum for a forced reimport/duplicate quotation.
    return row_dict(
        conn.execute(
            """
            SELECT * FROM source_files
            WHERE content_sha256 = ? OR sha256 = ?
            ORDER BY CASE WHEN lifecycle_status='ACTIVE' THEN 0 ELSE 1 END,
                     version_no DESC,
                     CASE WHEN sha256=? THEN 0 ELSE 1 END,
                     id DESC
            LIMIT 1
            """,
            (sha256, sha256, sha256),
        ).fetchone()
    )


def _link_catalog_source(
    conn,
    *,
    entity_type: str,
    product_id: int | None = None,
    labor_item_id: int | None = None,
    source_file_id: int | None = None,
    source_sheet_id: int | None = None,
    source_row_id: int | None = None,
    relation_type: str = "ingested",
) -> None:
    """Attach a catalog identity to the exact source location that supplied it.

    Identity rows are shared across workbooks, so direct ``source_file_id``
    columns on price/rate tables are insufficient for detail sheets or
    holdouts that contribute specs but intentionally no operational price.
    """

    if entity_type not in {"product", "labor"}:
        return
    if entity_type == "product" and not product_id:
        return
    if entity_type == "labor" and not labor_item_id:
        return
    if not source_file_id:
        return
    existing = conn.execute(
        """
        SELECT id FROM catalog_source_links
        WHERE entity_type=? AND COALESCE(product_id, 0)=COALESCE(?, 0)
          AND COALESCE(labor_item_id, 0)=COALESCE(?, 0)
          AND source_file_id=?
          AND COALESCE(source_sheet_id, 0)=COALESCE(?, 0)
          AND COALESCE(source_row_id, 0)=COALESCE(?, 0)
          AND relation_type=?
        LIMIT 1
        """,
        (
            entity_type,
            product_id,
            labor_item_id,
            source_file_id,
            source_sheet_id,
            source_row_id,
            relation_type,
        ),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """
        INSERT INTO catalog_source_links(
            entity_type, product_id, labor_item_id, source_file_id,
            source_sheet_id, source_row_id, relation_type, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            product_id,
            labor_item_id,
            source_file_id,
            source_sheet_id,
            source_row_id,
            relation_type,
            utc_now(),
        ),
    )


def _upsert_product(
    conn,
    description: str,
    code: str | None,
    brand: str | None,
    origin: str | None,
    unit: str | None,
    category: str | None = None,
    context_attributes: dict[str, Any] | None = None,
) -> int:
    attrs = technical_attributes(description)
    if context_attributes:
        # Sheet-level construction/voltage context is useful when the leaf
        # product name is terse (e.g. ``CXV 1x120``).  Keep the extracted
        # description attributes authoritative if both are present.
        for key, value in context_attributes.items():
            if value not in (None, "") and key not in attrs:
                attrs[key] = value
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


def _merge_product_context(
    conn,
    product_id: int,
    *,
    context_attributes: dict[str, Any] | None = None,
    category: str | None = None,
    brand: str | None = None,
    origin: str | None = None,
) -> None:
    """Enrich an existing identity with detail-sheet context.

    Supplier workbooks often repeat the same product in a terse ``TONG``
    summary and a detail sheet that carries construction/voltage/standard.
    The summary remains the canonical price source, while this merge preserves
    the richer identity metadata without creating a second product or price.
    Existing non-empty identity values win over weaker repeated context.
    """

    row = conn.execute(
        "SELECT category, brand, origin, technical_attributes_json "
        "FROM products WHERE id=?",
        (product_id,),
    ).fetchone()
    if not row:
        return
    current = loads(row["technical_attributes_json"], {})
    if not isinstance(current, dict):
        current = {}
    changed = False
    for key, value in (context_attributes or {}).items():
        if value not in (None, "") and current.get(key) in (None, ""):
            current[key] = value
            changed = True
    next_category = row["category"] or category
    next_brand = row["brand"] or brand
    next_origin = row["origin"] or origin
    if next_category != row["category"] or next_brand != row["brand"] or next_origin != row["origin"]:
        changed = True
    if changed:
        conn.execute(
            """
            UPDATE products
            SET category=?, brand=?, origin=?, technical_attributes_json=?
            WHERE id=?
            """,
            (
                next_category,
                next_brand,
                next_origin,
                dumps(current),
                product_id,
            ),
        )


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


def _supplier_name(
    path: Path,
    sheet: ParsedSheet,
    supplier_hint: str | None = None,
) -> str:
    """Infer a stable supplier label without making price policy assumptions."""

    if supplier_hint:
        return supplier_hint
    # Supplier names are often present in a workbook banner/header row rather
    # than the filename or the leaf table title. Scan only the bounded
    # title/context area, never the full data body, so a product description
    # cannot accidentally become a supplier label.
    banner = " ".join(
        str(value)
        for row in sheet.snapshot.rows[:20]
        for value in row
        if value not in (None, "")
    )
    text = normalize_text(
        f"{path.name} {sheet.metadata.get('title', '')} {banner}"
    )
    if "cadi sun" in text or "cadi-sun" in text or "cadi" in text:
        return "CADI-SUN"
    # Preserve a generic workbook family label for future suppliers.  This is
    # intentionally not used as a confidence signal.
    return path.stem.split("(")[0].strip() or "Unknown supplier"


def _sheet_context_attributes(sheet: ParsedSheet) -> dict[str, Any]:
    """Turn sheet-level product context into technical attributes."""

    values = [
        str(sheet.metadata.get(key) or "")
        for key in ("title", "construction", "voltage", "standard")
    ]
    attrs = technical_attributes(" ".join(value for value in values if value))
    # ``detect_sheet_metadata`` may expose voltage as free text that does not
    # match the attribute extractor; preserve it as a descriptive hint too.
    if sheet.metadata.get("voltage") and "voltage_context" not in attrs:
        attrs["voltage_context"] = sheet.metadata["voltage"]
    if sheet.metadata.get("construction") and "construction" not in attrs:
        attrs["construction"] = sheet.metadata["construction"]
    return attrs


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
    learned_synonyms = _load_learned_header_synonyms()
    newly_learned: list[tuple[str, str, str]] = []
    parsed = parse_workbook(
        path,
        learned_synonyms=learned_synonyms,
        on_ai_column_mapped=lambda sheet_type, field, header_text: newly_learned.append(
            (sheet_type, field, header_text)
        ),
    )
    manual_pricing_rules = load_manual_pricing_rules()
    supplier_hint = next(
        (
            "CADI-SUN"
            for parsed_sheet in parsed["sheets"]
            if any(
                marker in normalize_text(str(parsed_sheet.metadata.get("title") or ""))
                for marker in ("cadi sun", "cadi-sun")
            )
        ),
        None,
    )
    with db_session() as conn:
        if newly_learned:
            _record_learned_header_synonyms(conn, newly_learned)
        existing = _source_file_record(conn, parsed["sha256"])
        if existing and not force and not allow_duplicate:
            stats = source_file_detail(conn, int(existing["id"]))
            stats["skipped_duplicate"] = True
            return stats

        # ``source_files.sha256`` is intentionally unique for ordinary
        # idempotent imports.  A quotation upload may explicitly request a
        # second project from the same bytes (``allow_duplicate=True``); in
        # that case retain the content checksum in metadata while assigning a
        # unique record checksum so the database constraint is respected.
        content_sha256 = parsed["sha256"]
        duplicate_of_source_file_id: int | None = None
        supersedes_source_file_id: int | None = None
        record_sha256 = content_sha256
        version_no = 1
        if existing and force:
            # Never delete the old source: derived prices, labor rates, BOQ
            # rows and audit records must retain their original provenance.
            # A forced reimport is a new immutable source version.
            supersedes_source_file_id = int(existing["id"])
            version_no = int(
                conn.execute(
                    """
                    SELECT COALESCE(MAX(version_no), 0) + 1
                    FROM source_files
                    WHERE COALESCE(content_sha256, sha256)=?
                    """,
                    (content_sha256,),
                ).fetchone()[0]
            )
            record_sha256 = hashlib.sha256(
                f"dh-source-version:{content_sha256}:{uuid.uuid4().hex}".encode(
                    "utf-8"
                )
            ).hexdigest()
        elif existing and allow_duplicate:
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
            "supersedes_source_file_id": supersedes_source_file_id,
            "version_no": version_no,
        }
        cur = conn.execute(
            """
            INSERT INTO source_files(
                filename, storage_key, sha256, content_sha256, extension, size_bytes,
                detected_type, confirmed_type, metadata_json, parsing_version,
                processing_status, lifecycle_status, status_reason,
                supersedes_source_file_id, version_no, uploaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PROCESSING', 'ACTIVE', ?, ?, ?, ?)
            """,
            (
                display_filename,
                str(raw_path),
                record_sha256,
                content_sha256,
                parsed["extension"],
                path.stat().st_size,
                parsed["workbook_type"],
                confirmed_type,
                dumps(metadata),
                parsed["parsing_version"],
                "forced_reimport" if supersedes_source_file_id else None,
                supersedes_source_file_id,
                version_no,
                utc_now(),
            ),
        )
        source_file_id = int(cur.lastrowid)
        if supersedes_source_file_id:
            conn.execute(
                """
                UPDATE source_files
                SET lifecycle_status='SUPERSEDED',
                    status_reason='superseded_by_forced_reimport',
                    archived_at=COALESCE(archived_at, ?),
                    superseded_by_source_file_id=?
                WHERE id=?
                """,
                (utc_now(), source_file_id, supersedes_source_file_id),
            )

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
            "price_observations": 0,
            "manual_pricing_rules_applied": 0,
            "labor_items": 0,
            "labor_rates": 0,
            "boq_items": 0,
            "priceable_rows": 0,
            "non_priceable_rows": 0,
            "row_classifications": {},
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
            previous_line_class: str | None = None
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
                            "metadata": sheet.metadata,
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
                line_audit: dict[str, Any] | None = None
                if sheet.detected_type in _BOQ_SHEET_TYPES:
                    line_audit = _classify_boq_row(
                        row,
                        sheet.detected_type,
                        previous_line_class,
                    )
                    classification = str(line_audit["classification"])
                    stats["row_classifications"][classification] = (
                        int(stats["row_classifications"].get(classification, 0)) + 1
                    )
                    if line_audit.get("priceable"):
                        stats["priceable_rows"] += 1
                    else:
                        stats["non_priceable_rows"] += 1
                    # Retain structural context for the next heading without
                    # letting ordinary line items erase the current section.
                    if classification in {"SECTION", "SUBSECTION"}:
                        previous_line_class = classification
                    elif classification in {"TOTAL", "SUBTOTAL"}:
                        previous_line_class = None
                if row["row_kind"] != "data":
                    continue

                fields = row["fields"]
                description = str(fields.get("description") or "").strip()
                if not description:
                    continue
                sheet_type = sheet.detected_type
                if (
                    sheet_type == "SUPPLIER_PRICE"
                    and not is_canonical_price_sheet
                ):
                    # Detail sheets repeat the canonical ``TONG`` rows but
                    # carry richer construction/voltage/standard metadata.
                    # Enrich product identity from them while deliberately
                    # avoiding duplicate operational price rows.
                    context_attrs = _sheet_context_attributes(sheet)
                    attrs = technical_attributes(description)
                    for key, value in context_attrs.items():
                        if value not in (None, "") and key not in attrs:
                            attrs[key] = value
                    product_id = _upsert_product(
                        conn,
                        description,
                        fields.get("code"),
                        _supplier_name(path, sheet, supplier_hint),
                        fields.get("origin"),
                        fields.get("unit") or "m",
                        attrs.get("category"),
                        context_attrs,
                    )
                    _link_catalog_source(
                        conn,
                        entity_type="product",
                        product_id=product_id,
                        source_file_id=source_file_id,
                        source_sheet_id=source_sheet_id,
                        source_row_id=source_row_id,
                        relation_type="detail_specification",
                    )
                    _merge_product_context(
                        conn,
                        product_id,
                        context_attributes=attrs,
                        category=attrs.get("category"),
                        brand=_supplier_name(path, sheet, supplier_hint),
                        origin=fields.get("origin"),
                    )
                    continue
                if sheet_type == "SUPPLIER_PRICE" and is_canonical_price_sheet:
                    observations = fields.get("price_observations") or []
                    supplier = _supplier_name(path, sheet, supplier_hint)
                    context_attrs = _sheet_context_attributes(sheet)
                    attrs = technical_attributes(description)
                    for key, value in context_attrs.items():
                        if value not in (None, "") and key not in attrs:
                            attrs[key] = value
                    category = attrs.get("category")
                    product_family = attrs.get("cable_family")
                    selected_rule = select_manual_pricing_rule(
                        manual_pricing_rules,
                        supplier=supplier,
                        category=category,
                        product_family=product_family,
                        effective_date=parsed["effective_date"],
                    )
                    selected_observation, selection_meta = select_price_observation(
                        observations,
                        rule=selected_rule,
                    )
                    # Backwards-compatible fallback for custom/legacy parser
                    # output that exposes only ``list_price``.
                    if selected_observation is None and fields.get("list_price") is not None:
                        selected_observation = {
                            "price_type": "supplier_list",
                            "tax_mode": "ex_vat",
                            "amount": fields.get("list_price"),
                            "discount_rate": 0.0,
                            "column": (sheet.mapping.get("list_price") or 0) + 1,
                        }
                        selection_meta = {"selection": "legacy_base_list_fallback"}
                    net_price = (
                        selected_observation.get("amount")
                        if selected_observation
                        else None
                    )
                    if net_price is None or net_price < 0:
                        continue
                    product_id = _upsert_product(
                        conn,
                        description,
                        fields.get("code"),
                        supplier,
                        fields.get("origin"),
                        fields.get("unit") or "m",
                        category,
                        context_attrs,
                    )
                    _link_catalog_source(
                        conn,
                        entity_type="product",
                        product_id=product_id,
                        source_file_id=source_file_id,
                        source_sheet_id=source_sheet_id,
                        source_row_id=source_row_id,
                        relation_type="price_observation",
                    )
                    stats["products"] += 1
                    if not exclude_prices:
                        # Retain every source list/VAT/discount tier as an
                        # immutable observation. Only the selected observation
                        # becomes the operational product_prices row.
                        base_ex = next(
                            (
                                obs.get("amount")
                                for obs in observations
                                if obs.get("tax_mode") == "ex_vat"
                                and not obs.get("discount_rate")
                            ),
                            None,
                        )
                        if base_ex is None:
                            base_ex = fields.get("list_price")
                        all_observations = list(observations)
                        if selected_observation.get("calculated"):
                            all_observations.append(selected_observation)
                        observation_ids: list[int] = []
                        for observation in all_observations:
                            amount = observation.get("amount")
                            if amount is None or amount < 0:
                                continue
                            obs_tax_mode = observation.get("tax_mode") or "ex_vat"
                            o_cur = conn.execute(
                                """
                                INSERT INTO price_observations(
                                    product_id, source_file_id, source_sheet_id,
                                    source_row_id, supplier, observation_type,
                                    list_price, discount, net_price,
                                    tax_mode, price_basis, effective_date,
                                    confidence, context_json, calc_json,
                                    created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    product_id,
                                    source_file_id,
                                    source_sheet_id,
                                    source_row_id,
                                    supplier,
                                    observation.get("price_type") or "supplier_list",
                                    base_ex,
                                    observation.get("discount_rate"),
                                    amount,
                                    obs_tax_mode,
                                    "gross" if obs_tax_mode == "inc_vat" else "net",
                                    parsed["effective_date"],
                                    0.98 if parsed["effective_date"] else 0.9,
                                    dumps(
                                        {
                                            "sheet_metadata": sheet.metadata,
                                            "source_column": observation.get("column"),
                                            "selected": observation is selected_observation,
                                        }
                                    ),
                                    dumps(
                                        selection_meta
                                        if observation is selected_observation
                                        else {}
                                    ),
                                    utc_now(),
                                ),
                            )
                            observation_ids.append(int(o_cur.lastrowid))
                            stats["price_observations"] += 1
                        selected_obs_index = next(
                            (
                                index
                                for index, observation in enumerate(all_observations)
                                if observation is selected_observation
                            ),
                            None,
                        )
                        selected_observation_id = (
                            observation_ids[selected_obs_index]
                            if selected_obs_index is not None
                            and selected_obs_index < len(observation_ids)
                            else None
                        )
                        selected_discount = (
                            selected_observation.get("discount_rate") or 0.0
                        )
                        if selected_rule is not None:
                            stats["manual_pricing_rules_applied"] += 1
                        conn.execute(
                            """
                            INSERT INTO product_prices(
                                product_id, source_file_id, source_sheet_id,
                                source_row_id, supplier, list_price, discount,
                                net_price, tax_mode, effective_date, confidence,
                                calc_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                product_id,
                                source_file_id,
                                source_sheet_id,
                                source_row_id,
                                supplier,
                                base_ex,
                                selected_discount,
                                net_price,
                                selected_observation.get("tax_mode") or "ex_vat",
                                parsed["effective_date"],
                                0.98 if parsed["effective_date"] else 0.9,
                                dumps(
                                    {
                                        "formula": "selected source price observation",
                                        "selection": selection_meta,
                                        "observation_id": selected_observation_id,
                                        "effective_date_inferred": parsed[
                                            "effective_date_inferred"
                                        ],
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
                    _link_catalog_source(
                        conn,
                        entity_type="labor",
                        labor_item_id=item_id,
                        source_file_id=source_file_id,
                        source_sheet_id=source_sheet_id,
                        source_row_id=source_row_id,
                        relation_type="labor_rate" if fields.get("labor_price") is not None else "identity",
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
                            material_price, labor_price, status, line_class,
                            status_reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            line_audit.get("classification") if line_audit else None,
                            line_audit.get("reason") if line_audit else None,
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
                            _link_catalog_source(
                                conn,
                                entity_type="product",
                                product_id=product_id,
                                source_file_id=source_file_id,
                                source_sheet_id=source_sheet_id,
                                source_row_id=source_row_id,
                                relation_type="historical_price",
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
                            conn.execute(
                                """
                                INSERT INTO price_observations(
                                    product_id, source_file_id, source_sheet_id,
                                    source_row_id, source_project_id,
                                    supplier, observation_type, net_price,
                                    tax_mode, price_basis, effective_date,
                                    confidence, context_json, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, 'historical_boq', ?,
                                          'ex_vat', 'net', ?, 0.82, ?, ?)
                                """,
                                (
                                    product_id,
                                    source_file_id,
                                    source_sheet_id,
                                    source_row_id,
                                    project_id,
                                    "Historical quotation",
                                    material_price,
                                    parsed["effective_date"],
                                    dumps({"boq_item_id": boq_id, "source_type": "historical_boq"}),
                                    utc_now(),
                                ),
                            )
                            stats["products"] += 1
                            stats["product_prices"] += 1
                            stats["price_observations"] += 1
                        labor_price = fields.get("labor_price")
                        if labor_price is not None and labor_price > 0:
                            labor_id = _upsert_labor_item(
                                conn,
                                description,
                                fields.get("code"),
                                fields.get("unit"),
                                attrs.get("category"),
                            )
                            _link_catalog_source(
                                conn,
                                entity_type="labor",
                                labor_item_id=labor_id,
                                source_file_id=source_file_id,
                                source_sheet_id=source_sheet_id,
                                source_row_id=source_row_id,
                                relation_type="historical_rate",
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
        # Catalog/price selectors keep process-local caches for large BOQs.
        # Invalidate them after a successful import so a subsequent run sees
        # newly ingested identities and observations immediately.
        try:
            from .pricing import clear_runtime_caches

            clear_runtime_caches()
        except Exception:
            # Ingestion is also used by lightweight parser/benchmark contexts
            # where importing the pricing module is intentionally avoided.
            pass
        return stats


def _decode_json_field(value: Any, default: Any) -> Any:
    decoded = loads(value, default)
    return decoded


def _source_ref_payload(conn, source_file_id: int, *, sheet_id: int | None = None, row_id: int | None = None) -> dict[str, Any]:
    """Return a compact, stable source pointer for catalog/detail responses."""

    source = conn.execute(
        """
        SELECT filename, lifecycle_status, status_reason, version_no
        FROM source_files WHERE id=?
        """,
        (source_file_id,),
    ).fetchone()
    result: dict[str, Any] = {"file_id": source_file_id}
    if source:
        result.update(
            {
                "filename": source["filename"],
                "status": source["lifecycle_status"] or "ACTIVE",
                "status_reason": source["status_reason"],
                "version_no": source["version_no"] or 1,
            }
        )
    if sheet_id is not None:
        row = conn.execute(
            "SELECT sheet_name, sheet_index FROM source_sheets WHERE id=?",
            (sheet_id,),
        ).fetchone()
        if row:
            result.update(
                {
                    "sheet_id": sheet_id,
                    "sheet_name": row["sheet_name"],
                    "sheet_index": row["sheet_index"],
                }
            )
    if row_id is not None:
        row = conn.execute(
            "SELECT row_no FROM source_rows WHERE id=?",
            (row_id,),
        ).fetchone()
        if row:
            result.update({"row_id": row_id, "row_no": row["row_no"]})
    return result


def source_file_detail(
    conn,
    source_file_id: int,
    *,
    include_rows: bool = True,
    limit: int = 500,
) -> dict[str, Any]:
    """Return everything the importer derived from one workbook.

    The raw workbook remains the source of truth; this response exposes the
    parsed sheets/mappings/rows and all catalog/BOQ records carrying a
    provenance pointer back to this source. ``limit`` bounds payload size
    while ``summary`` always reports complete counts.
    """

    source = conn.execute(
        "SELECT * FROM source_files WHERE id = ?", (source_file_id,)
    ).fetchone()
    if not source:
        raise KeyError(source_file_id)
    limit = max(1, min(int(limit or 500), 5000))
    result = dict(source)
    # Never return the internal absolute storage path in an API payload.
    storage_key = result.pop("storage_key", None)
    result["raw_available"] = bool(storage_key)
    result["metadata"] = _decode_json_field(result.pop("metadata_json", "{}"), {})
    if not isinstance(result["metadata"], dict):
        result["metadata"] = {}
    # Keep server-local paths out of source detail responses.  The raw
    # workbook is represented by ``raw_available`` and resolved internally
    # for reprocess/delete operations.
    result["metadata"].pop("original_path", None)
    result["lifecycle"] = {
        "status": result.get("lifecycle_status") or "ACTIVE",
        "reason": result.get("status_reason"),
        "archived_at": result.get("archived_at"),
        "version_no": result.get("version_no") or 1,
        "supersedes_source_file_id": result.get("supersedes_source_file_id"),
        "superseded_by_source_file_id": result.get("superseded_by_source_file_id"),
    }

    sheet_rows = conn.execute(
        """
        SELECT ss.*, COUNT(sr.id) AS row_count,
               COALESCE(SUM(CASE WHEN sr.row_kind='data' THEN 1 ELSE 0 END), 0) AS data_row_count
        FROM source_sheets ss
        LEFT JOIN source_rows sr ON sr.source_sheet_id=ss.id
        WHERE ss.source_file_id=?
        GROUP BY ss.id ORDER BY ss.sheet_index
        """,
        (source_file_id,),
    ).fetchall()
    sheets: list[dict[str, Any]] = []
    metadata_warnings = result["metadata"].get("warnings")
    warnings: list[Any] = (
        list(metadata_warnings)
        if isinstance(metadata_warnings, (list, tuple, set))
        else ([metadata_warnings] if metadata_warnings else [])
    )
    for row in sheet_rows:
        item = dict(row)
        item["mapping"] = _decode_json_field(item.pop("mapping_json", "{}"), {})
        item["metadata"] = _decode_json_field(item.pop("metadata_json", "{}"), {})
        item["warnings"] = list(item["metadata"].get("warnings") or [])
        warnings.extend(item["warnings"])
        if include_rows:
            # ``limit`` is a workbook-level payload budget, not a per-sheet
            # multiplier.  Allocate a small, deterministic sample to every
            # sheet so large workbooks remain inspectable without returning
            # tens of thousands of raw cells in one response.
            sheet_position = len(sheets)
            sheet_total = len(sheet_rows)
            base_limit, remainder = divmod(limit, max(1, sheet_total))
            row_limit = base_limit + (1 if sheet_position < remainder else 0)
            raw_rows = conn.execute(
                """
                SELECT id, row_no, raw_cells_json, row_kind, parse_warnings_json
                FROM source_rows
                WHERE source_sheet_id=?
                ORDER BY row_no LIMIT ?
                """,
                (row["id"], row_limit),
            ).fetchall()
            item["rows"] = []
            for raw in raw_rows:
                parsed_row = dict(raw)
                parsed_row["raw_cells"] = _decode_json_field(
                    parsed_row.pop("raw_cells_json", "{}"), {}
                )
                parsed_row["warnings"] = _decode_json_field(
                    parsed_row.pop("parse_warnings_json", "[]"), []
                )
                item["rows"].append(parsed_row)
                warnings.extend(parsed_row["warnings"] or [])
        sheets.append(item)
    result["sheets"] = sheets

    # Resolve identity ids from both the explicit bridge and legacy direct
    # provenance columns so older databases still produce complete details.
    product_ids = {
        int(row[0])
        for row in conn.execute(
            """
            SELECT product_id FROM catalog_source_links
            WHERE source_file_id=? AND product_id IS NOT NULL
            UNION SELECT product_id FROM product_prices
            WHERE source_file_id=? AND product_id IS NOT NULL
            UNION SELECT product_id FROM price_observations
            WHERE source_file_id=? AND product_id IS NOT NULL
            UNION SELECT matched_product_id FROM boq_items
            WHERE source_file_id=? AND matched_product_id IS NOT NULL
            """,
            (source_file_id, source_file_id, source_file_id, source_file_id),
        ).fetchall()
    }
    labor_ids = {
        int(row[0])
        for row in conn.execute(
            """
            SELECT labor_item_id FROM catalog_source_links
            WHERE source_file_id=? AND labor_item_id IS NOT NULL
            UNION SELECT labor_item_id FROM labor_rates
            WHERE source_file_id=? AND labor_item_id IS NOT NULL
            UNION SELECT matched_labor_item_id FROM boq_items
            WHERE source_file_id=? AND matched_labor_item_id IS NOT NULL
            """,
            (source_file_id, source_file_id, source_file_id),
        ).fetchall()
    }

    def _entity_provenance(entity_type: str, entity_id: int) -> list[dict[str, Any]]:
        """Resolve bridge links plus legacy direct provenance for one entity.

        Databases created before ``catalog_source_links`` was introduced still
        contain useful source pointers on ``product_prices``,
        ``price_observations`` and ``labor_rates``.  Include those references
        so the workbook detail view remains truthful after a schema upgrade.
        """

        if entity_type == "product":
            refs = conn.execute(
                """
                SELECT source_file_id, source_sheet_id, source_row_id, relation_type
                FROM catalog_source_links
                WHERE entity_type='product' AND product_id=? AND source_file_id=?
                ORDER BY id
                """,
                (entity_id, source_file_id),
            ).fetchall()
            refs = [
                *refs,
                *conn.execute(
                    """
                    SELECT source_file_id, source_sheet_id, source_row_id,
                           'price' AS relation_type
                    FROM product_prices
                    WHERE product_id=? AND source_file_id=?
                    ORDER BY id
                    """,
                    (entity_id, source_file_id),
                ).fetchall(),
                *conn.execute(
                    """
                    SELECT source_file_id, source_sheet_id, source_row_id,
                           'observation' AS relation_type
                    FROM price_observations
                    WHERE product_id=? AND source_file_id=?
                    ORDER BY id
                    """,
                    (entity_id, source_file_id),
                ).fetchall(),
            ]
        else:
            refs = conn.execute(
                """
                SELECT source_file_id, source_sheet_id, source_row_id, relation_type
                FROM catalog_source_links
                WHERE entity_type='labor' AND labor_item_id=? AND source_file_id=?
                ORDER BY id
                """,
                (entity_id, source_file_id),
            ).fetchall()
            refs = [
                *refs,
                *conn.execute(
                    """
                    SELECT source_file_id, source_sheet_id, source_row_id,
                           'labor_rate' AS relation_type
                    FROM labor_rates
                    WHERE labor_item_id=? AND source_file_id=?
                    ORDER BY id
                    """,
                    (entity_id, source_file_id),
                ).fetchall(),
            ]

        result: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for ref in refs:
            pointer = _source_ref_payload(
                conn,
                int(ref["source_file_id"]),
                sheet_id=ref["source_sheet_id"],
                row_id=ref["source_row_id"],
            )
            pointer["relation_type"] = ref["relation_type"]
            key = (
                pointer.get("file_id"),
                pointer.get("sheet_id"),
                pointer.get("row_id"),
                pointer.get("relation_type"),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(pointer)
        return result

    def _product_payload(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["technical_attributes"] = _decode_json_field(
            item.pop("technical_attributes_json", "{}"), {}
        )
        item["aliases"] = _decode_json_field(item.pop("aliases_json", "[]"), [])
        item["provenance"] = _entity_provenance("product", int(item["id"]))
        return item

    products = [
        _product_payload(row)
        for row in conn.execute(
            f"SELECT * FROM products WHERE id IN ({','.join('?' for _ in product_ids)}) ORDER BY normalized_name",
            tuple(product_ids),
        ).fetchall()
    ] if product_ids else []

    def _labor_payload(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["technical_attributes"] = _decode_json_field(
            item.pop("technical_attributes_json", "{}"), {}
        )
        item["provenance"] = _entity_provenance("labor", int(item["id"]))
        return item

    labor = [
        _labor_payload(row)
        for row in conn.execute(
            f"SELECT * FROM labor_items WHERE id IN ({','.join('?' for _ in labor_ids)}) ORDER BY normalized_name",
            tuple(labor_ids),
        ).fetchall()
    ] if labor_ids else []

    price_rows = conn.execute(
        """
        SELECT pp.*, p.normalized_name, p.product_code, p.category,
               sf.filename AS source_filename, ss.sheet_name,
               sr.row_no
        FROM product_prices pp
        JOIN products p ON p.id=pp.product_id
        LEFT JOIN source_files sf ON sf.id=pp.source_file_id
        LEFT JOIN source_sheets ss ON ss.id=pp.source_sheet_id
        LEFT JOIN source_rows sr ON sr.id=pp.source_row_id
        WHERE pp.source_file_id=?
        ORDER BY COALESCE(pp.effective_date, pp.created_at) DESC, pp.id DESC
        LIMIT ?
        """,
        (source_file_id, limit),
    ).fetchall()
    prices = []
    for row in price_rows:
        item = dict(row)
        item["calc"] = _decode_json_field(item.pop("calc_json", "{}"), {})
        item["provenance"] = _source_ref_payload(
            conn, source_file_id, sheet_id=item.get("source_sheet_id"), row_id=item.get("source_row_id")
        )
        prices.append(item)

    observation_rows = conn.execute(
        """
        SELECT po.*, p.normalized_name, p.product_code, p.category,
               sf.filename AS source_filename, ss.sheet_name, sr.row_no
        FROM price_observations po
        JOIN products p ON p.id=po.product_id
        LEFT JOIN source_files sf ON sf.id=po.source_file_id
        LEFT JOIN source_sheets ss ON ss.id=po.source_sheet_id
        LEFT JOIN source_rows sr ON sr.id=po.source_row_id
        WHERE po.source_file_id=?
        ORDER BY COALESCE(po.effective_date, po.created_at) DESC, po.id DESC
        LIMIT ?
        """,
        (source_file_id, limit),
    ).fetchall()
    observations = []
    for row in observation_rows:
        item = dict(row)
        item["context"] = _decode_json_field(item.pop("context_json", "{}"), {})
        item["calc"] = _decode_json_field(item.pop("calc_json", "{}"), {})
        item["provenance"] = _source_ref_payload(
            conn, source_file_id, sheet_id=item.get("source_sheet_id"), row_id=item.get("source_row_id")
        )
        observations.append(item)

    rate_rows = conn.execute(
        """
        SELECT lr.*, li.normalized_name, li.code, li.category,
               sf.filename AS source_filename, ss.sheet_name, sr.row_no
        FROM labor_rates lr
        JOIN labor_items li ON li.id=lr.labor_item_id
        LEFT JOIN source_files sf ON sf.id=lr.source_file_id
        LEFT JOIN source_sheets ss ON ss.id=lr.source_sheet_id
        LEFT JOIN source_rows sr ON sr.id=lr.source_row_id
        WHERE lr.source_file_id=?
        ORDER BY COALESCE(lr.effective_date, lr.created_at) DESC, lr.id DESC
        LIMIT ?
        """,
        (source_file_id, limit),
    ).fetchall()
    rates = []
    for row in rate_rows:
        item = dict(row)
        item["policy"] = _decode_json_field(item.pop("policy_json", "{}"), {})
        item["provenance"] = _source_ref_payload(
            conn, source_file_id, sheet_id=item.get("source_sheet_id"), row_id=item.get("source_row_id")
        )
        rates.append(item)

    boq_rows = conn.execute(
        """
        SELECT bi.*, p.normalized_name AS matched_product_name,
               li.normalized_name AS matched_labor_name
        FROM boq_items bi
        LEFT JOIN products p ON p.id=bi.matched_product_id
        LEFT JOIN labor_items li ON li.id=bi.matched_labor_item_id
        WHERE bi.source_file_id=?
        ORDER BY bi.id LIMIT ?
        """,
        (source_file_id, limit),
    ).fetchall()
    boq = []
    for row in boq_rows:
        item = dict(row)
        item["raw_cells"] = _decode_json_field(item.pop("raw_cells_json", "{}"), {})
        item["material_source"] = _decode_json_field(
            item.pop("material_source_json", "{}"), {}
        )
        item["labor_source"] = _decode_json_field(item.pop("labor_source_json", "{}"), {})
        item["alternatives"] = _decode_json_field(item.pop("alternatives_json", "[]"), [])
        boq.append(item)

    counts = {
        "sheets": len(sheet_rows),
        "rows": int(
            conn.execute(
                """
                SELECT COUNT(*) FROM source_rows sr
                JOIN source_sheets ss ON ss.id=sr.source_sheet_id
                WHERE ss.source_file_id=?
                """,
                (source_file_id,),
            ).fetchone()[0]
        ),
        "data_rows": int(
            conn.execute(
                """
                SELECT COUNT(*) FROM source_rows sr
                JOIN source_sheets ss ON ss.id=sr.source_sheet_id
                WHERE ss.source_file_id=? AND sr.row_kind='data'
                """,
                (source_file_id,),
            ).fetchone()[0]
        ),
        "products": len(product_ids),
        "prices": int(
            conn.execute(
                "SELECT COUNT(*) FROM product_prices WHERE source_file_id=?",
                (source_file_id,),
            ).fetchone()[0]
        ),
        "price_observations": int(
            conn.execute(
                "SELECT COUNT(*) FROM price_observations WHERE source_file_id=?",
                (source_file_id,),
            ).fetchone()[0]
        ),
        "labor": len(labor_ids),
        "labor_rates": int(
            conn.execute(
                "SELECT COUNT(*) FROM labor_rates WHERE source_file_id=?",
                (source_file_id,),
            ).fetchone()[0]
        ),
        "boq": int(
            conn.execute(
                "SELECT COUNT(*) FROM boq_items WHERE source_file_id=?",
                (source_file_id,),
            ).fetchone()[0]
        ),
        "projects": int(
            conn.execute(
                "SELECT COUNT(*) FROM projects WHERE source_file_id=?",
                (source_file_id,),
            ).fetchone()[0]
        ),
        "catalog_links": int(
            conn.execute(
                "SELECT COUNT(*) FROM catalog_source_links WHERE source_file_id=?",
                (source_file_id,),
            ).fetchone()[0]
        ),
    }
    # Preserve ordering while avoiding duplicate warning strings from sheet
    # and row metadata.
    result["warnings"] = list(dict.fromkeys(str(w) for w in warnings if str(w).strip()))
    result["summary"] = counts
    result["products"] = products[:limit]
    result["prices"] = prices
    result["price_observations"] = observations
    result["labor"] = labor[:limit]
    result["labor_rates"] = rates
    result["boq"] = boq
    result["provenance"] = {
        "source_file_id": source_file_id,
        "derived_counts": counts,
        "retained_on_archive": True,
    }
    result["referenced_count"] = int(
        sum(
            counts.get(key, 0)
            for key in (
                "projects",
                "prices",
                "price_observations",
                "labor_rates",
                "boq",
                "catalog_links",
            )
        )
    )
    result["processing"] = {
        "status": result.get("processing_status") or "PENDING",
        "warnings": len(result["warnings"]),
    }
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
