# Holdout pricing benchmark

- Generated: `2026-09-06T07:46:00.435201+00:00`
- Input directory: `input`
- Holdout: `BOQ-HỆ THỐNG ĐIỆN TRẠI LƠN HẢI HÀ-DH290124.xlsx`
- Database: `None`
- Database persisted: **no (temporary)**
- Runtime: **194.931s** (ingest 38.770s, pricing 129.559s, evaluation 6.587s)

## Leakage guard

- Holdout material price rows in catalog: **0**
- Holdout labor rate rows in catalog: **0**
- Result: **PASS**

## Parsing

- Workbooks ingested: 6
- Sheets inspected: 176
- Source rows retained: 15539
- Data rows detected: 11103
- Descriptions present: 2480
- Quantities present: 2358

## BOQ row classification audit

- Parsed BOQ/PANEL rows included: 2829
- Priceable line items (audit view): 2388
- Non-priceable/uncertain rows: 441
- Rows on excluded `OTHER` sheets: 20

| Classification | Rows |
| --- | ---: |
| `PRICEABLE_LINE_ITEM` | 2388 |
| `SECTION` | 68 |
| `SUBSECTION` | 151 |
| `NOTE` | 0 |
| `SUBTOTAL` | 56 |
| `TOTAL` | 6 |
| `HEADER` | 6 |
| `NON_PRICEABLE_REFERENCE` | 0 |
| `UNKNOWN` | 154 |

## Material

- Ground-truth items: 1072
- Predicted/priced: 133 (12.4%)
- Exact price matches: 0 (0.0%)
- Mean relative error: 90.7%
- Median relative error: 97.0%
- Technical/name match accuracy (proxy): 100.0%
- High-confidence price-error rate: 100.0%
- High-confidence wrong-match rate (proxy): 0.0%

## Labor

- Ground-truth items: 1095
- Predicted/priced: 89 (8.1%)
- Exact price matches: 0 (0.0%)
- Mean relative error: 52.0%
- Median relative error: 35.5%
- Technical/name match accuracy (proxy): 100.0%
- High-confidence price-error rate: 100.0%
- High-confidence wrong-match rate (proxy): 0.0%

## Provenance

- Material predictions missing source: 0
- Labor predictions missing source: 0
- Complete material provenance: **PASS**
- Complete labor provenance: **PASS**

## Status distribution

| Status | Rows |
| --- | ---: |
| `AUTO_APPROVED` | 4 |
| `IGNORED` | 50 |
| `NO_MATCH` | 2169 |
| `NO_PRICE_FOUND` | 36 |
| `REVIEW_REQUIRED` | 214 |

## Persisted row classes

- Priceable BOQ rows: 2388
- Non-priceable BOQ rows: 85

| Row class | Rows |
| --- | ---: |
| `PRICEABLE_LINE_ITEM` | 2388 |
| `SECTION` | 4 |
| `SUBSECTION` | 6 |
| `SUBTOTAL` | 40 |
| `UNKNOWN` | 35 |

## Interpretation

Prices from the holdout workbook are retained on BOQ rows as ground truth but are excluded from `product_prices` and `labor_rates`. A price prediction is counted as an exact match within 0.5% (or one đồng) because historical Excel files may round values. Technical/name match accuracy is a conservative proxy because the holdout does not contain catalog IDs. Price errors can therefore reflect project-specific pricing drift even when the item match is plausible. Items with missing/ambiguous matches remain reviewable rather than receiving a fabricated price.
