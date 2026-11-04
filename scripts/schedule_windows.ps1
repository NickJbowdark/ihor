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
# Remove them again with:
#     Unregister-ScheduledTask -TaskName "GIM Ingest" -Confirm:$false
#     Unregister-ScheduledTask -TaskName "GIM Backup" -Confirm:$false

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
