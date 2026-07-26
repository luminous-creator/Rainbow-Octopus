<#
    Publish Rainbow Octopus to PyPI.

    A ready-built, twine-checked wheel and sdist are already sitting in dist\.
    This script re-verifies them and uploads. Run -TestPyPI first if you want a
    dry run against test.pypi.org.

    Before the first upload:
      1. https://pypi.org/manage/account/token/  ->  create an API token
      2. Username is literally  __token__
      3. Password is the token, including the  pypi-  prefix

    Usage:
        powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Check
        powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -TestPyPI
        powershell -ExecutionPolicy Bypass -File scripts\release.ps1
#>
[CmdletBinding()]
param(
    [switch]$Rebuild,
    [switch]$TestPyPI,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

Step 'Tests'
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw 'Tests failed — not releasing.' }

if ($Rebuild) {
    Step 'Rebuild'
    python -m pip install --quiet --upgrade build twine
    Remove-Item dist\*.whl, dist\*.tar.gz -ErrorAction SilentlyContinue
    python -m build
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
} else {
    python -m pip install --quiet --upgrade twine
}

$artifacts = Get-ChildItem dist\rainbow_octopus-*.whl, dist\rainbow_octopus-*.tar.gz -ErrorAction SilentlyContinue |
             Where-Object { $_.Name -notlike '*STALE*' }
if (-not $artifacts) { throw 'Nothing in dist\. Re-run with -Rebuild.' }

Step 'Artifacts'
$artifacts | ForEach-Object { Write-Host ("  {0,-52} {1,7:N0} KB" -f $_.Name, ($_.Length / 1KB)) }

Step 'twine check'
python -m twine check @($artifacts.FullName)
if ($LASTEXITCODE -ne 0) { throw 'twine check failed.' }

Step 'Clean-install smoke test'
$venv = Join-Path $env:TEMP "rocto-release-$(Get-Random)"
python -m venv $venv
& "$venv\Scripts\pip.exe" install --quiet ($artifacts | Where-Object Extension -eq '.whl').FullName
& "$venv\Scripts\rocto.exe" --version
& "$venv\Scripts\rocto.exe" --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Installed package is broken.' }
Remove-Item $venv -Recurse -Force -ErrorAction SilentlyContinue
Write-Host '  install + CLI OK' -ForegroundColor Green

if ($Check) {
    Write-Host "`nAll checks passed. Re-run without -Check to upload." -ForegroundColor Green
    exit 0
}

Step 'Upload'
Write-Host '  Username: __token__' -ForegroundColor DarkGray
Write-Host '  Password: your pypi-... API token' -ForegroundColor DarkGray

if ($TestPyPI) {
    python -m twine upload --repository testpypi @($artifacts.FullName)
    $url = 'https://test.pypi.org/project/rainbow-octopus/'
} else {
    python -m twine upload @($artifacts.FullName)
    $url = 'https://pypi.org/project/rainbow-octopus/'
}
if ($LASTEXITCODE -ne 0) { throw 'Upload failed.' }

Write-Host "`nPublished: $url" -ForegroundColor Green
Write-Host 'Verify from a clean machine:' -ForegroundColor DarkGray
Write-Host '  python -m pip install rainbow-octopus' -ForegroundColor DarkGray
Write-Host '  rocto doctor' -ForegroundColor DarkGray
