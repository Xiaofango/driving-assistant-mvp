param(
    [string]$InstallDir = 'D:\CARLA_MVP\CARLA_0.9.16'
)

$carlaExe = Join-Path $InstallDir 'CarlaUE4.exe'
if (-not (Test-Path -LiteralPath $carlaExe -PathType Leaf)) {
    throw "CARLA executable not found: $carlaExe"
}

$listener = Get-NetTCPConnection -LocalPort 2000 -State Listen -ErrorAction SilentlyContinue |
    Where-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName -like 'CarlaUE4*' } |
    Select-Object -First 1
if ($listener) {
    Write-Output "CARLA is already listening on port 2000 (PID $($listener.OwningProcess))."
    return
}

$process = Start-Process -FilePath $carlaExe `
    -WorkingDirectory $InstallDir `
    -ArgumentList @('-quality-level=Low', '-RenderOffScreen', '-ResX=800', '-ResY=600') `
    -WindowStyle Hidden -PassThru

Write-Output "CARLA started (PID $($process.Id)). Wait for port 2000, then run .\.venv\Scripts\python.exe -m da_mvp.web_demo --mode carla"
