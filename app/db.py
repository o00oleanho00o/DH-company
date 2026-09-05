from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH, ensure_directories


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    detected_type TEXT NOT NULL DEFAULT 'UNKNOWN',
    confirmed_type TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    parsing_version TEXT NOT NULL DEFAULT '1.0',
    processing_status TEXT NOT NULL DEFAULT 'PENDING',
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_sheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    sheet_name TEXT NOT NULL,
    sheet_index INTEGER NOT NULL,
    detected_type TEXT NOT NULL DEFAULT 'UNKNOWN',
    header_row INTEGER,
    table_range TEXT,
    mapping_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_file_id, sheet_name)
);

CREATE TABLE IF NOT EXISTS source_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_sheet_id INTEGER NOT NULL REFERENCES source_sheets(id) ON DELETE CASCADE,
    row_no INTEGER NOT NULL,
    raw_cells_json TEXT NOT NULL DEFAULT '{}',
    row_kind TEXT NOT NULL DEFAULT 'empty',
    parse_warnings_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(source_sheet_id, row_no)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    normalized_name TEXT NOT NULL,
    product_code TEXT,
    category TEXT,
    subcategory TEXT,
    brand TEXT,
    origin TEXT,
    unit TEXT,
    technical_attributes_json TEXT NOT NULL DEFAULT '{}',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_code ON products(product_code);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(normalized_name);
CREATE INDEX IF NOT EXISTS idx_source_files_sha256 ON source_files(sha256);

CREATE TABLE IF NOT EXISTS product_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    source_sheet_id INTEGER REFERENCES source_sheets(id) ON DELETE SET NULL,
    source_row_id INTEGER REFERENCES source_rows(id) ON DELETE SET NULL,
    supplier TEXT,
    list_price REAL,
    discount REAL,
    net_price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'VND',
    tax_mode TEXT NOT NULL DEFAULT 'ex_vat',
    effective_date TEXT,
    valid_to TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    calc_json TEXT NOT NULL DEFAULT '{}',
    is_approved INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prices_product_date ON product_prices(product_id, effective_date DESC);

CREATE TABLE IF NOT EXISTS labor_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    normalized_name TEXT NOT NULL,
    code TEXT,
    category TEXT,
    unit TEXT,
    technical_attributes_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_labor_name ON labor_items(normalized_name);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    customer TEXT,
    quotation_date TEXT,
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labor_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    labor_item_id INTEGER NOT NULL REFERENCES labor_items(id) ON DELETE CASCADE,
    source_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    source_sheet_id INTEGER REFERENCES source_sheets(id) ON DELETE SET NULL,
    source_row_id INTEGER REFERENCES source_rows(id) ON DELETE SET NULL,
    rate REAL NOT NULL,
    effective_date TEXT,
    policy_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_labor_rates_item_date ON labor_rates(labor_item_id, effective_date DESC);

CREATE TABLE IF NOT EXISTS pricing_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    policy_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    model_usage_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boq_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    pricing_run_id INTEGER REFERENCES pricing_runs(id) ON DELETE SET NULL,
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    source_sheet_id INTEGER REFERENCES source_sheets(id) ON DELETE SET NULL,
    source_row_id INTEGER REFERENCES source_rows(id) ON DELETE SET NULL,
    section TEXT,
    raw_description TEXT NOT NULL,
    normalized_description TEXT NOT NULL,
    raw_cells_json TEXT NOT NULL DEFAULT '{}',
    product_code TEXT,
    brand TEXT,
    origin TEXT,
    unit TEXT,
    quantity REAL,
    material_price REAL,
    labor_price REAL,
    material_total REAL,
    labor_total REAL,
    matched_product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    matched_labor_item_id INTEGER REFERENCES labor_items(id) ON DELETE SET NULL,
    material_confidence REAL,
    labor_confidence REAL,
    material_source_json TEXT NOT NULL DEFAULT '{}',
    labor_source_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',
    risk TEXT,
    explanation TEXT,
    alternatives_json TEXT NOT NULL DEFAULT '[]',
    reviewed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_boq_project_status ON boq_items(project_id, status);

CREATE TABLE IF NOT EXISTS match_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pricing_run_id INTEGER REFERENCES pricing_runs(id) ON DELETE CASCADE,
    boq_item_id INTEGER NOT NULL REFERENCES boq_items(id) ON DELETE CASCADE,
    candidate_type TEXT NOT NULL,
    candidate_id INTEGER,
    rank_no INTEGER NOT NULL,
    score REAL NOT NULL,
    score_components_json TEXT NOT NULL DEFAULT '{}',
    explanation TEXT
);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    boq_item_id INTEGER REFERENCES boq_items(id) ON DELETE SET NULL,
    raw_description TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    old_candidate TEXT,
    selected_candidate TEXT,
    rule_type TEXT NOT NULL DEFAULT 'manual_selection',
    created_by TEXT NOT NULL DEFAULT 'engineer',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL,
    canonical TEXT NOT NULL,
    rule_type TEXT NOT NULL DEFAULT 'alias',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(alias, canonical, rule_type)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_path() -> Path:
    # Keep a simple SQLite path for dev; DATABASE_URL can be extended later.
    return DB_PATH


def connect() -> sqlite3.Connection:
    ensure_directories()
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Multiple browser/API requests can legitimately overlap during imports
    # and review actions.  A short busy timeout lets SQLite wait for the
    # writer instead of surfacing a transient "database is locked" error.
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
