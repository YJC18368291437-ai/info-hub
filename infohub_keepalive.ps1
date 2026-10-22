# Own info-hub process only. Never inspect QQ identity through a TCP port.
$ErrorActionPreference = 'Stop'
$root = 'D:\info-hub'
if (Test-Path -LiteralPath (Join-Path $root 'data\PAUSED')) { exit 0 }
try {
    $entry = Join-Path $root 'infohub.py'
    $owned = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and ($_.CommandLine.Contains($entry) -or
            ($_.CommandLine -match '(?i)infohub\.py' -and $_.CommandLine.Contains($root))) }
    if ($owned) { exit 0 }
    Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList ('"{0}"' -f $entry) `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput "$root\data\service-out.log" -RedirectStandardError "$root\data\service-error.log"
} catch {
    Write-Error 'info-hub keepalive: process inspection/start failed; inspect manually.'
    exit 2
}
