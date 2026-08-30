# PrivatePlan setup - Windows / PowerShell
# Creates a repo-local .venv and installs dependencies. Nothing leaves your machine.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "Python not found on PATH. Install Python 3.11+ and retry." }

$version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "Using Python $version"
if ([version]$version -lt [version]"3.11") { throw "Python 3.11 or newer is required (found $version)." }

if (-not (Test-Path ".venv")) {
    Write-Host "Creating .venv ..."
    & python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Done. Next:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  python server.py"
Write-Host "  then open http://127.0.0.1:5000/finance and load the sample household"
