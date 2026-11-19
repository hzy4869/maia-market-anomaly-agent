$ErrorActionPreference = 'Stop'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Virtual environment missing. Run setup.ps1 first.'
}
Push-Location -LiteralPath $PSScriptRoot
try {
    & $venvPython -m maia.cli examples/synthetic_earnings.json --mode demo
    if ($LASTEXITCODE -ne 0) { throw 'Demo failed. Run setup.ps1 to install/check dependencies.' }
} finally {
    Pop-Location
}
