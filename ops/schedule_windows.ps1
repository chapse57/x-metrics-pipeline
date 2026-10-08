# Register (or remove) the Task Scheduler job that runs ops\run_weekly.cmd.
#
#   powershell -ExecutionPolicy Bypass -File ops\schedule_windows.ps1            # Sunday 22:00 + at logon
#   powershell -ExecutionPolicy Bypass -File ops\schedule_windows.ps1 -At 21:30 -Day Saturday
#   powershell -ExecutionPolicy Bypass -File ops\schedule_windows.ps1 -Remove
#   schtasks /Run /TN "x-metrics weekly"                                          # fire it now, by hand
#
# Two triggers. Sunday 22:00 is the schedule. Five minutes after each logon is the catch-up:
# run_weekly.cmd passes --if-due 6, so a logon start does nothing unless the last successful run
# is 6+ days old — which is exactly the case after a Sunday when the PC was off, or a run that
# was cut off by a shutdown (2026-10-04: started 22:00, PC switched off 22:40, nothing recorded).
# Runs as the current user, only while logged on (the collector needs the browser profile in this
# account). Linux/macOS: the crontab line in ops/README.md does the same.
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
$Weekly   = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At
$Logon    = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Logon.Delay = "PT5M"     # let Docker Desktop start first
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($Weekly, $Logon) -Settings $Settings -Force | Out-Null
Write-Host "registered '$TaskName': every $Day at $At, and 5 minutes after logon (runs only if due) -> $Cmd"
Write-Host "check:  schtasks /Query /TN `"$TaskName`" /V /FO LIST"
