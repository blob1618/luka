param(
    [switch]$Lint,
    [switch]$Full
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$localPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $localPython) {
    $python = $localPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "No encontré Python. Creá .venv en la raíz del repo o agregá python al PATH."
    }
    $python = $pythonCommand.Source
}

$focusedTests = @(
    "tests/test_investment_guidance.py",
    "tests/test_financial_education.py",
    "tests/test_educational_route.py",
    "tests/test_monthly_balance.py"
)

Push-Location $repoRoot
try {
    Write-Host "Ejecutando pruebas focalizadas de HU-EDU-02..."
    & $python -m pytest -q @focusedTests
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    if ($Lint) {
        Write-Host "Ejecutando Ruff..."
        & $python -m ruff check .
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }

    if ($Full) {
        Write-Host "Ejecutando la suite completa..."
        & $python -m pytest -v
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
} finally {
    Pop-Location
}

Write-Host "Pruebas finalizadas correctamente."
