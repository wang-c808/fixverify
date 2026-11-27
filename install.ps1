﻿# fixverify 技能安装脚本
# 用法：powershell -File install.ps1 [-ToolDir <fixverify目录>]
# 作用：把 skills/fix-verify/SKILL.md 安装到各 agent 工具的技能目录，
#       并把工具实际路径写进去（__FIXVERIFY_DIR__ 占位符）。
# 迁移到新电脑：git clone 本仓库 → 跑本脚本一次 → 重启编辑器。

param(
    [string]$ToolDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"
$ToolDir = (Resolve-Path $ToolDir).Path
$skillSrc = Join-Path $ToolDir "skills\fix-verify\SKILL.md"

if (-not (Test-Path $skillSrc)) {
    throw "找不到 $skillSrc"
}

# 读取技能正文并替换占位符
$content = Get-Content $skillSrc -Raw -Encoding UTF8
$content = $content.Replace("__FIXVERIFY_DIR__", $ToolDir)
$installed = @()

function Install-Skill($destRoot, $label) {
    $destDir = Join-Path $destRoot "fix-verify"
    $dest = Join-Path $destDir "SKILL.md"
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    [System.IO.File]::WriteAllText($destDir + "\SKILL.md", $content, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "[install] $label -> $destDir\SKILL.md"
    $script:installed += $destDir
}

# ① ~/.agents/skills：跨工具标准位置（opencode 自动扫描加载；其他遵循 agentskills 约定的工具同样读取）
Install-Skill (Join-Path $HOME ".agents\skills") "agents 标准目录"

# ② ~/.claude/skills：Claude Code
Install-Skill (Join-Path $HOME ".claude\skills") "Claude Code"

# ③ ~/.codex/skills：OpenAI Codex（$CODEX_HOME/skills）
Install-Skill (Join-Path $HOME ".codex\skills") "Codex"

# ③ Cursor：生成项目级规则模板（Cursor 规则按项目放在 .cursor/rules/，无法全局安装）
$body = $content -replace '(?s)^---.*?---\s*', ''
$mdc = "---`r`ndescription: 修复验证闭环：修 bug 后、声明完成前，必须用 fixverify.py 复验（证伪协议），直到收敛。处理 bug 修复、验证修复、衍生 bug 时使用。`r`nglobs:`r`nalwaysApply: false`r`n---`r`n`r`n" + $body
$mdcPath = Join-Path $ToolDir "integrations\fix-verify.mdc"
New-Item -ItemType Directory -Force -Path (Split-Path $mdcPath) | Out-Null
[System.IO.File]::WriteAllText($mdcPath, $mdc, (New-Object System.Text.UTF8Encoding $false))
Write-Host "[install] Cursor 规则模板 -> $mdcPath"
Write-Host "          使用时复制到目标项目的 .cursor\rules\fix-verify.mdc 即可"

Write-Host ""
Write-Host "完成。工具目录: $ToolDir"
Write-Host "opencode 重启后生效（技能随新会话加载）。"
Write-Host "验证：在任意会话里说『跑 fixverify 自测』，或手动运行："
Write-Host ('  python "' + $ToolDir + '\fixverify.py" selftest')
