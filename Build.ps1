param([string]$Python = '.venv\Scripts\python.exe')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONPATH = $PSScriptRoot
& $Python -m PyInstaller --noconfirm --clean frameforge.spec
if ($LASTEXITCODE -ne 0) { throw 'Portable build failed.' }
& $Python scripts\package_release.py --portable dist\FrameForge
if ($LASTEXITCODE -ne 0) { throw 'Packaging or release audit failed.' }
Write-Host 'Candidate prepared. Run the clean-install and update tests before publishing.'

