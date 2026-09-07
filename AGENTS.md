# Agent Instructions

## Context First
- Read `README.md`, then `docs/DECISIONS.md` and relevant deliverables before changing code.
- Documentation order is mandatory: `DECISIONS → SCOPE → SPEC → MODULEMAP → ARCH → ADR → WBS`.
- WBS is derived state and is always updated last.
- Preserve user files under `input/` and local runtime data under `storage/`; never commit `.env`.

## Runtime
- Python dependencies: `python -m pip install -r requirements.txt`.
- Initialize database: `python -m app.cli init-db`.
- Run server: `.\run.ps1` (`0.0.0.0:3000` by default).
- Full test: `python -m pytest -q`.

## File-Scoped Commands
| Task | Command |
|---|---|
| Test module | `python -m pytest -q tests/test_<module>.py` |
| Compile file | `python -m py_compile app/<module>.py` |
| Search API routes | `rg -n "^@app\\." app/main.py` |

## Key Conventions
- Pricing arithmetic and workbook formulas are deterministic; AI may only rerank existing candidates.
- Material and labor matching are independent and may combine different sources.
- Never apply a price without workbook/sheet/row provenance.
- Export only writes material/labor unit-price cells; preserve all other values, formulas and layout.
- Archive/reprocess referenced sources; do not hard-delete provenance.
- Tests must force `ENABLE_LLM=false` unless explicitly testing the provider.

## Documentation Map
- Decisions: `docs/DECISIONS.md`
- Scope/spec/module map/architecture/WBS: `deliverables/*-DHBG1.md`
- Technical decisions: `deliverables/adr/`

## Commit Attribution
AI commits must include `Co-Authored-By: Codex <noreply@openai.com>`.
