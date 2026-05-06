# run_demo.ps1 — abre broker + 2 clientes em janelas separadas (Windows).
#
# Uso:
#   powershell -ExecutionPolicy Bypass -File .\run_demo.ps1
#
# Cada janela ativa o venv local (.venv) e roda um componente. Feche as
# janelas (ou Ctrl+C) para encerrar.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvActivate = Join-Path $root ".venv\Scripts\activate.bat"

if (-not (Test-Path $venvActivate)) {
    Write-Host "venv não encontrada em $venvActivate"
    Write-Host "Crie com:  python -m venv .venv ; .\.venv\Scripts\activate ; pip install -r requirements.txt"
    exit 1
}

function Open-Window($title, $cmd) {
    $full = "title $title && call `"$venvActivate`" && cd /d `"$root`" && $cmd"
    Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $full | Out-Null
}

Write-Host "Abrindo broker..."
Open-Window "BROKER" "python -u broker.py"
Start-Sleep -Seconds 1

Write-Host "Abrindo cliente 1 (alice)..."
Open-Window "CLIENT alice" "python -u client.py"
Start-Sleep -Milliseconds 500

Write-Host "Abrindo cliente 2 (bob)..."
Open-Window "CLIENT bob" "python -u client.py"

Write-Host ""
Write-Host "Pronto. Em cada janela de cliente:"
Write-Host "  1. Digite o ID (alice / bob)"
Write-Host "  2. Entre na mesma sala (ex: A) para se verem"
Write-Host "  3. Comandos: l (online)  s (sala)  m <texto> (msg)  q (sair)"
