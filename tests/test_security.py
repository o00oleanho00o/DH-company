from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.main as main_module


def test_save_upload_rejects_oversized_payload_and_cleans_temp_dir(tmp_path, monkeypatch):
    storage_dir = tmp_path / "storage"
    monkeypatch.setattr(main_module, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(main_module, "MAX_UPLOAD_BYTES", 3)

    upload = SimpleNamespace(filename="too-large.xlsx", file=io.BytesIO(b"1234"))
    with pytest.raises(HTTPException) as exc_info:
        main_module._save_upload(upload)

    assert exc_info.value.status_code == 413
    assert not any(path.is_file() for path in storage_dir.rglob("*"))
    assert not any(
        path.is_dir() and path.name.startswith("upload-")
        for path in storage_dir.rglob("*")
    )
