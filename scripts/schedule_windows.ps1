# Registers a Windows Scheduled Task that runs the GIM collector every 30 minutes.
# Each source is only fetched when its own poll interval (config/sources.toml) has passed.
# Run ONCE from the project root in PowerShell:
#     powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
# Remove it again with:
#     Unregister-ScheduledTask -TaskName "GIM Ingest" -Confirm:$false

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Virtual environment not found at $Python. Follow docs\WINDOWS_SETUP.md first." }

$Action  = New-ScheduledTaskAction -Execute $Python -Argument "-m gim.collect" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "GIM Ingest" -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Global Intelligence Monitor: collect GDELT metadata" -Force
Write-Host "Scheduled task 'GIM Ingest' registered (every 30 min; each source respects its own poll interval)."
