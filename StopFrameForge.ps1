param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$base = 'http://127.0.0.1:' + $Port
$capability = Invoke-RestMethod -Uri ($base + '/api/editorial-capabilities') -TimeoutSec 5
if ($capability.scoped_scene_batches -ne $true) { throw 'This port does not identify FrameForge.' }
$session = Invoke-RestMethod -Uri ($base + '/api/session') -TimeoutSec 5
Invoke-RestMethod -Method Post -Uri ($base + '/api/shutdown') -Headers @{'X-Session-Token' = $session.token} -TimeoutSec 5 | Out-Null
Write-Host 'Shutdown requested. Wait for the application window to close. No other processes were terminated.'

