# One-time local setup for Windows (PowerShell). Run from the project root:  .\scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
& $py -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'; print('Python', sys.version.split()[0])"

if (-not (Test-Path .venv)) { & $py -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env (TRIAGE_BACKEND=mock)" }
& .\.venv\Scripts\python.exe -m pytest -q
Write-Host "`nDone. Next:  .\.venv\Scripts\Activate.ps1 ; python -m claims_triage demo" -ForegroundColor Green
Write-Host "If activation is blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
