$ErrorActionPreference = "Stop"
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
