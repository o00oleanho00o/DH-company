# Round 2 benchmark report

- Generated: `2026-09-06T07:50:07.619003+00:00`
- Holdout: `BOQ-HỆ THỐNG ĐIỆN TRẠI LƠN HẢI HÀ-DH290124.xlsx`
- Round 1 source: `benchmarks/baseline-round1.json`
- Round 2 source: `benchmarks/holdout-report.json`

## Before / after

| Metric | Kind | Round 1 | Round 2 | Delta |
| --- | --- | ---: | ---: | ---: |
| BOQ items (total) | `count` | 2473 | 2473 | +0 |
| Priceable rows (audit) | `count` | 2388 | 2388 | +0 |
| Material ground-truth items | `count` | 1072 | 1072 | +0 |
| Material priced items | `count` | 106 | 133 | +27 |
| Material priced coverage | `ratio` | 9.9% | 12.4% | +2.5 pp |
| Material source-supported items | `count` | n/a | 2077 | n/a |
| Material engine capture rate | `ratio` | n/a | 6.4% | n/a |
| Labor ground-truth items | `count` | 1095 | 1095 | +0 |
| Labor priced items | `count` | 90 | 89 | -1 |
| Labor priced coverage | `ratio` | 8.2% | 8.1% | -0.1 pp |
| Labor source-supported items | `count` | n/a | 2273 | n/a |
| Labor engine capture rate | `ratio` | n/a | 8.9% | n/a |
| AUTO_APPROVED | `count` | 4 | 4 | +0 |
| REVIEW_REQUIRED | `count` | 87 | 214 | +127 |
| MISSING_SOURCE_DATA failures | `count` | n/a | 115 | n/a |
| MATCH_FAILURE failures | `count` | n/a | 2019 | n/a |
| High-confidence wrong matches | `count` | 0 | 0 | +0 |
| Provenance complete | `bool` | PASS | PASS | unchanged |
| Runtime (seconds) | `seconds` | 188.143s | 194.931s | +6.788s |

## Round 2 interpretation

- Round 1 metrics are the immutable baseline snapshot from the previous report.
- Round 2 adds explicit row classification, source-supported coverage and capture-rate metrics.
- A missing Round 1 value means that metric was not measured, not zero.
- Price exact-match remains 0 in both snapshots; current supplier repricing and historical project prices are separate temporal questions.

### Round 2 source-gap summary

- Material source-supported items: 2077
- Labor source-supported items: 2273
- Main category gap counts: `{"MATCH_FAILURE": 14, "MISSING_SOURCE_DATA": 2, "NO_PRICE_FOUND": 1}`

Detailed row-level evidence remains in the linked data-gap, failure-analysis, temporal-mode and row-classification artifacts referenced by `benchmarks/holdout-report.json`.
