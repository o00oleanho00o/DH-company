# Known limitations

- Development uses SQLite and a synchronous in-process pricing run. It is
  suitable for local/demo workloads, not concurrent production traffic.
- Legacy `.xls` output is normalized into a new usable `.xlsx`; `.xlsx` source
  workbooks are copied and updated in place where column mappings are known.
- Formula evaluation depends on cached values stored by Excel. Formulas are
  retained in raw provenance but are not recalculated by Python.
- The supplied labor master contains item names but no populated labor rates;
  historical quotations are therefore the primary labor-rate source.
- Semantic LLM calls are implemented as an opt-in provider but are not needed
  by the default deterministic path. Ambiguous lines intentionally go to
  review instead of being guessed.
- The initial holdout benchmark is a real baseline, not a KPI claim. Price
  policies, supplier date differences and unmatched technical specifications
  can produce low exact-price accuracy; the generated report records the
  measured result and leakage guard.
- No authentication, role-based access, managed object storage, PostgreSQL
  deployment or multi-user job queue is included in this local MVP.
