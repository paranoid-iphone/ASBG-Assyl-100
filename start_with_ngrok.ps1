$ErrorActionPreference = "Stop"

if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    throw "ngrok не найден. Установите его и добавьте токен командой ngrok config add-authtoken."
}

$pythonCommand = "python"
if (Test-Path ".venv\Scripts\python.exe") {
    $pythonCommand = ".venv\Scripts\python.exe"
}

$ngrokProcess = Start-Process ngrok -ArgumentList @("http", "8000") -WindowStyle Hidden -PassThru
try {
    Start-Sleep -Seconds 2
    & $pythonCommand -m uvicorn app.main:app --host 0.0.0.0 --port 8000
}
finally {
    if ($ngrokProcess -and -not $ngrokProcess.HasExited) {
        Stop-Process -Id $ngrokProcess.Id
    }
}

