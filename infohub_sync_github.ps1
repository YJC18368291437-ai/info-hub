# infohub_sync_github.ps1
# One-click: D:\info-hub -> D:\info-hub-public (sanitized) -> commit & push.
#
# Principles
#   1) robocopy /E  -> incremental, NEVER deletes. Private runtime files are excluded.
#   2) sanitize pass -> replace real QQ/group IDs, personal names and machine paths in
#      every synced text file, using the (gitignored) rule file .sync-secrets.json.
#      Encoding is detected per file (UTF-8 / UTF-8 BOM / GBK) and preserved.
#   3) leak self-check -> forbidden literals + "any 8-13 digit number not whitelisted"
#      + phone-number pattern. Any hit ABORTS (no commit, no push).
#   4) git add/commit/push.
#
# Usage
#   powershell -NoProfile -ExecutionPolicy Bypass -File infohub_sync_github.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File infohub_sync_github.ps1 -DryRun

param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$src = 'D:\info-hub'
$dst = 'D:\info-hub-public'
$secretFile = Join-Path $dst '.sync-secrets.json'

# ---- 0) load rules ----
if (-not (Test-Path -LiteralPath $secretFile)) { throw "missing rule file: $secretFile" }
$cfg = Get-Content -LiteralPath $secretFile -Raw -Encoding UTF8 | ConvertFrom-Json

# ---- 1) copy (incremental, never delete) ----
$xd = @('.venv', 'data', '__pycache__', '.git', 'docs')
$xf = @(
    '.env', '*.log', '*.bak*', '*.zip', 'textfmt.py.bak-20261013',
    'targets.json', 'groups.yaml', 'topic_ids.json', 'group_names.json',
    'newspaper.yaml', '.sync-secrets.json', '.gitignore',
    'infohub_sync_github.ps1', 'HANDOFF*.md', '项目复核档案.md'
)
robocopy $src $dst /E /XD $xd /XF $xf /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE" }
Write-Host "[1/5] copied code + config (robocopy rc=$LASTEXITCODE)"

# curated docs only: real IDs scattered in the rest of docs/ are not published
$docSrc = Join-Path $src 'docs\napcat-setup.md'
if (Test-Path -LiteralPath $docSrc) {
    $docDst = Join-Path $dst 'docs'
    New-Item -ItemType Directory -Path $docDst -Force | Out-Null
    Copy-Item -LiteralPath $docSrc -Destination (Join-Path $docDst 'napcat-setup.md') -Force
}
Write-Host "[1b/5] copied curated docs (docs/napcat-setup.md)"

# prune files that exist in dst but must not be published
#   - no longer exist in src (deleted/renamed upstream)
#   - private handoff / review dumps that are excluded from the copy
$publicOnly = @('.sync-secrets.json', '.gitignore', 'infohub_sync_github.ps1')
$privateNames = @('项目复核档案.md', 'HANDOFF*.md', 'HANDOFF-*.md')
$keepDirs = '\\(\.git|\.venv|data|__pycache__)\\'
$pruned = @()
Get-ChildItem -LiteralPath $dst -Recurse -File -Force |
    Where-Object { $_.FullName -notmatch $keepDirs } |
    ForEach-Object {
        $rel = $_.FullName.Substring($dst.Length + 1)
        if ($publicOnly -contains $rel) { return }
        $isPrivate = $false
        foreach ($pat in $privateNames) { if ($_.Name -like $pat) { $isPrivate = $true } }
        if ($isPrivate -or -not (Test-Path -LiteralPath (Join-Path $src $rel))) {
            Remove-Item -LiteralPath $_.FullName -Force
            $pruned += $rel
        }
    }
if ($pruned.Count -gt 0) {
    Write-Host ("[1c/5] pruned {0} stale file(s)" -f $pruned.Count)
    $pruned | ForEach-Object { Write-Host "      - $_" }
}

# ---- 2) sanitize ----
$exts = @('.py', '.md', '.json', '.yaml', '.yml', '.bat', '.vbs', '.ps1', '.txt', '.example')
$utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$utf8Bom = New-Object System.Text.UTF8Encoding($true)
$gbk = [System.Text.Encoding]::GetEncoding(936)
$changed = @()

Get-ChildItem -LiteralPath $dst -Recurse -File -Force |
    Where-Object {
        $_.FullName -notmatch '\\\.git\\' -and
        $_.Name -ne '.sync-secrets.json' -and
        (($exts -contains $_.Extension) -or $_.Name -eq '.gitignore')
    } |
    ForEach-Object {
        $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
        $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
        $enc = $utf8NoBom
        try {
            $t = $utf8Strict.GetString($bytes)
        } catch {
            $t = $gbk.GetString($bytes)
            $enc = $gbk
        }
        if ($hasBom) { $enc = $utf8Bom }
        $o = $t
        foreach ($p in $cfg.replace.PSObject.Properties) { $t = $t.Replace($p.Name, $p.Value) }
        if ($t -ne $o) {
            [System.IO.File]::WriteAllText($_.FullName, $t, $enc)
            $changed += $_.FullName.Replace($dst, '')
        }
    }
Write-Host ("[2/5] sanitized ({0} file(s) changed)" -f $changed.Count)
$changed | ForEach-Object { Write-Host "      ~ $_" }

# ---- 3) leak self-check ----
$allowed = @($cfg.allowed_numbers)
$hits = @()
Get-ChildItem -LiteralPath $dst -Recurse -File -Force |
    Where-Object { $_.FullName -notmatch '\\\.git\\' -and $_.Name -ne '.sync-secrets.json' } |
    ForEach-Object {
        $f = $_.FullName
        $rel = $f.Replace($dst, '')
        $bytes = [System.IO.File]::ReadAllBytes($f)
        try { $text = $utf8Strict.GetString($bytes) } catch { $text = $gbk.GetString($bytes) }
        $lines = $text -split "`r?`n"

        foreach ($tok in $cfg.forbidden) {
            for ($i = 0; $i -lt $lines.Count; $i++) {
                if ($lines[$i].Contains($tok)) {
                    $hits += ("forbidden '{0}'  {1}:{2}" -f $tok, $rel, ($i + 1))
                }
            }
        }
        for ($i = 0; $i -lt $lines.Count; $i++) {
            foreach ($m in [regex]::Matches($lines[$i], '(?<![\w.])\d{8,13}(?![\w.])')) {
                $n = $m.Value
                if ($allowed -contains $n) { continue }
                if ($n -match '^2026\d{4}$') { continue }
                if ($n -match '^123456\d{3}$') { continue }
                if ($n -match '^100000000\d?$') { continue }
                if ($n -match '^1000000000$') { continue }
                if ($n -match '^100000000000\d?$') { continue }
                $hits += ("number '{0}'  {1}:{2}" -f $n, $rel, ($i + 1))
            }
            if ($lines[$i] -match '(?<!\d)1[3-9]\d{9}(?!\d)') {
                $hits += ("phone-like '{0}'  {1}:{2}" -f $Matches[0], $rel, ($i + 1))
            }
        }
    }
$hits = @($hits | Sort-Object -Unique)
if ($hits.Count -gt 0) {
    Write-Host "[3/5] LEAK CHECK FAILED -> aborting (no commit, no push):"
    $hits | ForEach-Object { Write-Host "      $_" }
    exit 1
}
Write-Host "[3/5] leak check clean"

# ---- 4) retire files that must not stay in the published repo ----
Push-Location $dst
try {
    $retired = @('HANDOFF.md', 'HANDOFF-20261019-1134.md')
    foreach ($r in $retired) {
        if (Test-Path -LiteralPath $r) { Remove-Item -LiteralPath $r -Force }
        git rm --cached --ignore-unmatch --quiet -- $r 2>$null
    }
    if ($DryRun) {
        Write-Host "[4/5] -DryRun: pending changes:"
        git status --short
        return
    }

    # ---- 5) commit & push ----
    git add -A
    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) { Write-Host "[5/5] no changes to commit"; return }
    git commit -m ("sync: " + (Get-Date -Format 'yyyy-MM-dd HH:mm')) | Out-Null
    git push
    Write-Host "[5/5] committed & pushed"
} finally { Pop-Location }
