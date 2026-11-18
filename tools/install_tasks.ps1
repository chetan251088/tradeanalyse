# Registers the TradeAnalyse scheduled tasks for the current Windows user (no admin rights needed).
#   pwsh -File tools\install_tasks.ps1            install or update
#   pwsh -File tools\install_tasks.ps1 -Remove    delete them again
# OpenAlgo and the scanner are checked every 15 minutes and started if they are not running;
# the option-data top-up runs Monday-Friday after the Indian close.
param([switch]$Remove, [string]$User = 'chetan', [string]$DataUpdateAt = '12:30')
$ErrorActionPreference = 'Stop'
$names = 'TradeAnalyse-OpenAlgo', 'TradeAnalyse-Scanner', 'TradeAnalyse-DataUpdate'

if ($Remove) {
    foreach ($n in $names) { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host 'Removed the TradeAnalyse tasks.'
    return
}

$shell = Get-Command pwsh -ErrorAction SilentlyContinue
if (-not $shell) { $shell = Get-Command powershell }
$root = Split-Path -Parent $PSScriptRoot

function New-Action([string]$Script, [string]$Extra = '') {
    $file = Join-Path $PSScriptRoot $Script
    New-ScheduledTaskAction -Execute $shell.Source -WorkingDirectory $root `
        -Argument "-NoLogo -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$file`" $Extra"
}

# Runs in your logged-on session (a locked screen is fine), so it uses your proxy settings.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$keepAlive = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$every15 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $names[0] -Action (New-Action 'run_openalgo.ps1') -Trigger $every15 `
    -Settings $keepAlive -Principal $principal -Force `
    -Description 'Keeps OpenAlgo running on http://127.0.0.1:5001 (log: data\logs\openalgo-*.log)' | Out-Null

Register-ScheduledTask -TaskName $names[1] -Action (New-Action 'run_scanner.ps1') -Trigger $every15 `
    -Settings $keepAlive -Principal $principal -Force `
    -Description 'Keeps the expiry-day alert scanner running (log: data\logs\scalper-*.log)' | Out-Null

$weekdays = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $DataUpdateAt
$once = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName $names[2] -Action (New-Action 'run_data_update.ps1' "-User $User") -Trigger $weekdays `
    -Settings $once -Principal $principal -Force `
    -Description 'Adds the day''s index/VIX/option data after the Indian close (log: data\logs\data-update-*.log)' | Out-Null

Get-ScheduledTask -TaskName 'TradeAnalyse-*' | Select-Object TaskName, State | Format-Table -AutoSize
