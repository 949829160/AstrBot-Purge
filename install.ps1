#Requires -Version 5.1
<#
.SYNOPSIS
    安装 AstrBot-Purge：插件 + 引擎 + 注入帧，然后打补丁。
.PARAMETER Root
    AstrBot 安装目录（含 backend\app 的那一层）。省略时自动探测。
.PARAMETER NoApply
    只复制文件，不执行 repatch.py apply。
.PARAMETER Frame
    帧预设：market（默认，三条 SCOPE）或 soft（只保留工程排障与提示词注入分析）。
#>
param(
    [string]$Root = "",
    [switch]$NoApply,
    [ValidateSet("market", "soft")]
    [string]$Frame = "market"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Resolve-Root {
    param([string]$Hint)
    $candidates = @()
    if ($Hint) { $candidates += $Hint }
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA "AstrBot"),
        (Join-Path $env:ProgramFiles "AstrBot"),
        (Join-Path $env:USERPROFILE "AstrBot")
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c "backend\app\astrbot"))) { return (Resolve-Path $c).Path }
    }
    throw "找不到 AstrBot 安装目录（需要 <root>\backend\app\astrbot）。请用 -Root 指定。"
}

function Backup-File {
    param([string]$Path)
    if (Test-Path $Path) {
        Copy-Item $Path "$Path.bak-$stamp" -Force
    }
}

$root = Resolve-Root $Root
$py = Join-Path $root "backend\python\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$engineDir = Join-Path $root "astrbot-purge"
$pluginSrc = $here
$pluginDst = Join-Path $root "backend\data\plugins\astrbot_purge"

Write-Host "AstrBot: $root"
Write-Host "Python : $py"

New-Item -ItemType Directory -Force -Path $engineDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $pluginDst) | Out-Null

# 1. 引擎 + 帧
foreach ($name in @("repatch.py")) {
    $src = Join-Path $here "engine\$name"
    $dst = Join-Path $engineDir $name
    Backup-File $dst
    Copy-Item $src $dst -Force
}
foreach ($name in @("core.md")) {
    $src = Join-Path $here "frame\$name"
    $dst = Join-Path $engineDir $name
    Backup-File $dst
    Copy-Item $src $dst -Force
}
$frameSrc = if ($Frame -eq "soft") { Join-Path $here "frame\prompt-inject.soft.md" } else { Join-Path $here "frame\prompt-inject.md" }
$frameDst = Join-Path $engineDir "prompt-inject.md"
Backup-File $frameDst
Copy-Item $frameSrc $frameDst -Force
Write-Host "frame  : $Frame"
$flag = Join-Path $engineDir "enabled.txt"
if (-not (Test-Path $flag)) { Set-Content -Path $flag -Value "on" -Encoding ASCII -NoNewline }

# 1b. 随包快照：首装把 22 个核心文件整份恢复，避免锚点链缺口
$snapSrc = Join-Path $here "snapshot"
if (Test-Path $snapSrc) {
    $snapDst = Join-Path $engineDir "tree"
    New-Item -ItemType Directory -Force -Path $snapDst | Out-Null
    Copy-Item (Join-Path $snapSrc "files") $snapDst -Recurse -Force
    Copy-Item (Join-Path $snapSrc "manifest.json") $snapDst -Force
    Write-Host "snapshot: 22 files -> $snapDst"
}

# 2. 插件
if (Test-Path $pluginDst) {
    Get-ChildItem $pluginDst -File | ForEach-Object { Backup-File $_.FullName }
}
New-Item -ItemType Directory -Force -Path $pluginDst | Out-Null
foreach ($name in @("main.py", "metadata.yaml", "_conf_schema.json", "logo.png")) {
    $src = Join-Path $pluginSrc $name
    if (Test-Path $src) { Copy-Item $src (Join-Path $pluginDst $name) -Force }
}

# 3. 打补丁
if (-not $NoApply) {
    Write-Host "`n==> repatch.py apply"
    & $py (Join-Path $engineDir "repatch.py") apply
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "apply 返回 $LASTEXITCODE；运行 status 查看缺失项。"
    }
    Write-Host "`n==> repatch.py status"
    & $py (Join-Path $engineDir "repatch.py") status
}

Write-Host "`n安装完成。重启 AstrBot 后生效。"
Write-Host "聊天里发送 /purge status 查看面板。"
