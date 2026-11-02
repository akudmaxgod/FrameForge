param([string]$ComponentsPath = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $ComponentsPath) {
    if (Test-Path -LiteralPath '.frameforge-profile.json') { $ComponentsPath = (Get-Content -Raw '.frameforge-profile.json' | ConvertFrom-Json).components_path }
    if (-not $ComponentsPath) { $ComponentsPath = $PSScriptRoot }
}
$runtime = Join-Path $ComponentsPath 'backends\voice-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) { throw 'Run InstallVoice.ps1 first.' }
& $runtime -m pip install -r (Join-Path $PSScriptRoot 'requirements-semantic.txt')
if ($LASTEXITCODE -ne 0) { throw 'Semantic runtime installation failed.' }
$env:FRAMEFORGE_COMPONENTS = [IO.Path]::GetFullPath($ComponentsPath)
& $runtime (Join-Path $PSScriptRoot 'scripts\install_semantic_model.py')
if ($LASTEXITCODE -ne 0) { throw 'Semantic model verification failed.' }
Write-Host 'Semantic runtime installed. See docs/COMPONENTS.md for the shared models path.'

