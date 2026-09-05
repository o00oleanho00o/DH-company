# DH M&E Pricing Platform — Architecture Note

## Scope

This phase covers M&E construction BOQ ingestion, material/labor matching,
price application, human review, provenance and Excel export. CAD/OCR takeoff,
panel design automation, ERP and accounting are deliberately out of scope.

## Runtime topology

```text
Browser (Vietnamese enterprise workflow)
          │ REST + file upload/download
          ▼
FastAPI modular monolith
  ├─ Intake / workbook inspector
  ├─ Normalization + validation
  ├─ Catalog / versioned prices
  ├─ Deterministic matching + review state
  ├─ Pricing policy + audit
  └─ Excel export
          │
          ▼
SQLite (dev, zero setup)
  └─ portable canonical schema → PostgreSQL/JSONB/pgvector path
```

The original workbooks are copied to `storage/raw/` and never treated as the
operational database. Every normalized row retains workbook, sheet and source
row provenance plus raw cell/formula JSON.

## Ingestion pipeline

`Workbook snapshots → document/sheet classification → header detection →
synonym/structure column mapping → row classification → normalization →
validation → canonical tables`

`openpyxl` reads `.xlsx` twice (cached values and formulas); `xlrd==2.0.1`
reads legacy BIFF `.xls`. Header position and column order are discovered from
content, not hardcoded. A mapping preview endpoint lets a user inspect the
prediction before importing.

Supplier workbooks use the `TONG` sheet as the canonical price table to avoid
double-counting repeated detail sheets. Labor master rows are retained even
when rate cells are blank. Historical BOQ rows can supply labor/material
observations, while a holdout can be ingested with `exclude_prices=True`.

## Matching and pricing

1. Exact code/name.
2. Normalized tokens and aliases/corrections.
3. Technical attribute extraction (family, voltage, conductor, insulation,
   armour, cores, cross-section, diameters).
4. Cheap token/technical retrieval.
5. Weighted deterministic score and ambiguity margin.
6. Optional provider reranking over a bounded top-candidate set.
7. Human review when a decisive attribute, candidate or price is uncertain.

All arithmetic (discount, parallel-run scaling, totals and rounding) is code.
A price is applied only when a `product_prices` or `labor_rates` row can be
traced to source file/sheet/row. Missing prices are represented as
`NO_PRICE_FOUND`; uncertain matches as `REVIEW_REQUIRED`.

## Data model

The SQLite schema contains source files/sheets/rows, products and versioned
product prices, labor items/rates, projects, BOQ items, pricing runs,
candidate evidence, corrections, aliases and append-only audit events. JSON
columns are intentionally used in development to preserve flexible Excel
metadata; the migration note documents the PostgreSQL `JSONB`/`NUMERIC`/
`pgvector` upgrade path.

## AI provider

`app/ai.py` provides an OpenAI-compatible semantic provider. It reads
`CLAUDE_BASE_URL`, `CLAUDE_API_KEY` and `CLAUDE_MODEL` from the existing `.env`
without logging secrets. It is opt-in (`ENABLE_LLM=true`) and is not required
for deterministic import/pricing. It cannot perform database lookup,
arithmetic or invent prices.

## Reliability and extension points

- Structured audit events and pricing-run metrics support debugging.
- File storage is isolated behind the raw storage path and can move to S3.
- Matching retrieval is isolated so PostgreSQL full-text/pgvector can replace
  the SQLite prefilter.
- Review corrections become aliases/rules without model fine-tuning.
- Production hardening still needs authentication/authorization, a managed
  PostgreSQL instance, background workers, observability export and stronger
  catalog governance.
