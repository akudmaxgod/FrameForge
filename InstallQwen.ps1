param([Parameter(Mandatory=$true)][string]$FromInstallation, [string]$ComponentsPath = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $ComponentsPath) {
    if (Test-Path -LiteralPath '.frameforge-profile.json') { $ComponentsPath = (Get-Content -Raw '.frameforge-profile.json' | ConvertFrom-Json).components_path }
    if (-not $ComponentsPath) { $ComponentsPath = $PSScriptRoot }
}
$sourceRoot = [IO.Path]::GetFullPath($FromInstallation)
$targetRoot = [IO.Path]::GetFullPath($ComponentsPath)
$lock = Get-Content -Raw (Join-Path $PSScriptRoot 'components-lock.json') | ConvertFrom-Json
foreach ($item in $lock.files) {
    if ($item.path -match '(^[/\\]|\.\.|:)') { throw 'Unsafe component path in manifest.' }
    $source = Join-Path $sourceRoot $item.path
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw ('Missing component: ' + $item.path) }
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) { throw ('Component checksum mismatch: ' + $item.path) }
}
foreach ($item in $lock.files) {
    $source = Join-Path $sourceRoot $item.path
    $target = Join-Path $targetRoot $item.path
    if ($source -ieq $target) { continue }
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) { throw ('Target already has another version: ' + $item.path) }
        continue
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) { throw 'Copied component is damaged.' }
}
Write-Host 'Pinned Qwen component set verified. Model inference was not started.'

