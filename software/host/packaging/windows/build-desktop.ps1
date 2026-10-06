$ErrorActionPreference = "Stop"

$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepositoryRoot

if (-not (Test-Path ".venv")) {
    py -3.10 -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -e ".[desktop,build]"
& ".venv\Scripts\python.exe" tools\build_factory_hmi.py desktop

Write-Host "Desktop package written under dist\"
