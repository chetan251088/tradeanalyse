# After the market closes: add today's index/VIX bars and option chains to data\market (resumable).
# Scheduled task "TradeAnalyse\DataUpdate". Needs OpenAlgo logged in to Dhan (it reuses that session).
param([string]$User = 'chetan')
. (Join-Path $PSScriptRoot 'task_env.ps1')

Set-Location $Root
Write-TaskLog 'data' "data update started for OpenAlgo user $User"
$log = Join-Path $Logs ('data-update-{0:yyyy-MM-dd}.log' -f (Get-Date))
& .\openalgo-main\.venv\Scripts\python.exe tools\dhan_download.py --user $User --years 5 --chains all *>> $log
Write-TaskLog 'data' "data update finished with code $LASTEXITCODE"
