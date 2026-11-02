param([int]$Port = 8765, [string]$DataPath = '', [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONPATH = $PSScriptRoot
$arguments = @('--port', "$Port")
if ($DataPath) { $arguments += @('--data', $DataPath) }
if (-not $NoBrowser) { $arguments += '--open' }
$exe = Join-Path $PSScriptRoot 'FrameForge.exe'
if (Test-Path -LiteralPath $exe) {
    & $exe @arguments
} else {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run Install.ps1 first, or download the Windows portable ZIP.' }
    & $python (Join-Path $PSScriptRoot 'frameforge_launcher.py') @arguments
}
exit $LASTEXITCODE

