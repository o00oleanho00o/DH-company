-- DH M&E Pricing Hub - initial SQLite schema
--
-- This migration is intentionally executable with SQLite 3.x and mirrors the
-- runtime schema in app/db.py. The local MVP uses SQLite so a fresh checkout
-- can be initialized without a separate database service:
--
--   sqlite3 storage/pricing.db ".read migrations/001_initial.sql"
--
-- app.db.init_db() applies the same idempotent DDL through
-- sqlite3.Connection.executescript(). Keep both definitions in sync when the
-- schema changes. PostgreSQL deployment can translate TEXT JSON columns to
-- JSONB and REAL money columns to NUMERIC while preserving this provenance
-- and foreign-key graph.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content_sha256 TEXT,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    detected_type TEXT NOT NULL DEFAULT 'UNKNOWN',
    confirmed_type TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    parsing_version TEXT NOT NULL DEFAULT '1.0',
    processing_status TEXT NOT NULL DEFAULT 'PENDING',
    lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE',
    status_reason TEXT,
    archived_at TEXT,
    superseded_by_source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    supersedes_source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    version_no INTEGER NOT NULL DEFAULT 1,
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
    lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE',
    status_reason TEXT,
    archived_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_code ON products(product_code);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(normalized_name);
CREATE INDEX IF NOT EXISTS idx_source_files_sha256 ON source_files(sha256);
CREATE INDEX IF NOT EXISTS idx_source_files_content_sha256 ON source_files(content_sha256);
CREATE INDEX IF NOT EXISTS idx_source_files_lifecycle ON source_files(lifecycle_status);

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

CREATE INDEX IF NOT EXISTS idx_prices_product_date
    ON product_prices(product_id, effective_date DESC);

-- Immutable source observations. ``product_prices`` is the operational
-- selection while this table preserves every supplier list/VAT/discount tier.
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    source_sheet_id INTEGER REFERENCES source_sheets(id) ON DELETE SET NULL,
    source_row_id INTEGER REFERENCES source_rows(id) ON DELETE SET NULL,
    source_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    supplier TEXT,
    observation_type TEXT NOT NULL DEFAULT 'supplier_list',
    list_price REAL,
    discount REAL,
    net_price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'VND',
    tax_mode TEXT NOT NULL DEFAULT 'ex_vat',
    price_basis TEXT NOT NULL DEFAULT 'net',
    effective_date TEXT,
    valid_to TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    context_json TEXT NOT NULL DEFAULT '{}',
    calc_json TEXT NOT NULL DEFAULT '{}',
    is_approved INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_observations_product_date
    ON price_observations(product_id, effective_date DESC);

CREATE TABLE IF NOT EXISTS labor_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    normalized_name TEXT NOT NULL,
    code TEXT,
    category TEXT,
    unit TEXT,
    technical_attributes_json TEXT NOT NULL DEFAULT '{}',
    lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE',
    status_reason TEXT,
    archived_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_labor_name ON labor_items(normalized_name);

CREATE TABLE IF NOT EXISTS catalog_source_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    labor_item_id INTEGER REFERENCES labor_items(id) ON DELETE CASCADE,
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    source_sheet_id INTEGER REFERENCES source_sheets(id) ON DELETE SET NULL,
    source_row_id INTEGER REFERENCES source_rows(id) ON DELETE SET NULL,
    relation_type TEXT NOT NULL DEFAULT 'ingested',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_source_links_product
    ON catalog_source_links(entity_type, product_id, source_file_id);
CREATE INDEX IF NOT EXISTS idx_catalog_source_links_labor
    ON catalog_source_links(entity_type, labor_item_id, source_file_id);
CREATE INDEX IF NOT EXISTS idx_catalog_source_links_source
    ON catalog_source_links(source_file_id, entity_type);

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

CREATE INDEX IF NOT EXISTS idx_labor_rates_item_date
    ON labor_rates(labor_item_id, effective_date DESC);

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
    line_class TEXT,
    status_reason TEXT,
    risk TEXT,
    explanation TEXT,
    alternatives_json TEXT NOT NULL DEFAULT '[]',
    reviewed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_boq_project_status
    ON boq_items(project_id, status);

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

-- Explicit schema version for tools that inspect SQLite metadata.
PRAGMA user_version = 2;
