param([Parameter(Mandatory=$true)][string]$PreviousInstallation, [string]$PreviousData = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$arguments = @('--prepare-update', $PreviousInstallation)
if ($PreviousData) { $arguments += @('--previous-data', $PreviousData) }
if (Test-Path -LiteralPath '.\FrameForge.exe') {
    & .\FrameForge.exe @arguments
} else {
    & .\.venv\Scripts\python.exe .\frameforge_launcher.py @arguments
}
if ($LASTEXITCODE -ne 0) { throw 'Update not prepared. The previous installation was not replaced.' }

