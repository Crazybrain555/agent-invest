[CmdletBinding()]
param(
    [string]$UpgradeScript = "C:\ProgramData\agent-invest\staging\upgrade_mineru_tunnel_gpu_metrics.ps1",
    [string]$LogTarget = "C:\ProgramData\agent-invest\staging\upgrade_mineru_tunnel_gpu_metrics.task.log"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$encoding = New-Object System.Text.UTF8Encoding($false)
try {
    $output = (& $UpgradeScript *>&1 | Out-String)
    [IO.File]::WriteAllText($LogTarget, $output, $encoding)
    exit 0
}
catch {
    $message = ($_ | Format-List * -Force | Out-String)
    [IO.File]::WriteAllText($LogTarget, $message, $encoding)
    exit 1
}
