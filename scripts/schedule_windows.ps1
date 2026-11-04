# Registers a Windows Scheduled Task that runs GIM collection every 3 hours.
# Run ONCE from the project root in PowerShell:
#     powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
# Remove it again with:
#     Unregister-ScheduledTask -TaskName "GIM Ingest" -Confirm:$false

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Virtual environment not found at $Python. Follow docs\WINDOWS_SETUP.md first." }

$Action  = New-ScheduledTaskAction -Execute $Python -Argument "-m gim.ingest --timespan 6h" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 3)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "GIM Ingest" -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Global Intelligence Monitor: collect GDELT metadata" -Force
Write-Host "Scheduled task 'GIM Ingest' registered (every 3 hours, 6h look-back window -> overlap is deduplicated)."
