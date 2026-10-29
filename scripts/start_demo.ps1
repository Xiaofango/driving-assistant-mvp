param(
    [switch]$NoBrowser,
    [string]$InstallDir = 'D:\CARLA_MVP\CARLA_0.9.16'
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Missing .venv Python. Install requirements-carla.txt first.'
}
& (Join-Path $PSScriptRoot 'start_carla_low.ps1') -InstallDir $InstallDir
$deadline = (Get-Date).AddSeconds(90)
do {
    $ready = Get-NetTCPConnection -LocalPort 2000 -State Listen -ErrorAction SilentlyContinue
    if ($ready) { break }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if (-not $ready) { throw 'CARLA port 2000 was not ready within 90 seconds.' }
$existing = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output 'A service is already listening at http://127.0.0.1:8765. Reuse it or stop it before restarting.'
    return
}
Push-Location -LiteralPath $projectDir
try {
    $demoArgs = @('-m', 'da_mvp.web_demo', '--mode', 'carla')
    if ($NoBrowser) { $demoArgs += '--no-browser' }
    & $pythonExe @demoArgs
} finally { Pop-Location }
