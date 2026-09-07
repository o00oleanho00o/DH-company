# DH M&E Pricing Engine — Round 2 Final Report

Generated on **September 6, 2026** (the benchmark timestamp is stored in UTC).
The report is based on the supplied `.xls/.xlsx` workbooks and a leakage-safe
holdout run; it does not treat an unavailable source as a matcher failure.

## 1. Baseline reproduced

The immutable Round 1 snapshot is stored in
`benchmarks/baseline-round1.json`. The same holdout workbook was evaluated
without inserting its prices into the searchable catalog.

| Metric | Round 1 |
| --- | ---: |
| BOQ rows | 2,473 |
| Material ground-truth rows | 1,072 |
| Material priced | 106 (9.89%) |
| Labor ground-truth rows | 1,095 |
| Labor priced | 90 (8.22%) |
| AUTO_APPROVED | 4 |
| Leakage guard | PASS |

## 2. Root causes found

The low baseline coverage was not one problem:

- The holdout contains structural headings, subtotals and unresolved rows
  mixed with actual line items.
- Supplier workbooks expose list price, VAT-inclusive price and many discount
  tiers, while the old ingestion path effectively selected list price and
  treated discount as zero.
- Product identity and price history were coupled, so repeated observations
  could overwrite the business meaning of a source.
- Current supplier prices and historical project quotation prices are not the
  same temporal/commercial target.
- The catalog has broad category presence but weak identity retrieval for
  protection equipment, panels, accessories and free-text work descriptions.
- Labor prices are project observations with variance, not one universal
  normative rate.

## 3. Bugs and design issues fixed

- Generalized `.xls/.xlsx` header detection and metadata extraction.
- Preserved all supplier price observations in immutable
  `price_observations`; `product_prices` is now the selected operational view.
- Added explicit manual pricing rules through
  `MANUAL_PRICING_RULES_JSON`; no discount is guessed by default.
- Added ex-VAT/inc-VAT and discount-tier provenance.
- Enriched product identity from detail sheets without duplicating canonical
  prices.
- Added LV/MV inference from voltage attributes and generic technical parsing.
- Added persistent row classification, status reason and actionable
  explanations.
- Added deterministic labor aggregation and statistics.
- Bounded LLM reranking to supplied candidate IDs only; it cannot create a
  price, quantity, candidate or provenance.
- Hardened sidebar navigation and exposed server-side AI health without
  exposing the API key.

## 4. BOQ row classification

The audit classified **2,829** parsed BOQ/PANEL rows:

| Class | Rows |
| --- | ---: |
| `PRICEABLE_LINE_ITEM` | 2,388 |
| `SECTION` | 68 |
| `SUBSECTION` | 151 |
| `SUBTOTAL` | 56 |
| `TOTAL` | 6 |
| `HEADER` | 6 |
| `UNKNOWN` | 154 |

Structural rows are retained with provenance and explanation. `UNKNOWN` rows
remain reviewable but are excluded from KPI denominators; they are not silently
deleted.

## 5. Pricing policy

The default deterministic policy is:

1. current approved supplier observation;
2. approved/internal observation when present;
3. recent historical exact-item observation;
4. similar historical reference only as a review candidate;
5. review or supplier quotation when no safe source exists.

The selected source carries supplier, tax mode, list/net amount, effective date,
source file/sheet/row and a deterministic explanation. A configured manual rule
may select a published discount tier or calculate a clearly labelled discount
from a source list amount.

## 6. Material benchmark before/after

| Metric | Round 1 | Round 2 | Delta |
| --- | ---: | ---: | ---: |
| Ground-truth material rows | 1,072 | 1,072 | 0 |
| Material priced | 106 | 133 | +27 |
| Priced coverage | 9.89% | 12.41% | +2.52 pp |
| Mean relative error | 102.9% | 90.7% | -12.2 pp |
| Exact historical price accuracy | 0% | 0% | unchanged |
| Technical/name match proxy | not measured | 100% (133/133) | new metric |
| High-confidence wrong-match proxy | 0 | 0 | unchanged |

The technical match proxy is not human-labelled identity truth; price accuracy
remains zero because commercial semantics still differ from the historical
quotation.

## 7. Labor benchmark before/after

| Metric | Round 1 | Round 2 | Delta |
| --- | ---: | ---: | ---: |
| Ground-truth labor rows | 1,095 | 1,095 | 0 |
| Labor priced | 90 | 89 | -1 |
| Priced coverage | 8.22% | 8.13% | -0.09 pp |
| Mean relative error | 51.6% | 52.0% | +0.4 pp |
| Policy support | latest row | latest/median last N + adjustment | improved |
| High-confidence wrong-match proxy | 0 | 0 | unchanged |

Labor selection now records count, latest/min/max/mean/median, spread,
coefficient of variation, recency, observation IDs and source provenance.
High-spread or high-variance suggestions require review.

## 8. Coverage by category

The category report is in `benchmarks/data-gap-report.md`. Selected results:

| Category | Priceable | Source-supported | Matched | Priced | Capture |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cable | 195 | 195 | 61 | 48 | 31.3% |
| Lighting | 347 | 347 | 113 | 99 | 32.6% |
| Pipe | 125 | 125 | 23 | 23 | 18.4% |
| Earthing | 26 | 26 | 22 | 22 | 84.6% |
| MCB/MCCB/Protection | 605 | 605 | 2 | 2 | 0.3% |
| Panel | 214 | 214 | 4 | 0 | 1.9% |
| Labor-only | 91 | 0 | 0 | 0 | 0% |
| External quotation | 24 | 0 | 0 | 0 | 0% |
| Unknown | 406 | 406* | 0 | 0 | 0% |

\*`source-supported` is a theoretical category-level upper bound, not proof
that the exact product identity exists.

## 9. Source-supported coverage

Across the **2,388** priceable rows:

- material source-supported upper bound: **2,077 (87.0%)**;
- labor source-supported upper bound: **2,273 (95.2%)**;
- combined category-level source-supported upper bound: **2,273 (95.2%)**.

These figures answer “could the current catalog category support this row?”,
not “does the catalog contain the exact SKU?”

## 10. Engine capture rate

Within the source-supported upper bound:

- material matched/priced: **133 / 2,077 = 6.4%**;
- labor matched: **202 / 2,273 = 8.9%**;
- labor priced: **89 / 2,273 = 3.9%**;
- combined matched: **254 / 2,273 = 11.2%**;
- combined priced: **218 / 2,273 = 9.6%**.

The gap between source presence and identity capture is now measurable and is
the next matcher/data-quality workstream; it is not hidden behind `NO_MATCH`.

## 11. Data-gap analysis

The detailed report (`benchmarks/data-gap-report.json`) routes failures into:

- `MATCH_FAILURE`: 2,019 rows in the sampled holdout analysis;
- `MISSING_SOURCE_DATA`: 115 rows;
- `NO_PRICE_FOUND`: 36 rows;
- `REVIEW_REQUIRED`: 214 rows.

The category-level main-gap summary has 14 `MATCH_FAILURE` categories, 2
`MISSING_SOURCE_DATA` categories and 1 `NO_PRICE_FOUND` category. The two
clearest source gaps are labor-only bundle work and external fan/equipment
quotations.

## 12. Historical reproduction vs current repricing

`benchmarks/temporal-modes-report.md` evaluates both meanings separately:

- **Historical reproduction:** the holdout quotation date cannot be safely
  inferred from compact code `DH290124`; historical as-of is therefore marked
  unknown/uncertain rather than guessed. Exact reproduction accuracy is 0%.
- **Current repricing:** runtime cutoff is recorded as **September 6, 2026
  UTC (September 6, 2026 in Asia/Saigon)**. Current material relative drift is
  **88.2%** and labor drift is **52.0%** against the old project values.

The drift is evidence that the benchmark ground truth is not proven to be a
supplier net price for the current catalog.

## 13. Discount/VAT/net-price findings

Direct inspection of the two CADI-SUN cable workbooks found:

- `TONG` exposes base ex-VAT and inc-VAT columns;
- the low-voltage sheet publishes discount tiers from 10% through 30%;
- the medium-voltage `TONG` sheet also preserves base/VAT observations;
- detail sheets carry construction, standard and voltage context;
- the workbook does not identify which commercial tier applies to this
  customer/project.

Therefore the engine stores every observation, defaults to the base ex-VAT
observation, and requires an explicit business rule before applying a discount.
No VAT conversion or discount is silently invented.

## 14. LLM experiment

The bounded experiment is documented in
`benchmarks/llm-experiment-comparison.md`:

- deterministic: 231.594 s, material 133, labor 89;
- semantic-assisted: 456.571 s, material 134, labor 89;
- external calls: 20/20; rerank attempts: 20/20;
- tokens: 96,122;
- reranks applied: 1; model-review reranks: 19;
- `AUTO_APPROVED`: 4 in both runs;
- `NO_MATCH`: 2,169 in both runs;
- exact price accuracy: 0% in both runs.

The gain was only one additional material row at roughly 1.97× runtime. Labor,
`AUTO_APPROVED` and `NO_MATCH` did not improve. LLM is therefore **disabled by
default**. If explicitly enabled, the engine stops attempting reranks after
the call budget is exhausted.

## 15. Test status

Verified before commit:

- `python -m pytest -q`: **113 passed** after the final
  catalog/source-lifecycle, security and pricing-policy tests;
- `python -m compileall -q app benchmarks`: PASS;
- `node --check app/static/app.js`: PASS;
- `git diff --check`: PASS;
- deterministic holdout rerun (`ENABLE_LLM=false`): **194.931 s**;
- browser smoke: all sidebar routes and API endpoints returned successfully;
- leakage guard: PASS;
- applied-price provenance: 100%;
- hallucinated numeric prices: 0%.

## 16. Remaining failure

The largest remaining issue is identity retrieval for rows whose descriptions
are broad, bundled or equipment-specific. High-confidence wrong-match proxy
remains zero, but coverage is intentionally conservative: uncertain rows go to
review rather than receiving a fabricated price. Exact historical price
reproduction also remains unresolved until commercial semantics and project
discounts are supplied.

## 17. Data the business should add

Prioritized requests are generated in `docs/DATA-REQUIREMENTS-NEXT.md`:

1. current supplier net-price files for lighting, switchgear, MCB/MCCB,
   panels, trays and accessories;
2. explicit supplier/category discount rules and VAT basis;
3. approved panel BOMs and fabrication prices;
4. transformer and external-equipment quotations;
5. labor master plus at least 2–3 recent project quotations with dates;
6. normalized SKU/code mappings and aliases for recurring BOQ language.

The report estimates potentially unlockable rows by category; those estimates
are not promises of exact coverage.

## 18. Demo flow

1. Start the local app with `python -m uvicorn app.main:app --host
   0.0.0.0 --port 3000`.
2. Open `http://127.0.0.1:3000`.
3. In **Kho dữ liệu**, preview/import supplier, labor and historical files.
4. In **Báo giá**, upload a new BOQ and run deterministic pricing.
5. Inspect candidate, status, reason and source coordinates in **Bàn rà soát**.
6. Approve a sourced candidate, enter a manual price with a note, request a
   supplier quote, or ignore a structural row.
7. Export the quotation workbook; the `AI Audit` sheet preserves the decision
   trail.
8. `/api/health` shows whether optional semantic assistance is enabled and
   configured, without returning the API key.

## 19. Next recommended engineering step

Use the failure report to improve generic structured retrieval and source
ingestion for the largest categories (MCB/MCCB, panels, cable accessories and
external equipment), then rerun the same leakage-safe benchmark on at least
two additional projects. Keep the commercial discount/VAT policy explicit and
continue measuring source coverage, capture, high-confidence false matches and
temporal drift independently.
