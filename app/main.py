from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .ai import ai_provider
from .chatbot import (
    ChatbotError,
    DEFAULT_GREETING,
    chatbot_status,
    generate_reply_with_metadata,
    register_upload,
)
from .config import EXPORT_DIR, RAW_DIR, ROOT_DIR, STORAGE_DIR, ensure_directories, settings
from .db import db_session, dumps, init_db, loads, utc_now
from .excel import parse_workbook
from .export import export_project
from .ingest import ingest_directory, ingest_workbook, source_file_detail
from .pricing import (
    catalog_stats,
    clear_runtime_caches,
    choose_product_price,
    get_project_result,
    review_item,
    run_pricing,
    serialize_boq_item,
)
from .normalize import parse_number


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Nền tảng nhập Excel, matching vật tư/nhân công và báo giá M&E.",
)

STATIC_DIR = ROOT_DIR / "app" / "static"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


CLASSIFICATION_MAP = {
    "supplier_price_list": "SUPPLIER_PRICE",
    "supplier_price": "SUPPLIER_PRICE",
    "labor": "LABOR",
    "historical_boq": "HISTORICAL_BOQ",
    "new_boq": "NEW_BOQ",
    "panel_bom": "PANEL_BOM",
    "mixed": "MIXED",
    "unknown": "UNKNOWN",
}


@app.on_event("startup")
def startup() -> None:
    ensure_directories()
    init_db()


def _classification(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    return CLASSIFICATION_MAP.get(normalized, value.strip().upper())


def _source_payload(conn, source_row: Any) -> dict[str, Any]:
    result = dict(source_row)
    # ``storage_key`` is an internal absolute filesystem path. Do not expose
    # it through list/detail APIs; callers only need to know whether the raw
    # workbook is retained, while reprocess/export resolve it server-side.
    storage_key = result.pop("storage_key", None)
    result["raw_available"] = bool(storage_key)
    result["metadata"] = loads(result.pop("metadata_json", "{}"), {})
    if not isinstance(result["metadata"], dict):
        result["metadata"] = {}
    # ``original_path`` is useful for server-side diagnostics but is an
    # absolute local filesystem path and must never be exposed to API clients.
    result["metadata"].pop("original_path", None)
    source_id = int(result["id"])
    sheet_rows = conn.execute(
        """
        SELECT ss.id, ss.sheet_name, ss.sheet_index, ss.detected_type,
               ss.header_row, ss.table_range, ss.mapping_json,
               COUNT(sr.id) AS row_count,
               COALESCE(SUM(CASE WHEN sr.row_kind='data' THEN 1 ELSE 0 END), 0) AS data_row_count
        FROM source_sheets ss
        LEFT JOIN source_rows sr ON sr.source_sheet_id=ss.id
        WHERE ss.source_file_id=?
        GROUP BY ss.id ORDER BY ss.sheet_index
        """,
        (source_id,),
    ).fetchall()
    result["sheets"] = [
        {
            **dict(row),
            "mapping": loads(row["mapping_json"], {}),
        }
        for row in sheet_rows
    ]
    result["sheet_count"] = len(sheet_rows)
    result["row_count"] = sum(int(row["row_count"] or 0) for row in sheet_rows)
    result["extracted_rows"] = sum(int(row["data_row_count"] or 0) for row in sheet_rows)
    metadata_warnings = result["metadata"].get("warnings", [])
    result["warning_count"] = len(metadata_warnings) if isinstance(
        metadata_warnings, (list, tuple, set)
    ) else (1 if metadata_warnings else 0)
    counts = _source_reference_counts(conn, source_id)
    result["summary"] = {
        "sheets": result["sheet_count"],
        "rows": result["row_count"],
        "data_rows": result["extracted_rows"],
        "products": counts["products"],
        "prices": counts["product_prices"],
        "price_observations": counts["price_observations"],
        "labor": counts["labor_items"],
        "labor_rates": counts["labor_rates"],
        "boq": counts["boq_items"],
        "projects": counts["projects"],
        "warnings": result["warning_count"],
    }
    result["lifecycle"] = {
        "status": result.get("lifecycle_status") or "ACTIVE",
        "reason": result.get("status_reason"),
        "archived_at": result.get("archived_at"),
        "version_no": result.get("version_no") or 1,
        "supersedes_source_file_id": result.get("supersedes_source_file_id"),
        "superseded_by_source_file_id": result.get("superseded_by_source_file_id"),
    }
    result["reference_counts"] = counts
    result["referenced_count"] = counts["derived_references"]
    result["processing"] = {
        "status": result.get("processing_status") or "PENDING",
        "warnings": result["warning_count"],
    }
    return result


def _source_reference_counts(conn, source_id: int) -> dict[str, int]:
    """Count records that must retain this source's provenance."""

    scalar_queries = {
        "projects": "SELECT COUNT(*) FROM projects WHERE source_file_id=?",
        "product_prices": "SELECT COUNT(*) FROM product_prices WHERE source_file_id=?",
        "price_observations": "SELECT COUNT(*) FROM price_observations WHERE source_file_id=?",
        "labor_rates": "SELECT COUNT(*) FROM labor_rates WHERE source_file_id=?",
        "boq_items": "SELECT COUNT(*) FROM boq_items WHERE source_file_id=?",
        "catalog_links": "SELECT COUNT(*) FROM catalog_source_links WHERE source_file_id=?",
        "version_links": """
            SELECT COUNT(*) FROM source_files
            WHERE superseded_by_source_file_id=?
               OR supersedes_source_file_id=?
        """,
    }
    result = {
        name: int(
            conn.execute(
                sql,
                (source_id, source_id)
                if name == "version_links"
                else (source_id,),
            ).fetchone()[0]
        )
        for name, sql in scalar_queries.items()
    }
    result["products"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT product_id FROM catalog_source_links
                WHERE source_file_id=? AND product_id IS NOT NULL
                UNION SELECT product_id FROM product_prices
                WHERE source_file_id=? AND product_id IS NOT NULL
                UNION SELECT product_id FROM price_observations
                WHERE source_file_id=? AND product_id IS NOT NULL
            )
            """,
            (source_id, source_id, source_id),
        ).fetchone()[0]
    )
    result["labor_items"] = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT labor_item_id FROM catalog_source_links
                WHERE source_file_id=? AND labor_item_id IS NOT NULL
                UNION SELECT labor_item_id FROM labor_rates
                WHERE source_file_id=? AND labor_item_id IS NOT NULL
            )
            """,
            (source_id, source_id),
        ).fetchone()[0]
    )
    result["derived_references"] = (
        result["projects"]
        + result["product_prices"]
        + result["price_observations"]
        + result["labor_rates"]
        + result["boq_items"]
        + result["catalog_links"]
        + result["version_links"]
    )
    return result


def _source_lifecycle_payload(conn, source_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM source_files WHERE id=?", (source_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy nguồn dữ liệu")
    payload = _source_payload(conn, row)
    payload["impact"] = {
        "archive_preserves_provenance": True,
        "excluded_from_new_pricing": payload["lifecycle"]["status"] != "ACTIVE",
        "references": payload["reference_counts"],
    }
    return payload


def _project_payload(conn, project_row: Any) -> dict[str, Any]:
    project = dict(project_row)
    project["metadata"] = loads(project.pop("metadata_json", "{}"), {})
    latest = conn.execute(
        "SELECT * FROM pricing_runs WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (project["id"],),
    ).fetchone()
    metrics = loads(latest["metrics_json"], {}) if latest else {}
    source = conn.execute(
        "SELECT filename FROM source_files WHERE id=?", (project.get("source_file_id"),)
    ).fetchone()
    project.update(
        {
            "status": latest["status"] if latest else ("draft" if project["metadata"].get("document_type") == "NEW_BOQ" else "imported"),
            "run_id": latest["id"] if latest else None,
            "stats": metrics,
            "total_rows": metrics.get("total_items", conn.execute("SELECT COUNT(*) FROM boq_items WHERE project_id=?", (project["id"],)).fetchone()[0]),
            "material_matched": metrics.get("material_matched", 0),
            "labor_matched": metrics.get("labor_matched", 0),
            "review_required": metrics.get("review_required", 0),
            "no_match": metrics.get("no_match", 0),
            # Coverage for a completed workbook includes deliberate
            # non-priceable/zero-quantity rows that were handled as IGNORED.
            # Keep the raw auto-only metric available under its original key.
            "auto_coverage": metrics.get(
                "handled_coverage",
                metrics.get("auto_coverage"),
            ),
            "handled_coverage": metrics.get("handled_coverage"),
            "source_filename": source["filename"] if source else "",
            "updated_at": (latest["finished_at"] if latest else project["created_at"]),
        }
    )
    return project


def _save_upload(upload: UploadFile) -> Path:
    filename = Path(upload.filename or "upload.xlsx").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".xls", ".xlsx"}:
        raise HTTPException(status_code=400, detail=f"Chỉ hỗ trợ .xls/.xlsx: {filename}")
    incoming = STORAGE_DIR / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    # Temporary filename avoids collisions while retaining the original suffix
    # required by the parser.
    # Keep the original basename so filename-based classification and inferred
    # effective dates remain meaningful. Each upload gets an isolated temp
    # directory to avoid collisions between simultaneous requests.
    temp_dir = Path(tempfile.mkdtemp(prefix="upload-", dir=incoming))
    path = temp_dir / filename
    handle = path.open("wb")
    total_bytes = 0
    too_large = False
    try:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_UPLOAD_BYTES:
                too_large = True
                break
            handle.write(chunk)
    except Exception:
        # Ensure a failed read/write cannot leave an attacker-controlled
        # temporary workbook behind on disk.
        handle.close()
        _cleanup_upload(path)
        raise
    finally:
        handle.close()
    if too_large or total_bytes > MAX_UPLOAD_BYTES:
        _cleanup_upload(path)
        raise HTTPException(status_code=413, detail="Tệp vượt quá giới hạn 50 MB")
    return path


def _cleanup_upload(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        if path.parent.name.startswith("upload-"):
            shutil.rmtree(path.parent, ignore_errors=True)
    except OSError:
        pass


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return "<h1>DH M&amp;E Pricing</h1><p>Static UI chưa được tạo.</p>"
    return page.read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    with db_session() as conn:
        conn.execute("SELECT 1").fetchone()
    return {
        "status": "ok",
        "service": settings.app_name,
        "database": "sqlite",
        "ai": ai_provider.status(),
        "chatbot": chatbot_status(),
        "timestamp": utc_now(),
    }


@app.post("/api/chatbot")
def chatbot_chat(payload: dict = Body(...)) -> dict[str, Any]:
    """Chat assistant widget endpoint — Q&A plus real tool-calling.

    Separate provider/config from the pricing AI boundary in app.ai. Tool
    calls reuse the exact same service-layer functions as the REST API
    (app.ingest / app.pricing / app.export) — see app/chatbot.py for the
    tool definitions and the confirm-gate safety model. The provider API
    key stays server-side.
    """

    try:
        reply, downloads = generate_reply_with_metadata(payload.get("messages"))
    except ChatbotError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"reply": reply, "downloads": downloads}


@app.get("/api/chatbot/greeting")
def chatbot_greeting() -> dict[str, Any]:
    return {"greeting": DEFAULT_GREETING, **chatbot_status()}


@app.post("/api/chatbot/upload")
async def chatbot_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """Save a file attached in the chat widget and hand back an upload_id.

    Reuses `_save_upload`'s existing .xls/.xlsx + 50 MB validation. The saved
    path is registered with app.chatbot so a later tool call (preview/import)
    can resolve it; abandoned uploads are swept after 30 minutes.
    """

    path = _save_upload(file)
    upload_id = register_upload(path, file.filename or path.name)
    return {"upload_id": upload_id, "filename": file.filename or path.name, "size_bytes": path.stat().st_size}


@app.get("/api/sources")
def list_sources(
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    with db_session() as conn:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("COALESCE(lifecycle_status, 'ACTIVE')=?")
            params.append(status.strip().upper())
        if q:
            clauses.append("(LOWER(filename) LIKE ? OR LOWER(detected_type) LIKE ?)")
            needle = f"%{q.strip().lower()}%"
            params.extend([needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM source_files {where} ORDER BY uploaded_at DESC, id DESC",
            tuple(params),
        ).fetchall()
        return [_source_payload(conn, row) for row in rows]


@app.get("/api/sources/{source_id}")
def get_source(
    source_id: int,
    include_rows: bool = Query(default=True),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    with db_session() as conn:
        try:
            return source_file_detail(
                conn,
                source_id,
                include_rows=include_rows,
                limit=limit,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Không tìm thấy nguồn dữ liệu")


@app.post("/api/sources/{source_id}/archive")
def archive_source(
    source_id: int,
    reason: str | None = Query(default=None),
    payload: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    reason = reason or str((payload or {}).get("reason") or "").strip() or None
    with db_session() as conn:
        row = conn.execute(
            "SELECT lifecycle_status FROM source_files WHERE id=?", (source_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy nguồn dữ liệu")
        status = str(row["lifecycle_status"] or "ACTIVE").upper()
        if status == "SUPERSEDED":
            return source_file_detail(conn, source_id, include_rows=False)
        now = utc_now()
        conn.execute(
            """
            UPDATE source_files
            SET lifecycle_status='ARCHIVED',
                status_reason=?,
                archived_at=COALESCE(archived_at, ?)
            WHERE id=?
            """,
            (reason or "archived_by_user", now, source_id),
        )
        conn.execute(
            """
            INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at)
            VALUES ('SOURCE_ARCHIVED', 'source_file', ?, ?, ?)
            """,
            (source_id, dumps({"reason": reason or "archived_by_user"}), now),
        )
        clear_runtime_caches()
        return source_file_detail(conn, source_id, include_rows=False)


@app.post("/api/sources/{source_id}/restore")
def restore_source(source_id: int) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM source_files WHERE id=?", (source_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy nguồn dữ liệu")
        status = str(row["lifecycle_status"] or "ACTIVE").upper()
        if status == "SUPERSEDED":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Nguồn đã bị supersede bởi phiên bản mới; hãy dùng phiên bản mới.",
                    "superseded_by_source_file_id": row["superseded_by_source_file_id"],
                },
            )
        conn.execute(
            """
            UPDATE source_files
            SET lifecycle_status='ACTIVE', status_reason=NULL, archived_at=NULL
            WHERE id=?
            """,
            (source_id,),
        )
        conn.execute(
            """
            INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at)
            VALUES ('SOURCE_RESTORED', 'source_file', ?, '{}', ?)
            """,
            (source_id, utc_now()),
        )
        clear_runtime_caches()
        return source_file_detail(conn, source_id, include_rows=False)


@app.delete("/api/sources/{source_id}")
def delete_source(
    source_id: int,
    force: bool = Query(default=False),
) -> dict[str, Any]:
    storage_key: str | None = None
    with db_session() as conn:
        row = conn.execute("SELECT * FROM source_files WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy nguồn dữ liệu")
        references = _source_reference_counts(conn, source_id)
        if references["derived_references"] and not force:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Nguồn đã được dùng để tạo dữ liệu tham chiếu; không thể xóa cứng. Hãy archive.",
                    "source_file_id": source_id,
                    "references": references,
                    "suggested_action": f"POST /api/sources/{source_id}/archive",
                },
            )
        if references["derived_references"] and force:
            now = utc_now()
            conn.execute(
                """
                UPDATE source_files
                SET lifecycle_status='ARCHIVED',
                    status_reason='force_delete_requested_but_references_retained',
                    archived_at=COALESCE(archived_at, ?)
                WHERE id=?
                """,
                (now, source_id),
            )
            conn.execute(
                """
                INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at)
                VALUES ('SOURCE_ARCHIVED', 'source_file', ?, ?, ?)
                """,
                (source_id, dumps({"force": True, "references": references}), now),
            )
            clear_runtime_caches()
            return {
                "deleted": False,
                "archived": True,
                "id": source_id,
                "references": references,
                "message": "Nguồn đã được archive để giữ nguyên provenance.",
            }
        storage_key = row["storage_key"]
        conn.execute("DELETE FROM source_files WHERE id=?", (source_id,))
        clear_runtime_caches()
    # Remove the retained raw workbook after the database transaction has
    # committed.  Only delete paths below the application-owned raw directory;
    # a malformed/stale DB row must never turn this endpoint into an arbitrary
    # filesystem delete primitive.
    raw_deleted = False
    if storage_key:
        try:
            raw_path = Path(storage_key).resolve()
            raw_root = RAW_DIR.resolve()
            if raw_path == raw_root or raw_root not in raw_path.parents:
                raw_path = None
            if raw_path is not None and raw_path.exists() and raw_path.is_file():
                raw_path.unlink()
                raw_deleted = True
        except OSError:
            raw_deleted = False
    return {"deleted": True, "id": source_id, "raw_deleted": raw_deleted}


@app.post("/api/sources/{source_id}/reprocess")
def reprocess_source(source_id: int) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM source_files WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy nguồn dữ liệu")
        path = Path(row["storage_key"])
        confirmed = row["confirmed_type"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Không còn file gốc để reprocess")
    return ingest_workbook(
        path,
        source_filename=row["filename"],
        confirmed_type=confirmed,
        force=True,
    )


@app.post("/api/import/preview")
async def preview_import(file: UploadFile = File(...)) -> dict[str, Any]:
    path = _save_upload(file)
    try:
        parsed = parse_workbook(path)
        return {
            "filename": file.filename,
            "detected_type": parsed["workbook_type"],
            "effective_date": parsed["effective_date"],
            "effective_date_inferred": parsed["effective_date_inferred"],
            "total_data_rows": parsed["total_data_rows"],
            "warnings": parsed["warnings"],
            "sheets": [
                {
                    "name": sheet.snapshot.name,
                    "detected_type": sheet.detected_type,
                    "header_row": sheet.header_row,
                    "mapping": sheet.mapping,
                    "max_row": sheet.snapshot.max_row,
                    "max_col": sheet.snapshot.max_col,
                    "data_rows": sum(1 for row in sheet.rows if row["row_kind"] == "data"),
                    "warnings": sheet.warnings,
                }
                for sheet in parsed["sheets"]
            ],
        }
    finally:
        _cleanup_upload(path)


@app.post("/api/import")
async def import_files(
    files: list[UploadFile] = File(...),
    confirm_classification: str | None = Form(default=None),
) -> dict[str, Any]:
    try:
        classifications = json.loads(confirm_classification or "{}")
    except json.JSONDecodeError:
        classifications = {}
    results: list[dict[str, Any]] = []
    for upload in files:
        path = _save_upload(upload)
        try:
            original_name = upload.filename or path.name
            confirmed = _classification(classifications.get(original_name))
            result = ingest_workbook(
                path,
                source_filename=original_name,
                confirmed_type=confirmed,
            )
            # Return the human filename even though the temporary upload path
            # is what the parser saw.
            result["filename"] = original_name
            with db_session() as conn:
                source = conn.execute(
                    "SELECT * FROM source_files WHERE id=?", (result["source_file_id"],)
                ).fetchone()
                result["source"] = _source_payload(conn, source) if source else None
            results.append(result)
        finally:
            _cleanup_upload(path)
    return {"sources": [result.get("source") or result for result in results], "results": results}


@app.post("/api/seed")
def seed_samples(
    include_holdout: bool = Query(default=False),
    force: bool = Query(default=False),
) -> dict[str, Any]:
    input_dir = ROOT_DIR / "input"
    if not input_dir.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy thư mục input")
    holdout = "BOQ-HỆ THỐNG ĐIỆN TRẠI LƠN HẢI HÀ-DH290124.xlsx"
    results = ingest_directory(
        input_dir,
        holdout_filename=holdout,
        skip_holdout=not include_holdout,
        force=force,
    )
    return {"results": results, "catalog": catalog_stats()}


@app.get("/api/catalog/stats")
def get_catalog_stats() -> dict[str, Any]:
    return catalog_stats()


def _catalog_source_pointer(
    conn,
    *,
    source_file_id: int | None,
    source_sheet_id: int | None = None,
    source_row_id: int | None = None,
) -> dict[str, Any] | None:
    if source_file_id is None:
        return None
    source = conn.execute(
        """
        SELECT id, filename, lifecycle_status, status_reason, version_no
        FROM source_files WHERE id=?
        """,
        (source_file_id,),
    ).fetchone()
    if not source:
        return {"file_id": source_file_id}
    pointer: dict[str, Any] = {
        "file_id": int(source["id"]),
        "filename": source["filename"],
        "status": source["lifecycle_status"] or "ACTIVE",
        "status_reason": source["status_reason"],
        "version_no": source["version_no"] or 1,
    }
    if source_sheet_id is not None:
        sheet = conn.execute(
            "SELECT id, sheet_name, sheet_index FROM source_sheets WHERE id=?",
            (source_sheet_id,),
        ).fetchone()
        if sheet:
            pointer.update(
                {
                    "sheet_id": int(sheet["id"]),
                    "sheet_name": sheet["sheet_name"],
                    "sheet_index": sheet["sheet_index"],
                }
            )
    if source_row_id is not None:
        source_row = conn.execute(
            "SELECT id, row_no FROM source_rows WHERE id=?",
            (source_row_id,),
        ).fetchone()
        if source_row:
            pointer.update(
                {"row_id": int(source_row["id"]), "row_no": source_row["row_no"]}
            )
    return pointer


def _dedupe_pointers(pointers: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for pointer in pointers:
        if not pointer:
            continue
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


def _catalog_provenance(
    conn,
    *,
    kind: str,
    entity_id: int,
) -> list[dict[str, Any]]:
    """Resolve all source pointers for a product/labor identity."""

    if kind == "product":
        links = conn.execute(
            """
            SELECT source_file_id, source_sheet_id, source_row_id, relation_type
            FROM catalog_source_links
            WHERE entity_type='product' AND product_id=?
            ORDER BY id
            """,
            (entity_id,),
        ).fetchall()
        price_refs = conn.execute(
            """
            SELECT source_file_id, source_sheet_id, source_row_id, 'price' AS relation_type
            FROM product_prices WHERE product_id=? AND source_file_id IS NOT NULL
            UNION ALL
            SELECT source_file_id, source_sheet_id, source_row_id, 'observation' AS relation_type
            FROM price_observations WHERE product_id=? AND source_file_id IS NOT NULL
            """,
            (entity_id, entity_id),
        ).fetchall()
    else:
        links = conn.execute(
            """
            SELECT source_file_id, source_sheet_id, source_row_id, relation_type
            FROM catalog_source_links
            WHERE entity_type='labor' AND labor_item_id=?
            ORDER BY id
            """,
            (entity_id,),
        ).fetchall()
        price_refs = conn.execute(
            """
            SELECT source_file_id, source_sheet_id, source_row_id, 'labor_rate' AS relation_type
            FROM labor_rates WHERE labor_item_id=? AND source_file_id IS NOT NULL
            """,
            (entity_id,),
        ).fetchall()
    pointers: list[dict[str, Any] | None] = []
    for ref in [*links, *price_refs]:
        pointer = _catalog_source_pointer(
            conn,
            source_file_id=ref["source_file_id"],
            source_sheet_id=ref["source_sheet_id"],
            source_row_id=ref["source_row_id"],
        )
        if pointer:
            pointer["relation_type"] = ref["relation_type"]
        pointers.append(pointer)
    return _dedupe_pointers(pointers)


def _serialize_product_catalog(
    conn,
    row: Any,
    *,
    include_history: bool = False,
) -> dict[str, Any]:
    item = dict(row)
    item["kind"] = "product"
    item["status"] = item.get("lifecycle_status") or "ACTIVE"
    item["technical_attributes"] = loads(
        item.pop("technical_attributes_json", "{}"), {}
    )
    item["aliases"] = loads(item.pop("aliases_json", "[]"), [])
    # Keep the catalog read model on the same observation-first policy as the
    # pricing engine.  ``product_prices`` is a compatibility projection for
    # older databases; selecting it directly here could show a stale value in
    # the UI even though a newer immutable observation is used for quotations.
    selected_value, selected_source = choose_product_price(
        conn,
        int(item["id"]),
        quotation_date=None,
    )
    if selected_source:
        current_payload = dict(selected_source)
        # ``choose_product_price`` returns decoded ``calc``/``context`` fields
        # for observations and legacy provenance.  Normalize a few aliases so
        # the catalog detail and existing clients can consume both row types.
        current_payload.setdefault(
            "source_filename", current_payload.get("filename")
        )
        current_payload.setdefault(
            "source_status", current_payload.get("source_lifecycle_status")
        )
        current_payload.setdefault("source_sheet_name", current_payload.get("sheet_name"))
        current_payload.setdefault("source_row_no", current_payload.get("row_no"))
        current_payload["record_type"] = (
            "price_observation"
            if current_payload.get("observation_type")
            else "product_price"
        )
        current_payload["source"] = _catalog_source_pointer(
            conn,
            source_file_id=current_payload.get("source_file_id"),
            source_sheet_id=current_payload.get("source_sheet_id"),
            source_row_id=current_payload.get("source_row_id"),
        )
    else:
        current_payload = None
    item["current_price"] = selected_value
    item["price"] = current_payload
    item["source"] = current_payload.get("source") if current_payload else None
    counts = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM price_observations WHERE product_id=?) AS observation_count,
            (SELECT COUNT(*) FROM product_prices WHERE product_id=?) AS price_count
        """,
        (item["id"], item["id"]),
    ).fetchone()
    item["observation_count"] = int(counts["observation_count"] or 0)
    item["price_observation_count"] = item["observation_count"]
    item["price_count"] = int(counts["price_count"] or 0)
    item["provenance"] = _catalog_provenance(
        conn, kind="product", entity_id=int(item["id"])
    )
    if include_history:
        item["prices"] = _product_price_history(conn, int(item["id"]))
        item["observations"] = _product_observation_history(conn, int(item["id"]))
        item["boq_references"] = [
            dict(ref)
            for ref in conn.execute(
                """
                SELECT id, project_id, raw_description, quantity, material_price,
                       status, source_file_id, source_sheet_id, source_row_id
                FROM boq_items WHERE matched_product_id=? ORDER BY id DESC LIMIT 500
                """,
                (item["id"],),
            ).fetchall()
        ]
    return item


def _product_price_history(conn, product_id: int, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT pp.*, sf.filename AS source_filename, sf.lifecycle_status AS source_status,
               ss.sheet_name, sr.row_no
        FROM product_prices pp
        LEFT JOIN source_files sf ON sf.id=pp.source_file_id
        LEFT JOIN source_sheets ss ON ss.id=pp.source_sheet_id
        LEFT JOIN source_rows sr ON sr.id=pp.source_row_id
        WHERE pp.product_id=?
        ORDER BY COALESCE(pp.effective_date, pp.created_at) DESC, pp.id DESC
        LIMIT ?
        """,
        (product_id, limit),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["calc"] = loads(item.pop("calc_json", "{}"), {})
        item["source"] = _catalog_source_pointer(
            conn,
            source_file_id=item.get("source_file_id"),
            source_sheet_id=item.get("source_sheet_id"),
            source_row_id=item.get("source_row_id"),
        )
        result.append(item)
    return result


def _product_observation_history(
    conn, product_id: int, limit: int = 200
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT po.*, sf.filename AS source_filename, sf.lifecycle_status AS source_status,
               ss.sheet_name, sr.row_no
        FROM price_observations po
        LEFT JOIN source_files sf ON sf.id=po.source_file_id
        LEFT JOIN source_sheets ss ON ss.id=po.source_sheet_id
        LEFT JOIN source_rows sr ON sr.id=po.source_row_id
        WHERE po.product_id=?
        ORDER BY COALESCE(po.effective_date, po.created_at) DESC, po.id DESC
        LIMIT ?
        """,
        (product_id, limit),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["context"] = loads(item.pop("context_json", "{}"), {})
        item["calc"] = loads(item.pop("calc_json", "{}"), {})
        item["source"] = _catalog_source_pointer(
            conn,
            source_file_id=item.get("source_file_id"),
            source_sheet_id=item.get("source_sheet_id"),
            source_row_id=item.get("source_row_id"),
        )
        result.append(item)
    return result


def _serialize_labor_catalog(
    conn,
    row: Any,
    *,
    include_history: bool = False,
) -> dict[str, Any]:
    item = dict(row)
    item["kind"] = "labor"
    item["status"] = item.get("lifecycle_status") or "ACTIVE"
    item["technical_attributes"] = loads(
        item.pop("technical_attributes_json", "{}"), {}
    )
    current = conn.execute(
        """
        SELECT lr.*, sf.filename AS source_filename, sf.lifecycle_status AS source_status,
               ss.sheet_name, sr.row_no
        FROM labor_rates lr
        LEFT JOIN source_files sf ON sf.id=lr.source_file_id
        LEFT JOIN source_sheets ss ON ss.id=lr.source_sheet_id
        LEFT JOIN source_rows sr ON sr.id=lr.source_row_id
        WHERE lr.labor_item_id=?
          AND (lr.source_file_id IS NULL OR COALESCE(sf.lifecycle_status, 'ACTIVE')='ACTIVE')
        ORDER BY COALESCE(lr.effective_date, lr.created_at) DESC, lr.id DESC
        LIMIT 1
        """,
        (item["id"],),
    ).fetchone()
    if current:
        current_payload = dict(current)
        current_payload["policy"] = loads(current_payload.pop("policy_json", "{}"), {})
        current_payload["source"] = _catalog_source_pointer(
            conn,
            source_file_id=current_payload.get("source_file_id"),
            source_sheet_id=current_payload.get("source_sheet_id"),
            source_row_id=current_payload.get("source_row_id"),
        )
    else:
        current_payload = None
    item["current_rate"] = current_payload["rate"] if current_payload else None
    item["rate"] = current_payload
    item["source"] = current_payload.get("source") if current_payload else None
    item["provenance"] = _catalog_provenance(
        conn, kind="labor", entity_id=int(item["id"])
    )
    if include_history:
        rows = conn.execute(
            """
            SELECT lr.*, sf.filename AS source_filename, sf.lifecycle_status AS source_status,
                   ss.sheet_name, sr.row_no
            FROM labor_rates lr
            LEFT JOIN source_files sf ON sf.id=lr.source_file_id
            LEFT JOIN source_sheets ss ON ss.id=lr.source_sheet_id
            LEFT JOIN source_rows sr ON sr.id=lr.source_row_id
            WHERE lr.labor_item_id=?
            ORDER BY COALESCE(lr.effective_date, lr.created_at) DESC, lr.id DESC
            LIMIT 200
            """,
            (item["id"],),
        ).fetchall()
        item["rates"] = []
        for rate in rows:
            rate_payload = dict(rate)
            rate_payload["policy"] = loads(
                rate_payload.pop("policy_json", "{}"), {}
            )
            rate_payload["source"] = _catalog_source_pointer(
                conn,
                source_file_id=rate_payload.get("source_file_id"),
                source_sheet_id=rate_payload.get("source_sheet_id"),
                source_row_id=rate_payload.get("source_row_id"),
            )
            item["rates"].append(rate_payload)
        item["boq_references"] = [
            dict(ref)
            for ref in conn.execute(
                """
                SELECT id, project_id, raw_description, quantity, labor_price,
                       status, source_file_id, source_sheet_id, source_row_id
                FROM boq_items WHERE matched_labor_item_id=? ORDER BY id DESC LIMIT 500
                """,
                (item["id"],),
            ).fetchall()
        ]
    return item


@app.get("/api/catalog/items")
def list_catalog_items(
    kind: str = Query(default="all"),
    category: str | None = Query(default=None),
    source_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    selected_kind = kind.strip().lower()
    if selected_kind in {"material", "materials", "product", "products"}:
        selected_kind = "product"
    elif selected_kind in {"labour", "labor", "labor_item", "labor_items"}:
        selected_kind = "labor"
    elif selected_kind != "all":
        raise HTTPException(status_code=400, detail="kind phải là all, product hoặc labor")
    status_value = status.strip().upper() if status else "ACTIVE"
    statuses = None if status_value in {"", "ALL", "*"} else {status_value}
    search = f"%{q.strip().lower()}%" if q and q.strip() else None

    def _where(alias: str, source_clause: str | None = None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append(f"COALESCE({alias}.lifecycle_status, 'ACTIVE') IN ({','.join('?' for _ in statuses)})")
            params.extend(sorted(statuses))
        if category and category.strip():
            clauses.append(f"LOWER(COALESCE({alias}.category, ''))=LOWER(?)")
            params.append(category.strip())
        if search:
            code_column = "product_code" if alias == "p" else "code"
            clauses.append(
                f"(LOWER(COALESCE({alias}.normalized_name,'')) LIKE ? "
                f"OR LOWER(COALESCE({alias}.{code_column},'')) LIKE ? "
                f"OR LOWER(COALESCE({alias}.category,'')) LIKE ?)"
            )
            params.extend([search, search, search])
        if source_clause:
            clauses.append(f"({source_clause})")
        return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)

    with db_session() as conn:
        products: list[dict[str, Any]] = []
        labor: list[dict[str, Any]] = []
        page_items: list[dict[str, Any]] = []
        if selected_kind == "all":
            # ``kind=all`` is one logical collection.  Apply LIMIT/OFFSET to
            # the globally ordered product + labor stream; applying the same
            # window independently to both tables previously returned up to
            # 2*limit rows and made later pages overlap/skip records.
            product_source_clause = None
            product_source_params: list[Any] = []
            if source_id is not None:
                product_source_clause = """
                    EXISTS (
                        SELECT 1 FROM catalog_source_links csl
                        WHERE csl.entity_type='product' AND csl.product_id=p.id
                              AND csl.source_file_id=?
                    )
                    OR EXISTS (
                        SELECT 1 FROM product_prices spp
                        WHERE spp.product_id=p.id AND spp.source_file_id=?
                    )
                    OR EXISTS (
                        SELECT 1 FROM price_observations spo
                        WHERE spo.product_id=p.id AND spo.source_file_id=?
                    )
                """
                product_source_params = [source_id, source_id, source_id]
            labor_source_clause = None
            labor_source_params: list[Any] = []
            if source_id is not None:
                labor_source_clause = """
                    EXISTS (
                        SELECT 1 FROM catalog_source_links csl
                        WHERE csl.entity_type='labor' AND csl.labor_item_id=li.id
                              AND csl.source_file_id=?
                    )
                    OR EXISTS (
                        SELECT 1 FROM labor_rates slr
                        WHERE slr.labor_item_id=li.id AND slr.source_file_id=?
                    )
                """
                labor_source_params = [source_id, source_id]
            product_where, product_params = _where("p", product_source_clause)
            product_params.extend(product_source_params)
            labor_where, labor_params = _where("li", labor_source_clause)
            labor_params.extend(labor_source_params)
            page_keys = conn.execute(
                f"""
                SELECT kind, entity_id
                FROM (
                    SELECT 'product' AS kind, p.id AS entity_id,
                           p.normalized_name AS sort_name
                    FROM products p {product_where}
                    UNION ALL
                    SELECT 'labor' AS kind, li.id AS entity_id,
                           li.normalized_name AS sort_name
                    FROM labor_items li {labor_where}
                )
                ORDER BY sort_name COLLATE NOCASE, kind, entity_id
                LIMIT ? OFFSET ?
                """,
                (*product_params, *labor_params, limit, offset),
            ).fetchall()
            product_ids = [int(row["entity_id"]) for row in page_keys if row["kind"] == "product"]
            labor_ids = [int(row["entity_id"]) for row in page_keys if row["kind"] == "labor"]
            product_rows = {}
            labor_rows = {}
            if product_ids:
                product_rows = {
                    int(row["id"]): row
                    for row in conn.execute(
                        f"SELECT * FROM products WHERE id IN ({','.join('?' for _ in product_ids)})",
                        tuple(product_ids),
                    ).fetchall()
                }
            if labor_ids:
                labor_rows = {
                    int(row["id"]): row
                    for row in conn.execute(
                        f"SELECT * FROM labor_items WHERE id IN ({','.join('?' for _ in labor_ids)})",
                        tuple(labor_ids),
                    ).fetchall()
                }
            for page_key in page_keys:
                entity_id = int(page_key["entity_id"])
                if page_key["kind"] == "product" and entity_id in product_rows:
                    item = _serialize_product_catalog(conn, product_rows[entity_id])
                    products.append(item)
                    page_items.append(item)
                elif page_key["kind"] == "labor" and entity_id in labor_rows:
                    item = _serialize_labor_catalog(conn, labor_rows[entity_id])
                    labor.append(item)
                    page_items.append(item)
        if selected_kind == "product":
            source_clause = None
            source_params: list[Any] = []
            if source_id is not None:
                source_clause = """
                    EXISTS (
                        SELECT 1 FROM catalog_source_links csl
                        WHERE csl.entity_type='product' AND csl.product_id=p.id
                              AND csl.source_file_id=?
                    )
                    OR EXISTS (
                        SELECT 1 FROM product_prices spp
                        WHERE spp.product_id=p.id AND spp.source_file_id=?
                    )
                    OR EXISTS (
                        SELECT 1 FROM price_observations spo
                        WHERE spo.product_id=p.id AND spo.source_file_id=?
                    )
                """
                source_params = [source_id, source_id, source_id]
            where, params = _where("p", source_clause)
            params.extend(source_params)
            rows = conn.execute(
                f"SELECT p.* FROM products p {where} ORDER BY p.normalized_name, p.id "
                "LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            products = [_serialize_product_catalog(conn, row) for row in rows]
        if selected_kind == "labor":
            source_clause = None
            source_params = []
            if source_id is not None:
                source_clause = """
                    EXISTS (
                        SELECT 1 FROM catalog_source_links csl
                        WHERE csl.entity_type='labor' AND csl.labor_item_id=li.id
                              AND csl.source_file_id=?
                    )
                    OR EXISTS (
                        SELECT 1 FROM labor_rates slr
                        WHERE slr.labor_item_id=li.id AND slr.source_file_id=?
                    )
                """
                source_params = [source_id, source_id]
            where, params = _where("li", source_clause)
            params.extend(source_params)
            rows = conn.execute(
                f"SELECT li.* FROM labor_items li {where} ORDER BY li.normalized_name, li.id "
                "LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            labor = [_serialize_labor_catalog(conn, row) for row in rows]

        def _count(table: str, alias: str, source_predicate: str, source_params: list[Any]) -> int:
            where, params = _where(alias, source_predicate)
            params.extend(source_params)
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} {alias} {where}", tuple(params)
                ).fetchone()[0]
            )

        product_count = 0
        labor_count = 0
        if selected_kind in {"all", "product"}:
            source_clause = None
            source_params = []
            if source_id is not None:
                source_clause = """
                    EXISTS (SELECT 1 FROM catalog_source_links csl
                            WHERE csl.entity_type='product' AND csl.product_id=p.id AND csl.source_file_id=?)
                    OR EXISTS (SELECT 1 FROM product_prices spp
                            WHERE spp.product_id=p.id AND spp.source_file_id=?)
                    OR EXISTS (SELECT 1 FROM price_observations spo
                            WHERE spo.product_id=p.id AND spo.source_file_id=?)
                """
                source_params = [source_id, source_id, source_id]
            product_count = _count("products", "p", source_clause, source_params)
        if selected_kind in {"all", "labor"}:
            source_clause = None
            source_params = []
            if source_id is not None:
                source_clause = """
                    EXISTS (SELECT 1 FROM catalog_source_links csl
                            WHERE csl.entity_type='labor' AND csl.labor_item_id=li.id AND csl.source_file_id=?)
                    OR EXISTS (SELECT 1 FROM labor_rates slr
                            WHERE slr.labor_item_id=li.id AND slr.source_file_id=?)
                """
                source_params = [source_id, source_id]
            labor_count = _count("labor_items", "li", source_clause, source_params)

        facet_categories = [
            row[0]
            for row in conn.execute(
                """
                SELECT category FROM (
                    SELECT category FROM products WHERE category IS NOT NULL AND TRIM(category)<>''
                    UNION SELECT category FROM labor_items WHERE category IS NOT NULL AND TRIM(category)<>''
                ) ORDER BY category COLLATE NOCASE
                """
            ).fetchall()
        ]
        facet_sources = [
            dict(row)
            for row in conn.execute(
                """
                SELECT sf.id, sf.filename, sf.lifecycle_status AS status,
                       (
                           SELECT COUNT(*) FROM (
                               SELECT 'product:' || csl.product_id
                               FROM catalog_source_links csl
                               WHERE csl.source_file_id=sf.id
                                 AND csl.product_id IS NOT NULL
                               UNION
                               SELECT 'labor:' || csl.labor_item_id
                               FROM catalog_source_links csl
                               WHERE csl.source_file_id=sf.id
                                 AND csl.labor_item_id IS NOT NULL
                               UNION
                               SELECT 'product:' || pp.product_id
                               FROM product_prices pp
                               WHERE pp.source_file_id=sf.id
                               UNION
                               SELECT 'product:' || po.product_id
                               FROM price_observations po
                               WHERE po.source_file_id=sf.id
                               UNION
                               SELECT 'labor:' || lr.labor_item_id
                               FROM labor_rates lr
                               WHERE lr.source_file_id=sf.id
                           )
                       ) AS link_count
                FROM source_files sf
                ORDER BY sf.uploaded_at DESC, sf.id DESC
                """
            ).fetchall()
        ]
        facet_statuses = [
            row[0]
            for row in conn.execute(
                """
                SELECT lifecycle_status FROM (
                    SELECT COALESCE(lifecycle_status, 'ACTIVE') AS lifecycle_status
                    FROM products
                    UNION
                    SELECT COALESCE(lifecycle_status, 'ACTIVE')
                    FROM labor_items
                ) ORDER BY lifecycle_status
                """
            ).fetchall()
        ]
        items = page_items if selected_kind == "all" else products + labor
        return {
            "items": items,
            "total": product_count + labor_count,
            "product_total": product_count,
            "labor_total": labor_count,
            "facets": {"categories": facet_categories, "sources": facet_sources},
            "categories": facet_categories,
            "sources": facet_sources,
            "statuses": facet_statuses,
            "limit": limit,
            "offset": offset,
            "kind": selected_kind,
        }


@app.get("/api/catalog/products/{product_id}")
def get_catalog_product(product_id: int) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy sản phẩm")
        return _serialize_product_catalog(conn, row, include_history=True)


@app.get("/api/catalog/labor/{labor_id}")
def get_catalog_labor(labor_id: int) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM labor_items WHERE id=?", (labor_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy mục nhân công")
        return _serialize_labor_catalog(conn, row, include_history=True)


def _normalise_catalog_kind(kind: str) -> str:
    value = kind.strip().lower()
    if value in {"product", "products", "material", "materials"}:
        return "product"
    if value in {"labor", "labour", "labor_item", "labor_items"}:
        return "labor"
    raise HTTPException(status_code=400, detail="kind phải là product hoặc labor")


def _catalog_reference_counts(conn, kind: str, entity_id: int) -> dict[str, int]:
    if kind == "product":
        queries = {
            "prices": "SELECT COUNT(*) FROM product_prices WHERE product_id=?",
            "observations": "SELECT COUNT(*) FROM price_observations WHERE product_id=?",
            "boq_items": "SELECT COUNT(*) FROM boq_items WHERE matched_product_id=?",
            "source_links": "SELECT COUNT(*) FROM catalog_source_links WHERE entity_type='product' AND product_id=?",
            "candidates": """
                SELECT COUNT(*) FROM match_candidates
                WHERE LOWER(candidate_type) IN ('product','material','supplier')
                  AND candidate_id=?
            """,
        }
    else:
        queries = {
            "rates": "SELECT COUNT(*) FROM labor_rates WHERE labor_item_id=?",
            "boq_items": "SELECT COUNT(*) FROM boq_items WHERE matched_labor_item_id=?",
            "source_links": "SELECT COUNT(*) FROM catalog_source_links WHERE entity_type='labor' AND labor_item_id=?",
            "candidates": """
                SELECT COUNT(*) FROM match_candidates
                WHERE LOWER(candidate_type) IN ('labor','labour','labor_item')
                  AND candidate_id=?
            """,
        }
    result = {
        name: int(conn.execute(sql, (entity_id,)).fetchone()[0])
        for name, sql in queries.items()
    }
    result["derived_references"] = sum(result.values())
    return result


def _catalog_item_payload(conn, kind: str, entity_id: int) -> dict[str, Any]:
    table = "products" if kind == "product" else "labor_items"
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy hạng mục")
    return (
        _serialize_product_catalog(conn, row, include_history=True)
        if kind == "product"
        else _serialize_labor_catalog(conn, row, include_history=True)
    )


@app.post("/api/catalog/items/{kind}/{item_id}/archive")
def archive_catalog_item(
    kind: str,
    item_id: int,
    reason: str | None = Query(default=None),
    payload: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    entity_kind = _normalise_catalog_kind(kind)
    reason = reason or str((payload or {}).get("reason") or "").strip() or None
    table = "products" if entity_kind == "product" else "labor_items"
    with db_session() as conn:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE id=?", (item_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy hạng mục")
        now = utc_now()
        conn.execute(
            f"""
            UPDATE {table}
            SET lifecycle_status='ARCHIVED',
                status_reason=?,
                archived_at=COALESCE(archived_at, ?)
            WHERE id=?
            """,
            (reason or "archived_by_user", now, item_id),
        )
        conn.execute(
            """
            INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at)
            VALUES ('CATALOG_ITEM_ARCHIVED', ?, ?, ?, ?)
            """,
            (
                entity_kind,
                item_id,
                dumps({"reason": reason or "archived_by_user"}),
                now,
            ),
        )
        clear_runtime_caches()
        return _catalog_item_payload(conn, entity_kind, item_id)


@app.post("/api/catalog/items/{kind}/{item_id}/restore")
def restore_catalog_item(kind: str, item_id: int) -> dict[str, Any]:
    entity_kind = _normalise_catalog_kind(kind)
    table = "products" if entity_kind == "product" else "labor_items"
    with db_session() as conn:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE id=?", (item_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy hạng mục")
        conn.execute(
            f"""
            UPDATE {table}
            SET lifecycle_status='ACTIVE', status_reason=NULL, archived_at=NULL
            WHERE id=?
            """,
            (item_id,),
        )
        conn.execute(
            """
            INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at)
            VALUES ('CATALOG_ITEM_RESTORED', ?, ?, '{}', ?)
            """,
            (entity_kind, item_id, utc_now()),
        )
        clear_runtime_caches()
        return _catalog_item_payload(conn, entity_kind, item_id)


@app.delete("/api/catalog/items/{kind}/{item_id}")
def delete_catalog_item(
    kind: str,
    item_id: int,
    force: bool = Query(default=False),
) -> dict[str, Any]:
    entity_kind = _normalise_catalog_kind(kind)
    table = "products" if entity_kind == "product" else "labor_items"
    with db_session() as conn:
        row = conn.execute(
            f"SELECT id FROM {table} WHERE id=?", (item_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy hạng mục")
        references = _catalog_reference_counts(conn, entity_kind, item_id)
        if references["derived_references"] and not force:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Hạng mục đã được tham chiếu; không thể xóa cứng. Hãy archive.",
                    "kind": entity_kind,
                    "id": item_id,
                    "references": references,
                    "suggested_action": f"POST /api/catalog/items/{entity_kind}/{item_id}/archive",
                },
            )
        if references["derived_references"] and force:
            now = utc_now()
            conn.execute(
                f"""
                UPDATE {table}
                SET lifecycle_status='ARCHIVED',
                    status_reason='force_delete_requested_but_references_retained',
                    archived_at=COALESCE(archived_at, ?)
                WHERE id=?
                """,
                (now, item_id),
            )
            conn.execute(
                """
                INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at)
                VALUES ('CATALOG_ITEM_ARCHIVED', ?, ?, ?, ?)
                """,
                (entity_kind, item_id, dumps({"force": True, "references": references}), now),
            )
            clear_runtime_caches()
            return {
                "deleted": False,
                "archived": True,
                "kind": entity_kind,
                "id": item_id,
                "references": references,
            }
        conn.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        conn.execute(
            """
            INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at)
            VALUES ('CATALOG_ITEM_DELETED', ?, ?, '{}', ?)
            """,
            (entity_kind, item_id, utc_now()),
        )
        clear_runtime_caches()
        return {
            "deleted": True,
            "archived": False,
            "kind": entity_kind,
            "id": item_id,
            "references": references,
        }


@app.get("/api/quotations")
def list_quotations() -> list[dict[str, Any]]:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [_project_payload(conn, row) for row in rows]


@app.post("/api/quotations")
async def create_quotation(
    files: list[UploadFile] | None = File(default=None),
    project_name: str | None = Form(default=None),
    customer: str | None = Form(default=None),
    quotation_date: str | None = Form(default=None),
    pricing_policy: str | None = Form(default=None),
    note: str | None = Form(default=None),
) -> dict[str, Any]:
    # FastAPI cannot bind a JSON body and multipart fields to the same
    # signature. If no files are supplied, accept a JSON object manually.
    if not files:
        raise HTTPException(status_code=400, detail="Hãy upload ít nhất một workbook BOQ")
    created: list[dict[str, Any]] = []
    for upload in files:
        path = _save_upload(upload)
        try:
            # The same workbook may have been imported previously as a
            # historical source. Keep a separate quotation/source record while
            # preserving checksum-level idempotency for ordinary imports.
            result = ingest_workbook(
                path,
                source_filename=upload.filename,
                confirmed_type="NEW_BOQ",
                exclude_prices=True,
                allow_duplicate=True,
            )
            project_id = result.get("project_id")
            if not project_id:
                raise HTTPException(status_code=422, detail="Không bóc được dòng BOQ từ workbook")
            with db_session() as conn:
                conn.execute(
                    """
                    UPDATE projects
                    SET project_name=COALESCE(?, project_name),
                        customer=?, quotation_date=?,
                        metadata_json=?
                    WHERE id=?
                    """,
                    (
                        project_name or None,
                        customer,
                        quotation_date,
                        dumps(
                            {
                                "document_type": "NEW_BOQ",
                                "pricing_policy": pricing_policy or "default",
                                "note": note or "",
                                "source_filename": upload.filename,
                            }
                        ),
                        project_id,
                    ),
                )
                row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                payload = _project_payload(conn, row)
            created.append(payload)
        finally:
            _cleanup_upload(path)
    return {"quotation": created[0] if len(created) == 1 else created, "quotations": created}


def _result_payload(project_id: int, run_id: int | None = None) -> dict[str, Any]:
    try:
        result = get_project_result(project_id, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Không tìm thấy báo giá")
    project = result["project"]
    with db_session() as conn:
        project_row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        payload_project = _project_payload(conn, project_row)
    metrics = result.get("metrics", {})
    payload_project["stats"] = metrics
    payload_project["status"] = result.get("run", {}).get("status") if result.get("run") else payload_project["status"]
    payload_project["total_rows"] = metrics.get("total_items", len(result["items"]))
    payload_project["material_matched"] = metrics.get("material_matched", 0)
    payload_project["labor_matched"] = metrics.get("labor_matched", 0)
    payload_project["review_required"] = metrics.get("review_required", 0)
    payload_project["no_match"] = metrics.get("no_match", 0)
    payload_project["auto_coverage"] = metrics.get(
        "handled_coverage",
        metrics.get("auto_coverage"),
    )
    payload_project["handled_coverage"] = metrics.get("handled_coverage")
    return {
        "id": project_id,
        "project": payload_project,
        "quotation": payload_project,
        "run": result.get("run"),
        "metrics": metrics,
        "stats": metrics,
        "items": result["items"],
        "boq_items": result["items"],
    }


@app.get("/api/quotations/{project_id}")
def get_quotation(project_id: int, run_id: int | None = Query(default=None)) -> dict[str, Any]:
    return _result_payload(project_id, run_id)


@app.post("/api/quotations/{project_id}/run")
def price_quotation(project_id: int, force: bool = Query(default=False)) -> dict[str, Any]:
    try:
        result = run_pricing(project_id, force=force)
    except KeyError:
        raise HTTPException(status_code=404, detail="Không tìm thấy báo giá")
    response = _result_payload(project_id, result["run_id"])
    response.update(result)
    response["quotation"] = response["project"]
    return response


@app.get("/api/quotations/{project_id}/review")
def quotation_review(project_id: int) -> list[dict[str, Any]]:
    with db_session() as conn:
        project = conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Không tìm thấy báo giá")
        # Return all rows so the UI can switch between risky/approved/no-price
        # filters without another endpoint.
        rows = conn.execute(
            "SELECT * FROM boq_items WHERE project_id=? ORDER BY CASE status WHEN 'PRICE_DRIFT_WARNING' THEN 0 WHEN 'REVIEW_REQUIRED' THEN 1 WHEN 'NO_MATCH' THEN 2 WHEN 'NO_PRICE_FOUND' THEN 3 ELSE 4 END, id",
            (project_id,),
        ).fetchall()
        return [serialize_boq_item(conn, row) for row in rows]


@app.post("/api/boq-items/{item_id}/review")
def review_boq_item(item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").lower()

    def _parse_optional_id(field_name: str) -> int | None:
        value = payload.get(field_name)
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{field_name} phải là số nguyên")
        if parsed <= 0:
            raise HTTPException(status_code=400, detail=f"{field_name} phải lớn hơn 0")
        return parsed

    selected_product_id = _parse_optional_id("selected_product_id")
    selected_labor_item_id = _parse_optional_id("selected_labor_item_id")
    generic_selected_id = _parse_optional_id("selected_candidate_id")
    candidate_type = str(
        payload.get("selected_candidate_type")
        or payload.get("candidate_type")
        or ""
    ).strip().lower()
    if generic_selected_id is not None:
        if candidate_type in {"labor", "labour", "labor_item", "labor_rate"}:
            selected_labor_item_id = generic_selected_id
        elif candidate_type in {"", "material", "product", "material_product", "supplier"}:
            # Preserve backwards compatibility: an untyped generic candidate
            # id has historically meant a material/product id.
            selected_product_id = generic_selected_id
        else:
            raise HTTPException(status_code=400, detail="selected_candidate_type không hợp lệ")

    if selected_product_id is not None or selected_labor_item_id is not None:
        with db_session() as conn:
            if selected_product_id is not None and not conn.execute(
                "SELECT id FROM products WHERE id=?", (selected_product_id,)
            ).fetchone():
                raise HTTPException(status_code=404, detail="Không tìm thấy sản phẩm được chọn")
            if selected_labor_item_id is not None and not conn.execute(
                "SELECT id FROM labor_items WHERE id=?", (selected_labor_item_id,)
            ).fetchone():
                raise HTTPException(status_code=404, detail="Không tìm thấy mục nhân công được chọn")

    kwargs: dict[str, Any] = {"status": None, "created_by": str(payload.get("created_by") or "engineer")}
    kwargs["add_alias"] = bool(payload.get("add_alias", False))
    if action in {"approve", "select_candidate"}:
        if action == "approve" and selected_product_id is None and selected_labor_item_id is None:
            # Approval without a candidate is allowed only when the pricing
            # run already supplied a sourced price.  The service layer will
            # downgrade a truly empty row to REVIEW_REQUIRED; reject a
            # no-match approval here so the UI gives the engineer a clear
            # next action.
            with db_session() as conn:
                existing_item = conn.execute(
                    "SELECT material_price, labor_price FROM boq_items WHERE id=?",
                    (item_id,),
                ).fetchone()
            if not existing_item:
                raise HTTPException(status_code=404, detail="Không tìm thấy dòng BOQ")
            if existing_item["material_price"] is None and existing_item["labor_price"] is None:
                raise HTTPException(
                    status_code=400,
                    detail="Không thể duyệt dòng chưa có candidate hoặc đơn giá có nguồn",
                )
        if action == "select_candidate" and selected_product_id is None and selected_labor_item_id is None:
            raise HTTPException(status_code=400, detail="select_candidate cần selected_product_id hoặc selected_labor_item_id")
        if selected_product_id is not None:
            kwargs["selected_product_id"] = selected_product_id
        if selected_labor_item_id is not None:
            kwargs["selected_labor_item_id"] = selected_labor_item_id
        kwargs["status"] = "AUTO_APPROVED"
    elif action == "manual_price":
        kwargs["material_price"] = parse_number(payload.get("material_price"))
        kwargs["labor_price"] = parse_number(payload.get("labor_price"))
        note = str(payload.get("note") or "").strip()
        kwargs["material_source"] = {"type": "manual", "note": note, "entered_by": kwargs["created_by"], "entered_at": utc_now()} if kwargs["material_price"] is not None else None
        kwargs["labor_source"] = {"type": "manual", "note": note, "entered_by": kwargs["created_by"], "entered_at": utc_now()} if kwargs["labor_price"] is not None else None
        kwargs["status"] = "AUTO_APPROVED" if kwargs["material_price"] is not None or kwargs["labor_price"] is not None else "REVIEW_REQUIRED"
    elif action in {"needs_supplier_quotation", "supplier_quote"}:
        kwargs["status"] = "EXTERNAL_QUOTATION_REQUIRED"
    elif action in {"ignore", "ignored"}:
        kwargs["status"] = "IGNORED"
    else:
        raise HTTPException(status_code=400, detail="Action review không hợp lệ")
    try:
        result = review_item(item_id, **kwargs)
    except KeyError:
        raise HTTPException(status_code=404, detail="Không tìm thấy dòng BOQ")
    return result


@app.get("/api/quotations/{project_id}/export")
def export_quotation(project_id: int, run_id: int | None = Query(default=None)) -> FileResponse:
    try:
        path = export_project(project_id, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Không tìm thấy báo giá")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@app.get("/api/benchmark")
def benchmark_status() -> dict[str, Any]:
    report_json = ROOT_DIR / "benchmarks" / "holdout-report.json"
    report_md = ROOT_DIR / "benchmarks" / "holdout-report.md"
    if report_json.exists():
        try:
            payload = json.loads(report_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if report_md.exists():
            payload["report_url"] = "/benchmark-report"
        return payload
    return {
        "status": "not_run",
        "message": "Chưa chạy holdout benchmark. Dùng `python -m app.cli benchmark`.",
    }


@app.get("/benchmark-report", response_class=HTMLResponse)
def benchmark_report() -> str:
    report = ROOT_DIR / "benchmarks" / "holdout-report.md"
    if not report.exists():
        raise HTTPException(status_code=404, detail="Chưa có báo cáo benchmark")
    text = report.read_text(encoding="utf-8")
    # Minimal Markdown-ish rendering without adding a frontend dependency.
    html = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )
    return f"<html><head><meta charset='utf-8'><title>Holdout benchmark</title></head><body style='font-family:system-ui;max-width:980px;margin:32px auto;line-height:1.6'><pre style='white-space:pre-wrap'>{html}</pre></body></html>"
