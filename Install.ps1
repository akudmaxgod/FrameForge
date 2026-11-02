param([string]$Python = 'py', [switch]$Build)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONPATH = ''
$env:PYTHONNOUSERSITE = '1'
if ($Python -eq 'py') { $prefix = @('-3.12') } else { $prefix = @() }
& $Python @prefix -c "import sys; assert sys.version_info[:2] == (3,12), 'Python 3.12 x64 required'; assert sys.maxsize > 2**32, '64-bit Python required'"
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 x64 from python.org and try again.' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $Python @prefix -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated environment.' }
}
$runtime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $runtime -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if ($Build) {
    & $runtime -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Build tools installation failed.' }
}
& $runtime -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependencies conflict; do not publish this environment.' }
& $runtime -c "from imagegen.nlp import RussianNLPAdapter; import av,cv2,openai; print('Core imports OK')"
if ($LASTEXITCODE -ne 0) { throw 'Core import verification failed.' }
Write-Host 'Ready. Run Start.cmd. Optional speech/Qwen components are installed separately.'

