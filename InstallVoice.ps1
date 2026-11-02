param([string]$Python = 'py', [string]$ComponentsPath = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $ComponentsPath) {
    if (Test-Path -LiteralPath '.frameforge-profile.json') { $ComponentsPath = (Get-Content -Raw '.frameforge-profile.json' | ConvertFrom-Json).components_path }
    if (-not $ComponentsPath) { $ComponentsPath = $PSScriptRoot }
}
$ComponentsPath = [IO.Path]::GetFullPath($ComponentsPath)
$env:PYTHONPATH = $PSScriptRoot
$env:PYTHONNOUSERSITE = '1'
$runtime = Join-Path $ComponentsPath 'backends\voice-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    if ($Python -eq 'py') { $prefix = @('-3.12') } else { $prefix = @() }
    & $Python @prefix -m venv (Join-Path $ComponentsPath 'backends\voice-venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 is required for the optional speech worker.' }
}
& $runtime -m pip install -r (Join-Path $PSScriptRoot 'requirements-voice.txt')
if ($LASTEXITCODE -ne 0) { throw 'Speech dependency installation failed.' }
& $runtime -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Speech dependencies conflict.' }
$env:FF_SPEECH_TARGET = Join-Path $ComponentsPath 'models\voice\turbo'
& $runtime -c "import os; from huggingface_hub import snapshot_download; snapshot_download('mobiuslabsgmbh/faster-whisper-large-v3-turbo', local_dir=os.environ['FF_SPEECH_TARGET'])"
if ($LASTEXITCODE -ne 0) { throw 'Speech model download failed.' }
Write-Host 'Speech installed. Restart FrameForge. This downloads a separate model; it is not a TTS voice generator.'

