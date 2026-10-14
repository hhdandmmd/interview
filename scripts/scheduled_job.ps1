# 任务计划程序的等价手工入口，保存结构化输出和实际退出码。
param(
    [Parameter(Mandatory=$true)][string]$Executable,
    [Parameter(Mandatory=$true)][string]$Workspace,
    [ValidateSet("run", "health")][string]$Command = "health"
)
$ErrorActionPreference = "Stop"
$TaskWorkspace = [System.IO.Path]::GetFullPath($Workspace)
$TaskLogs = Join-Path $TaskWorkspace "job_logs"
New-Item -ItemType Directory -Force -Path $TaskLogs | Out-Null
$TaskStamp = Get-Date -Format "yyyyMMdd_HHmmss_ffff"
& $Executable $Command --workspace $TaskWorkspace 2>&1 |
    Out-File -LiteralPath (Join-Path $TaskLogs "$Command-$TaskStamp.json") -Encoding utf8
$TaskExitCode = $LASTEXITCODE
exit $TaskExitCode
