<#
.SYNOPSIS
  移除 Raricy 资金服务的 Windows 计划任务（不动任何数据或凭据）。
.DESCRIPTION
  默认移除 RaricyFundsService；RaricyFundsBackup 只是为清理旧版本注册过的每日备份
  任务而顺带处理，当前版本不会注册它。支持 -WhatIf：只打印将移除的任务，不实际改动。
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"

foreach ($name in @("RaricyFundsService", "RaricyFundsBackup")) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($name, "移除计划任务")) {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "已移除：$name"
        }
    } else {
        Write-Host "不存在，跳过：$name"
    }
}

Write-Host "数据目录、日志与凭据文件保持原样，请自行决定是否清除。"
