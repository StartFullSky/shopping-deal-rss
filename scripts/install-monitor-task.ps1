[CmdletBinding()]
param(
    [ValidateRange(5, 60)]
    [int]$IntervalMinutes = 5,
    [string]$TaskName = "Shopping Deal RSS Monitor",
    [string]$ConfigPath = "",
    [switch]$Uninstall,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Uninstall) {
    if ($DryRun) {
        [pscustomobject]@{
            status = "would_unregister"
            taskName = $TaskName
        } | ConvertTo-Json
        return
    }
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        [pscustomobject]@{
            status = "not_found"
            taskName = $TaskName
        } | ConvertTo-Json
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    [pscustomobject]@{
        status = "unregistered"
        taskName = $TaskName
    } | ConvertTo-Json
    return
}

$scriptDirectory = $PSScriptRoot
$repositoryRoot = Split-Path -Parent $scriptDirectory
$monitorScript = Join-Path $scriptDirectory "monitor.ps1"
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $repositoryRoot "monitor-config.json"
}
$resolvedConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$resolvedMonitorScript = (Resolve-Path -LiteralPath $monitorScript).Path
$windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$shellPath = if (Test-Path -LiteralPath $windowsPowerShell) {
    $windowsPowerShell
}
elseif ($PSVersionTable.PSEdition -eq "Core") {
    (Get-Process -Id $PID).Path
}
else {
    (Get-Process -Id $PID).Path
}
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$resolvedMonitorScript`" -ConfigPath `"$resolvedConfigPath`""
$action = New-ScheduledTaskAction -Execute $shellPath -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

if ($DryRun) {
    [pscustomobject]@{
        status = "would_register"
        taskName = $TaskName
        intervalMinutes = $IntervalMinutes
        executable = $shellPath
        arguments = $arguments
    } | ConvertTo-Json
    return
}

$task = Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Poll configured shopping RSS feeds and send only qualified new or materially changed leads to Bark." `
    -Force

[pscustomobject]@{
    status = "registered"
    taskName = $task.TaskName
    intervalMinutes = $IntervalMinutes
    state = [string]$task.State
    configPath = $resolvedConfigPath
} | ConvertTo-Json
