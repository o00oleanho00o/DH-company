from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_default_server_bind_is_network_accessible_on_port_3000() -> None:
    env = os.environ.copy()
    env.pop("APP_HOST", None)
    env.pop("APP_PORT", None)
    code = "from app.config import settings; print(f'{settings.host}:{settings.port}')"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "0.0.0.0:3000"
