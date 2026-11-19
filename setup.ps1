param(
    [string]$PythonCommand = 'python',
    [string]$IndexUrl = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Invoke-CheckedPython {
    param([string[]]$Arguments)
    & $venvPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python step failed (exit $LASTEXITCODE): $($Arguments -join ' ')"
    }
}

Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $PythonCommand -m venv (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create virtual environment.' }
    }
    Invoke-CheckedPython -Arguments @('-c', 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"; assert sys.prefix != sys.base_prefix, "Expected virtual environment"; print("Using:", sys.executable)')
    $installArgs = @('-m', 'pip', 'install', '--retries', '1', '--timeout', '20', '-e', '.')
    if ($IndexUrl) { $installArgs += @('--index-url', $IndexUrl) }
    Invoke-CheckedPython -Arguments $installArgs
    Invoke-CheckedPython -Arguments @('-m', 'pip', 'check')
    Invoke-CheckedPython -Arguments @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
    Invoke-CheckedPython -Arguments @('-m', 'maia.cli', 'examples/synthetic_earnings.json', '--mode', 'demo')
    $freeze = & $venvPython -m pip freeze --exclude-editable
    if ($LASTEXITCODE -ne 0) { throw 'Could not record installed dependency versions.' }
    $freeze | Set-Content -LiteralPath (Join-Path $projectRoot 'requirements.lock.txt') -Encoding UTF8
    Write-Host 'SUCCESS: dependencies installed, all tests passed, demo report generated.' -ForegroundColor Green
} finally {
    Pop-Location
}
