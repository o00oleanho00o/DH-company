from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .ai import ai_provider
from .config import EXPORT_DIR, RAW_DIR, ROOT_DIR, STORAGE_DIR, ensure_directories, settings
from .db import db_session, dumps, init_db, loads, row_dict, utc_now
from .excel import parse_workbook
from .export import export_project
from .ingest import ingest_directory, ingest_workbook, source_file_detail
from .pricing import catalog_stats, get_project_result, review_item, run_pricing, serialize_boq_item
from .normalize import parse_number


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Nền tảng nhập Excel, matching vật tư/nhân công và báo giá M&E.",
)

STATIC_DIR = ROOT_DIR / "app" / "static"
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
    result["metadata"] = loads(result.pop("metadata_json", "{}"), {})
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
    result["warning_count"] = len(result["metadata"].get("warnings", []))
    return result


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
            "auto_coverage": metrics.get("auto_coverage"),
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
    try:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    finally:
        handle.close()
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
        "timestamp": utc_now(),
    }


@app.get("/api/sources")
def list_sources() -> list[dict[str, Any]]:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM source_files ORDER BY uploaded_at DESC").fetchall()
        return [_source_payload(conn, row) for row in rows]


@app.get("/api/sources/{source_id}")
def get_source(source_id: int) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM source_files WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy nguồn dữ liệu")
        return _source_payload(conn, row)


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int) -> dict[str, Any]:
    storage_key: str | None = None
    with db_session() as conn:
        row = conn.execute("SELECT * FROM source_files WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy nguồn dữ liệu")
        storage_key = row["storage_key"]
        conn.execute("DELETE FROM source_files WHERE id=?", (source_id,))
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
    first_project_id: int | None = None
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
            first_project_id = first_project_id or project_id
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
        kwargs["status"] = "NEEDS_SUPPLIER_QUOTATION"
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
