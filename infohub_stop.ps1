$ErrorActionPreference = 'Stop'
$root = 'D:\info-hub'
New-Item -ItemType File -Path "$root\data\PAUSED" -Force | Out-Null
$pidFile = "$root\data\run.pid.json"
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output 'No owned PID record. Keepalive paused; use Ctrl+C in the foreground console.'
    exit 0
}
$record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
$entry = "$root\infohub.py"
if ($record.entry -ne $entry) { throw 'PID entry mismatch; refusing stop.' }
$target = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$record.pid)"
if (-not $target) { exit 0 }
$entryOk = $target.CommandLine.Contains($entry) -or ($target.CommandLine -match '(?i)(^|[\s"])infohub\.py([\s"]|$)')
if (-not $record.image -or $target.ExecutablePath -ne $record.image -or -not $entryOk) {
    throw 'PID ownership mismatch; refusing stop.'
}
$created = ([DateTimeOffset]$target.CreationDate).ToUnixTimeSeconds()
if ([Math]::Abs($created - [double]$record.started) -gt 10) { throw 'PID reused; refusing stop.' }
# Prefer Ctrl+C. This explicit stop is a forced last resort; expired claims become unknown.
Stop-Process -Id ([int]$record.pid) -Force
