$ErrorActionPreference = "Stop"
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$bindHost = if ($env:APP_HOST) { $env:APP_HOST } else { "0.0.0.0" }
$bindPort = if ($env:APP_PORT) { $env:APP_PORT } else { "3000" }
& $python -m uvicorn app.main:app --host $bindHost --port $bindPort --reload
