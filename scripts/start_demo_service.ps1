# Detached launcher: survives tool-session recycling (unlike shells backgrounded
# by an agent harness). Usage: powershell -File scripts\start_demo_service.ps1
param([string]$LogDir = "$PSScriptRoot\..\tmp")
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Missing .venv Python. Install requirements.txt first.'
}
$existing = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output 'A demo service is already listening at http://127.0.0.1:8765. Reuse it.'
    return
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path $LogDir "demo-service-$stamp.log"
$err = Join-Path $LogDir "demo-service-$stamp.err.log"
$process = Start-Process -FilePath $pythonExe `
    -WorkingDirectory $projectDir `
    -ArgumentList @('-m', 'da_mvp.web_demo', '--mode', 'carla', '--no-browser') `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
Write-Output "Demo service detached (PID $($process.Id)), log: $out"
Write-Output 'Page: http://127.0.0.1:8765 - wait for the connection badge to turn live.'
