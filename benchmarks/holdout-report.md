# Holdout pricing benchmark

- Generated: `2026-09-05T16:41:14.472083+00:00`
- Input directory: `F:\BUL_Product\DH-company\input`
- Holdout: `BOQ-HỆ THỐNG ĐIỆN TRẠI LƠN HẢI HÀ-DH290124.xlsx`
- Database: `C:\Users\email\AppData\Local\Temp\dh-holdout-54jjwv2c\pricing.db`
- Database persisted: **no (temporary)**
- Runtime: **188.143s** (ingest 29.403s, pricing 129.403s, evaluation 0.107s)

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

## Material

- Ground-truth items: 1072
- Predicted/priced: 106 (9.9%)
- Exact price matches: 0 (0.0%)
- Mean relative error: 102.9%
- Median relative error: 104.5%
- Technical/name match accuracy (proxy): 100.0%
- High-confidence price-error rate: 100.0%
- High-confidence wrong-match rate (proxy): 0.0%

## Labor

- Ground-truth items: 1095
- Predicted/priced: 90 (8.2%)
- Exact price matches: 0 (0.0%)
- Mean relative error: 51.6%
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
| `NO_MATCH` | 2269 |
| `NO_PRICE_FOUND` | 113 |
| `REVIEW_REQUIRED` | 87 |

## Interpretation

Prices from the holdout workbook are retained on BOQ rows as ground truth but are excluded from `product_prices` and `labor_rates`. A price prediction is counted as an exact match within 0.5% (or one đồng) because historical Excel files may round values. Technical/name match accuracy is a conservative proxy because the holdout does not contain catalog IDs. Price errors can therefore reflect project-specific pricing drift even when the item match is plausible. Items with missing/ambiguous matches remain reviewable rather than receiving a fabricated price.
