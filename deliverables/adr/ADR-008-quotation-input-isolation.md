# ADR-008 — Isolate quotation inputs from reference sources

- **Status:** Accepted
- **Date:** 2026-09-08

## Decision

The system retains each quotation input workbook as a `QUOTATION_INPUT` snapshot
for row provenance and formula-preserving export. Source-data APIs and catalog
statistics only expose `REFERENCE` sources. The importer rejects workbooks
created by this system, identified by the `AI Audit` worksheet.

## Consequences

Quotation inputs cannot silently feed later pricing. Export remains reproducible
because the snapshot is not deleted with the temporary upload directory.
