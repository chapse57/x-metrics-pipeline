# Register (or remove) the weekly Task Scheduler job that runs ops\run_weekly.cmd.
#
#   powershell -ExecutionPolicy Bypass -File ops\schedule_windows.ps1            # every Sunday 22:00
#   powershell -ExecutionPolicy Bypass -File ops\schedule_windows.ps1 -At 21:30 -Day Saturday
#   powershell -ExecutionPolicy Bypass -File ops\schedule_windows.ps1 -Remove
#   schtasks /Run /TN "x-metrics weekly"                                          # fire it now, by hand
#
# The task runs as the current user, only when that user is logged on (the collector needs the
# browser profile in this account), and starts as soon as possible if the machine was asleep at
# the scheduled minute. Linux/macOS: the crontab line in ops/README.md does the same.
param(
    [string]$At = "22:00",
    [string]$Day = "Sunday",
    [switch]$Remove
)
$TaskName = "x-metrics weekly"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Cmd = Join-Path $Root "ops\run_weekly.cmd"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "removed task '$TaskName'"
    exit 0
}
if (-not (Test-Path (Join-Path $Root "ops\env.cmd"))) {
    Write-Host "ops\env.cmd missing - copy ops\env.example.cmd and fill it in first"; exit 2
}
$Action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Cmd`"" -WorkingDirectory $Root
$Trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Host "registered '$TaskName': every $Day at $At -> $Cmd"
Write-Host "check:  schtasks /Query /TN `"$TaskName`" /V /FO LIST"
