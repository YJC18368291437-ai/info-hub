﻿param([switch]$RestartNow)

$ErrorActionPreference = 'SilentlyContinue'
$qq        = '1000000001'
$api       = 'http://127.0.0.1:8083'
$tokenPath = 'D:\info-hub\data\napcat_http_token.txt'
$qrDir     = 'D:\napcat-toolkit\napcat\cache'
$uiScript  = 'D:\info-hub\qr_login_ui.py'
$pythonw   = 'D:\python\pythonw.exe'
$autostart = 'D:\napcat-toolkit\napcat_autostart.ps1'
$logPath   = 'D:\info-hub\data\login_guard.log'
$snooze    = 'D:\info-hub\data\qr_ui_snooze'
$uiMarker  = 'qr_login_ui.py'
$paused    = 'D:\info-hub\data\PAUSED'
$botEntry  = 'D:\info-hub\infohub.py'
$pidFile   = 'D:\info-hub\data\run.pid.json'
$heartbeat = 'D:\info-hub\data\heartbeat.json'
$alertStatePath = 'D:\info-hub\data\alert_state.json'
$alertRequest   = 'D:\info-hub\data\alert_request.json'
$alertLog       = 'D:\info-hub\data\alert_mail.log'
$alertScript    = 'D:\info-hub\tools\alert_mail.py'
$pythonExe      = 'D:\info-hub\.venv\Scripts\python.exe'
$alertRepeatSeconds = 6 * 3600
$alertRetrySeconds  = 300

function Write-GuardLog([string]$msg) {
    try {
        if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 1MB)) {
            Move-Item -Force $logPath ($logPath + '.old')
        }
        Add-Content -Path $logPath -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    } catch {}
}

function Test-Paused {
    return Test-Path -LiteralPath $paused
}

function Get-BotProcess {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object {
            $_.CommandLine -and ($_.CommandLine.Contains($botEntry) -or
                ($_.CommandLine -match '(?i)infohub\.py' -and $_.CommandLine.Contains('D:\info-hub')))
        })
    if ($procs.Count -gt 0) { return $procs }
    # Fallback when CIM query is blocked: validate the recorded PID + image + start time.
    try {
        $rec = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        $p = Get-Process -Id ([int]$rec.pid) -ErrorAction Stop
        $created = ([DateTimeOffset]$p.StartTime).ToUnixTimeSeconds()
        if ($p.Path -eq $rec.image -and [Math]::Abs($created - [double]$rec.started) -le 10) {
            return @([pscustomobject]@{ ProcessId = $p.Id })
        }
    } catch {}
    return @()
}

function Get-Heartbeat {
    try {
        $h = Get-Content -LiteralPath $heartbeat -Raw | ConvertFrom-Json
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int64]$h.updated
        return [pscustomobject]@{
            age = $age
            ok = [bool]$h.ok
            connected = $h.connected
            pid = $h.pid
        }
    } catch {
        return $null
    }
}

function Read-AlertState {
    try {
        $s = Get-Content -LiteralPath $alertStatePath -Raw | ConvertFrom-Json
        return [pscustomobject]@{
            alerted = [bool]$s.alerted
            outage_started = [int64]$s.outage_started
            last_alert = [int64]$s.last_alert
            last_problem = [string]$s.last_problem
        }
    } catch {
        return [pscustomobject]@{
            alerted = $false
            outage_started = [int64]0
            last_alert = [int64]0
            last_problem = ''
        }
    }
}

function Save-AlertState {
    try {
        $json = $script:alertState | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($alertStatePath, $json,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch {}
}

function Send-AlertMail([string]$subject, [string]$body) {
    try {
        $payload = @{ subject = $subject; body = $body } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($alertRequest, $payload,
            (New-Object System.Text.UTF8Encoding($false)))
        $output = & $pythonExe $alertScript --request $alertRequest 2>&1
        $code = $LASTEXITCODE
        try {
            if ((Test-Path -LiteralPath $alertLog) -and ((Get-Item -LiteralPath $alertLog).Length -gt 1MB)) {
                Move-Item -Force -LiteralPath $alertLog -Destination ($alertLog + '.old')
            }
            $line = ('{0} exit={1} {2} {3}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),
                $code, $subject, (($output | Out-String).Trim()))
            Add-Content -LiteralPath $alertLog -Value $line -Encoding UTF8
        } catch {}
        if ($code -eq 0) {
            Remove-Item -LiteralPath $alertRequest -Force -ErrorAction SilentlyContinue
            Write-GuardLog ('alert mail sent: ' + $subject)
            return $true
        }
        Write-GuardLog ('alert mail failed: exit=' + $code)
    } catch {
        Write-GuardLog ('alert mail failed: ' + $_.Exception.Message)
    }
    return $false
}

function Invoke-AlertCheck([bool]$Online) {
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

    if (Test-Paused) {
        $script:problemSince.Clear()
        if ($script:alertState.alerted) {
            $script:alertState.alerted = $false
            $script:alertState.outage_started = 0
            $script:alertState.last_problem = ''
            Save-AlertState
        }
        return
    }

    $bot = Get-BotProcess
    $hb = Get-Heartbeat
    $problems = New-Object System.Collections.Generic.List[string]

    if ($bot.Count -eq 0) {
        $problems.Add('bot 进程不存在')
    }
    if (-not $Online) {
        $problems.Add('QQ 离线或 NapCat 未登录')
    }
    if ($bot.Count -gt 0) {
        if (-not $hb) {
            $problems.Add('采集心跳缺失')
        } elseif ($hb.age -gt 300) {
            $problems.Add('采集心跳超时')
        } elseif (-not $hb.ok) {
            $problems.Add('采集流水线停止')
        } elseif ($hb.last_ok -and (($now - [int64]$hb.last_ok) -gt 300)) {
            $problems.Add('采集流水线连续失败')
        } elseif ($Online -and ($hb.connected -eq $false)) {
            $problems.Add('OneBot 未连接')
        }
    }

    $alive = @{}
    foreach ($p in $problems) {
        $alive[$p] = $true
        if (-not $script:problemSince.ContainsKey($p)) {
            $script:problemSince[$p] = $now
        }
    }
    foreach ($k in @($script:problemSince.Keys)) {
        if (-not $alive.ContainsKey($k)) {
            $script:problemSince.Remove($k)
        }
    }

    if ($problems.Count -eq 0) {
        if ($script:alertState.alerted) {
            if (($now - $script:lastAlertAttempt) -lt $alertRetrySeconds) { return }
            $outage = if ($script:alertState.outage_started -gt 0) {
                [int][math]::Round(($now - $script:alertState.outage_started) / 60)
            } else { 0 }
            $body = @(
                'info-hub 已恢复正常。'
                ''
                ('恢复时间：' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
                ('异常持续：约 {0} 分钟' -f $outage)
                ('上次问题：' + $script:alertState.last_problem)
                ''
                'QQ 在线、bot 进程存活、采集心跳正常。'
            ) -join "`r`n"
            $script:lastAlertAttempt = $now
            if (Send-AlertMail '[info-hub] 已恢复：bot 可正常使用' $body) {
                $script:alertState.alerted = $false
                $script:alertState.outage_started = 0
                $script:alertState.last_problem = ''
                Save-AlertState
            }
        }
        return
    }

    $thresholds = @{
        'bot 进程不存在' = 360
        'QQ 离线或 NapCat 未登录' = 180
        '采集心跳缺失' = 300
        '采集心跳超时' = 300
        '采集流水线停止' = 300
        '采集流水线连续失败' = 300
        'OneBot 未连接' = 300
    }
    $sustained = $false
    foreach ($p in @($script:problemSince.Keys)) {
        $need = if ($thresholds.ContainsKey($p)) { $thresholds[$p] } else { 300 }
        if (($now - [int64]$script:problemSince[$p]) -ge $need) {
            $sustained = $true
        }
    }
    if (-not $sustained) { return }

    if ($script:alertState.alerted) {
        if (($now - $script:alertState.last_alert) -lt $alertRepeatSeconds) { return }
    } elseif (($now - $script:lastAlertAttempt) -lt $alertRetrySeconds) {
        return
    }

    $botPid = if ($bot.Count -gt 0) {
        (@($bot | ForEach-Object { $_.ProcessId }) -join ',')
    } else { '无' }
    $hbText = if ($hb) {
        $t = '{0} 秒前（ok={1}, connected={2}）' -f [int]$hb.age, $hb.ok, $hb.connected
        if ($hb.last_ok) {
            $t += ('，最近成功 {0} 秒前' -f [int]($now - [int64]$hb.last_ok))
        }
        $t
    } else { '文件缺失' }
    $body = @(
        'info-hub 检测到异常，bot 可能无法正常使用。'
        ''
        ('时间：' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
        ('主机：' + $env:COMPUTERNAME)
        ('问题：' + ($problems -join '；'))
        ('bot PID：' + $botPid)
        ('采集心跳：' + $hbText)
        ('QQ/NapCat 在线：' + $(if ($Online) { '是' } else { '否' }))
        ''
        '登录守护会继续尝试恢复；若长时间离线请手动扫码。'
    ) -join "`r`n"
    $subject = '[info-hub] 异常：' + $problems[0]

    $script:lastAlertAttempt = $now
    if (Send-AlertMail $subject $body) {
        if (-not $script:alertState.alerted) {
            $starts = @($script:problemSince.Values | ForEach-Object { [int64]$_ })
            $script:alertState.outage_started = if ($starts.Count -gt 0) {
                ($starts | Measure-Object -Minimum).Minimum
            } else { $now }
        }
        $script:alertState.alerted = $true
        $script:alertState.last_alert = $now
        $script:alertState.last_problem = ($problems -join '；')
        Save-AlertState
    }
}

function Get-Token {
    try { return (Get-Content -LiteralPath $tokenPath -Raw).Trim() } catch { return '' }
}

function Test-Online {
    $token = Get-Token
    if (-not $token) { return $false }
    try {
        $r = Invoke-RestMethod -Uri ($api + '/get_login_info') -TimeoutSec 5 `
            -Headers @{ Authorization = "Bearer $token" }
        if ($r.retcode -ne 0) { return $false }
        return ([string]$r.data.user_id -eq $qq)
    } catch {
        return $false
    }
}

function Get-NapCat {
    return @(Get-CimInstance Win32_Process -Filter "Name='NapCatWinBootMain.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $qq })
}

function Get-Ui {
    return @(Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match '^python' -and
            $_.CommandLine -and
            $_.CommandLine -match [regex]::Escape($uiMarker)
        })
}

function Stop-Ui {
    Get-Ui | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

function Start-Ui {
    if ((Get-Ui).Count -gt 0) { return }
    Write-GuardLog 'starting QR UI'
    Start-Process -FilePath $pythonw -ArgumentList ('"{0}"' -f $uiScript)
}

function Start-NapCat {
    Write-GuardLog 'starting NapCat'
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $autostart `
        -WindowStyle Hidden
}

function Restart-NapCat {
    Write-GuardLog 'restarting NapCat for a fresh QR'
    Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq 'NapCatWinBootMain.exe' -and $_.CommandLine -and $_.CommandLine -match $qq) -or
        ($_.Name -eq 'QQ.exe' -and $_.CommandLine -and $_.CommandLine -match $qq)
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 2
    Start-NapCat
}

if ($RestartNow) {
    Restart-NapCat
    exit 0
}

$mutex = New-Object System.Threading.Mutex($false, 'Global\infohub-login-guard')
if (-not $mutex.WaitOne(0)) { exit 0 }

$offlineCount = 0
$lastRestart = [datetime]::MinValue
$lastState = ''
$restartCount = 0
$script:alertState = Read-AlertState
$script:problemSince = @{}
$script:lastAlertAttempt = [int64]0

while ($true) {
    $nap = Get-NapCat
    if ($nap.Count -eq 0) {
        Start-NapCat
        Start-Sleep -Seconds 20
    }

    $online = Test-Online
    if ($online) {
        if ($lastState -ne 'online') {
            Write-GuardLog 'account online'
            $lastState = 'online'
        }
        Remove-Item -LiteralPath $snooze -Force -ErrorAction SilentlyContinue
        Stop-Ui
        $offlineCount = 0
        $restartCount = 0
        Invoke-AlertCheck $true
        Start-Sleep -Seconds 60
        continue
    }

    $offlineCount++
    if ($lastState -ne 'offline') {
        Write-GuardLog 'account offline'
        $lastState = 'offline'
    }

    $qr = Get-ChildItem -LiteralPath $qrDir -Filter 'qrcode*.png' -File |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $qrAge = if ($qr) { ((Get-Date) - $qr.LastWriteTime).TotalSeconds } else { 99999 }

    $canRestart = $offlineCount -ge 2 -and
        $qrAge -gt 240 -and
        $restartCount -lt 3 -and
        ((Get-Date) - $lastRestart).TotalMinutes -ge 10
    if ($canRestart) {
        Restart-NapCat
        $lastRestart = Get-Date
        $restartCount++
        Invoke-AlertCheck $false
        Start-Sleep -Seconds 20
        continue
    }

    $snoozed = $false
    if (Test-Path -LiteralPath $snooze) {
        try {
            $until = [int64](Get-Content -LiteralPath $snooze -Raw).Trim()
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            $snoozed = $now -lt ($until + 600)
        } catch { $snoozed = $false }
    }
    if (-not $snoozed) { Start-Ui }

    Invoke-AlertCheck $false
    Start-Sleep -Seconds 60
}
