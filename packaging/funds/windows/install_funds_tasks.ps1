<#
.SYNOPSIS
  注册 Raricy 双基金资金服务的 Windows 计划任务（无窗口、独立运行）。

.DESCRIPTION
  只做**注册**：创建一个隐藏任务，不启动任何真实服务、不填写任何凭据。
  - RaricyFundsService：登录时启动资金服务，失败自动重启，窗口隐藏。

  运行中的备份由服务内部每小时执行（backup_interval_seconds=3600），也可在控制台
  触发已认证的 POST /api/backup。这里**不再注册**每日备份任务——另一个进程开备份
  会与运行中的服务争用同一份 SQLite（单写者由 OS 数据锁保证，第二个写者会被拒绝）。

  凭据仍由当前用户环境变量或独立的凭据文件提供，绝不写进任务命令行或本脚本。

  支持 -WhatIf：此时**不生成配置文件、不注销也不注册任何任务、不修改任何现有计划
  任务**，只打印将执行的动作（Python 版本预检是只读的，仍会运行）。

.PARAMETER RepoRoot
  仓库根目录；默认由脚本位置上溯三级推断（例：D:\Study\Code\raricy_capital）。
.PARAMETER DataDir
  资金数据目录；默认 <RepoRoot>\data\capital_funds。
.PARAMETER ConfigPath
  服务配置文件；默认 <RepoRoot>\config.funds.yaml。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\funds\windows\install_funds_tasks.ps1
  powershell -File packaging\funds\windows\install_funds_tasks.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RepoRoot,
    [string]$DataDir,
    [string]$ConfigPath
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
if (-not $DataDir)     { $DataDir = Join-Path $RepoRoot "data\capital_funds" }
if (-not $ConfigPath)  { $ConfigPath = Join-Path $RepoRoot "config.funds.yaml" }

$launcher = Join-Path $RepoRoot "packaging\funds\windows\run_funds_hidden.vbs"
$wscript = Join-Path $env:SystemRoot "System32\wscript.exe"

foreach ($required in @($launcher)) {
    if (-not (Test-Path $required)) { throw "缺少文件：$required" }
}

# 预检 Python >= 3.12：只跑一次只读的版本检查，不安装、不修改任何东西。
# 优先用仓库 .venv 里的解释器；没有就退回 PATH 上的 python。
$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python.exe" }
try {
    & $python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "需要 Python 3.12 或以上：$python（请先建好 .venv 并 pip install .）"
    }
    Write-Host "Python 版本检查通过：$python"
} catch [System.Management.Automation.CommandNotFoundException] {
    Write-Warning "找不到 Python 解释器（$python）。注册后请确认 .venv 或 PATH 中的 Python 为 3.12+。"
}

# 初始配置：不存在时从样例生成，并把 data_dir 指向本次安装的数据目录。
if (-not (Test-Path $ConfigPath)) {
    $example = Join-Path $RepoRoot "packaging\funds\config.example.yaml"
    if (-not (Test-Path $example)) {
        Write-Warning "缺少配置且找不到样例：$ConfigPath"
    } elseif ($PSCmdlet.ShouldProcess($ConfigPath, "生成初始基金配置")) {
        Copy-Item $example $ConfigPath
        $DataDirYaml = $DataDir -replace '\\', '/'
        (Get-Content $ConfigPath) -replace '^data_dir:.*', "data_dir: `"$DataDirYaml`"" |
            Set-Content -Encoding UTF8 $ConfigPath
        Write-Host "已放置初始配置：$ConfigPath（data_dir=$DataDirYaml）"
    }
}

if ($PSCmdlet.ShouldProcess("RaricyFundsService", "注册计划任务（不启动）")) {
    $settings = New-ScheduledTaskSettingsSet `
        -Hidden `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    # 已存在则先移除，保证幂等。旧版本注册过的每日备份任务一并清理。
    foreach ($legacy in @("RaricyFundsService", "RaricyFundsBackup")) {
        Unregister-ScheduledTask -TaskName $legacy -Confirm:$false -ErrorAction SilentlyContinue
    }
    $action = New-ScheduledTaskAction -Execute $wscript `
        -Argument "`"$launcher`" `"$ConfigPath`"" -WorkingDirectory $RepoRoot
    # 服务任务在登录时启动，作为当前用户运行（SYSTEM 无法访问用户的 venv 与凭据）。
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName "RaricyFundsService" -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Highest -Description "Raricy 资金服务（隐藏）" | Out-Null
    Write-Host "已注册：RaricyFundsService"
    Write-Host ""
    Write-Host "未启动任何服务。检查后手工启动：Start-ScheduledTask -TaskName RaricyFundsService"
    Write-Host "凭据请通过当前用户环境变量或独立凭据文件提供，切勿写进任务。"
    Write-Host "备份：服务内部每小时执行；也可在控制台触发已认证的 POST /api/backup。"
    Write-Host "完整步骤与人工验收清单见docs/DEPLOYMENT.md。"
} else {
    Write-Host "（未执行注册：-WhatIf 或已取消。没有生成配置、没有注销或注册任务、也没有启动服务。）"
}
