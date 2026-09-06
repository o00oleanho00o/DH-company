# Temporal benchmark modes

Historical reproduction and current repricing are evaluated separately.

| Metric | Historical reproduction | Current repricing |
| --- | ---: | ---: |
| As-of date | unknown | 2026-09-06 |
| Priceable rows | 2388 | 2388 |
| Material source coverage | 12.4% | 12.4% |
| Material capture rate | 100.0% | 100.0% |
| Material repricing coverage | 12.4% | 12.4% |
| Historical material reproduction accuracy | 0.0% | n/a |
| Current material relative drift | n/a | 88.2% |
| Labor source coverage | 18.4% | 18.4% |
| Labor capture rate | 44.1% | 44.1% |
| Labor repricing coverage | 8.1% | 8.1% |
| Historical labor reproduction accuracy | 0.0% | n/a |
| Current labor relative drift | n/a | 52.0% |
| Future sources excluded | 0 | 0 |

## Interpretation

- Historical reproduction must use observations eligible at the historical as-of date.
- Current repricing measures usable current coverage and relative drift against the old BOQ price; it is not a claim that the old project price was a supplier net price.
- Unknown quotation dates and undated observations remain explicit uncertainty rather than being silently treated as historical truth.
