# Bounded LLM reranking experiment

- Experiment generated: `2026-09-06` UTC
- Holdout: `BOQ-HỆ THỐNG ĐIỆN TRẠI LƠN HẢI HÀ-DH290124.xlsx`
- Control: same code version, deterministic mode, no API calls
- Semantic budget: maximum 20 external calls (final run)
- Leakage guard: **PASS** in both runs
- Applied-price provenance: **100%** in both runs
- Portability: this file and its JSON companion embed the canonical compact
  results; gitignored scratch benchmark directories are not required after
  cloning.

## Result

| Metric | Deterministic | Semantic assisted | Delta |
| --- | ---: | ---: | ---: |
| Total runtime | 231.594 s | 456.571 s | +224.977 s |
| Pricing runtime | 149.685 s | 391.107 s | +241.422 s |
| Material priced / evaluable | 133 / 1,072 | 134 / 1,072 | +1 |
| Material coverage | 12.41% | 12.50% | +0.09 pp |
| Labor priced / evaluable | 89 / 1,095 | 89 / 1,095 | 0 |
| Labor coverage | 8.13% | 8.13% | 0 pp |
| AUTO_APPROVED | 4 | 4 | 0 |
| REVIEW_REQUIRED | 214 | 215 | +1 |
| NO_MATCH | 2,169 | 2,169 | 0 |
| NO_PRICE_FOUND | 36 | 35 | -1 |
| Material match accuracy (proxy) | 100% | 100% | 0 |
| Labor match accuracy (proxy) | 100% | 100% | 0 |
| High-confidence wrong-match rate (proxy) | 0% | 0% | 0 |

The final semantic run required approximately **1.97×** the total runtime. Exact
historical price accuracy remained **0%** in both runs, so reranking did not
solve list/net/VAT/discount or project-specific price semantics.

## LLM usage

| Measure | Value |
| --- | ---: |
| External calls | 20 / 20 |
| Rerank attempts | 20 |
| Reranks applied | 1 |
| Model requested review | 19 |
| Attempts after budget exhaustion | 0 |
| Candidates skipped as non-ambiguous | 4,826 |
| Prompt tokens | 89,631 |
| Completion tokens | 6,491 |
| Total tokens | 96,122 |
| Aggregate model latency | 263,620.945 ms |

No API key, authorization value, full prompt, candidate price, or price
provenance is stored in this comparison.

## Reproduce

The paths below are scratch outputs and are intentionally ignored by Git. The
committed result remains the embedded summary above.

```powershell
$env:ENABLE_LLM = "false"
python -m benchmarks.holdout --output-dir benchmarks/deterministic-experiment

$env:ENABLE_LLM = "true"
$env:LLM_MAX_CALLS = "20"
python -m benchmarks.holdout --output-dir benchmarks/llm-experiment-final --enable-llm
```

## Interpretation

The accepted rerank produced one additional material prediction and moved
one row from `NO_PRICE_FOUND` to `REVIEW_REQUIRED`. It did not reduce
`NO_MATCH`, improve labor coverage, or increase automatic approvals.

The reported match-accuracy and false-positive metrics are proxies because
the holdout does not contain canonical catalog IDs. They must not be presented
as human-labeled match accuracy.

## Recommendation

Keep semantic reranking **disabled by default**. The measured gain is too small
relative to latency and token usage. If used operationally, restrict it to
narrow ambiguous material families after deterministic retrieval and stop
attempting reranks immediately when the provider call budget reaches zero.
