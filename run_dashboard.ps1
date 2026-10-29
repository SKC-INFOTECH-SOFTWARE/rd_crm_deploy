$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$dashboardPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $dashboardPython)) {
    throw 'Install the project virtual environment first.'
}
if (-not $env:RD_SECRET_KEY) {
    throw 'Set RD_SECRET_KEY before launching the authenticated CRM.'
}

Write-Host 'Sales CRM: http://127.0.0.1:8501'
Write-Host 'Run .\run_scheduler.ps1 in a separate terminal for local scheduler testing.'
& $dashboardPython -m rd_web.app
