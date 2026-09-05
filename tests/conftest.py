from __future__ import annotations

import os
from pathlib import Path

import pytest

# Tests must remain deterministic and must never spend the operational LLM
# budget or make network calls just because the developer's .env enables the
# semantic provider. Runtime/CLI behavior still reads ENABLE_LLM from .env.
os.environ.setdefault("ENABLE_LLM", "false")

from app import config, db, ingest


ROOT_DIR = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT_DIR / "input"


def find_input_file(prefix: str) -> Path:
    """Return the first workbook whose filename starts with *prefix*.

    The sample directory contains Vietnamese filenames (and one intentional
    typo in the holdout filename), so tests should not duplicate the complete
    names in multiple places.
    """

    matches = sorted(
        path
        for path in INPUT_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".xls", ".xlsx"}
        and path.name.startswith(prefix)
    )
    if not matches:
        raise FileNotFoundError(f"No sample workbook starts with {prefix!r}")
    return matches[0]


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the application at an isolated SQLite database for a test.

    ``DB_PATH`` and the raw/export directories are imported into more than
    one module, hence both the source config and already-imported aliases are
    patched.  This keeps integration tests deterministic and prevents them
    from changing a developer's local ``storage/pricing.db``.
    """

    storage_dir = tmp_path / "storage"
    raw_dir = storage_dir / "raw"
    export_dir = storage_dir / "exports"
    db_path = storage_dir / "pricing.db"

    monkeypatch.setattr(config, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(config, "RAW_DIR", raw_dir)
    monkeypatch.setattr(config, "EXPORT_DIR", export_dir)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(ingest, "RAW_DIR", raw_dir)

    config.ensure_directories()
    db.init_db()
    return db_path
