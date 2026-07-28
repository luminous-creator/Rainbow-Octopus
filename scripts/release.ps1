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

# Clear PYTHONPATH first. This step exists to prove the wheel installs into a
# *clean* environment, and the PYTHONPATH set earlier for the test run made it
# anything but: with `src` on the path, pip discovers src\rainbow_octopus.egg-info,
# concludes the requirement is already satisfied, exits 0, and installs nothing.
# The failure then surfaces as a missing rocto.exe, which points nowhere near
# the cause. An environment variable set for one step must not leak into a step
# whose entire purpose is isolation.
$env:PYTHONPATH = ''

$venv = Join-Path $env:TEMP "rocto-release-$(Get-Random)"
python -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "Could not create a virtualenv at $venv." }

$wheel = ($artifacts | Where-Object Extension -eq '.whl').FullName

# Not --quiet, and the exit code is checked.
#
# This step used to install quietly and never look at the result, so a failed
# install surfaced three lines later as "rocto.exe is not recognized" — which
# points at the wrong thing entirely. A release script that hides the one error
# that matters is worse than no release script.
& "$venv\Scripts\pip.exe" install $wheel
if ($LASTEXITCODE -ne 0) { throw "pip could not install $wheel (see the output above)." }

$rocto = Join-Path $venv 'Scripts\rocto.exe'
if (-not (Test-Path $rocto)) {
    Write-Host "`nInstalled files under Scripts\:" -ForegroundColor Yellow
    Get-ChildItem (Join-Path $venv 'Scripts') | ForEach-Object { Write-Host "  $($_.Name)" }
    throw "The wheel installed but produced no rocto.exe — check [project.scripts] in pyproject.toml."
}

& $rocto --version
if ($LASTEXITCODE -ne 0) { throw 'rocto --version failed.' }
& $rocto --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'rocto --help failed.' }
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
