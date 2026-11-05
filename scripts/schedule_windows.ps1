# Registers two Windows Scheduled Tasks for GIM. Run ONCE from the project root in PowerShell:
#     powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
#
#  "GIM Ingest"  every 30 min: python -m gim.collect
#                Each source is only fetched when its own poll interval (config/sources.toml) has
#                passed; failing sources back off automatically. Overlap protection exists twice:
#                Task Scheduler (MultipleInstances IgnoreNew) and GIM's own database lock
#                (which also covers the dashboard's "Run" buttons). Exit code 3 = skipped, lock held.
#  "GIM Backup"  daily 03:17: python -m gim.maintenance backup  (keeps the newest 14)
#
#  "GIM Daily Brief" (only with -DailyBrief): daily 07:43 local time:
#                python -m gim.report --type daily_brief --pdf reports\daily\latest.pdf
#                (uses the AI provider in .env; budget-capped; add --email in the action if SMTP is set)
#
#     powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -DailyBrief
#
# Remove them again with:
#     Unregister-ScheduledTask -TaskName "GIM Ingest" -Confirm:$false
#     Unregister-ScheduledTask -TaskName "GIM Backup" -Confirm:$false
#     Unregister-ScheduledTask -TaskName "GIM Daily Brief" -Confirm:$false
param([switch]$DailyBrief)

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Virtual environment not found at $Python. Follow docs\WINDOWS_SETUP.md first." }

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -MultipleInstances IgnoreNew

$Action  = New-ScheduledTaskAction -Execute $Python -Argument "-m gim.collect" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "GIM Ingest" -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Global Intelligence Monitor: collect due sources" -Force | Out-Null

$BackupAction  = New-ScheduledTaskAction -Execute $Python -Argument "-m gim.maintenance backup" -WorkingDirectory $ProjectRoot
$BackupTrigger = New-ScheduledTaskTrigger -Daily -At 03:17
Register-ScheduledTask -TaskName "GIM Backup" -Action $BackupAction -Trigger $BackupTrigger -Settings $Settings `
    -Description "Global Intelligence Monitor: daily database backup" -Force | Out-Null

Write-Host "Registered 'GIM Ingest' (every 30 min) and 'GIM Backup' (daily 03:17)."

if ($DailyBrief) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "reports\daily") | Out-Null
    $BriefAction  = New-ScheduledTaskAction -Execute $Python -Argument "-m gim.report --type daily_brief --pdf reports\daily\latest.pdf" -WorkingDirectory $ProjectRoot
    $BriefTrigger = New-ScheduledTaskTrigger -Daily -At 07:43
    Register-ScheduledTask -TaskName "GIM Daily Brief" -Action $BriefAction -Trigger $BriefTrigger -Settings $Settings `
        -Description "Global Intelligence Monitor: daily brief (draft)" -Force | Out-Null
    Write-Host "Registered 'GIM Daily Brief' (daily 07:43) -> reports\daily\latest.pdf"
}
