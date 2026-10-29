$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$schedulerPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $schedulerPython)) {
    throw 'Install the project virtual environment before starting the scheduler.'
}

$stdoutLog = Join-Path $PSScriptRoot 'scheduler.stdout.log'
$stderrLog = Join-Path $PSScriptRoot 'scheduler.stderr.log'
$worker = Start-Process -FilePath $schedulerPython `
    -ArgumentList '-m', 'rd_import.scheduler' `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -Wait -PassThru
exit $worker.ExitCode
