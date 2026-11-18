# Keeps the alert scanner running. It sleeps outside market hours and wakes at session.wake (IST) by itself.
# Scheduled task "TradeAnalyse\Scanner".
. (Join-Path $PSScriptRoot 'task_env.ps1')

$running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match '-m scalper ' -and $_.CommandLine -notmatch '--check|--sim|--replay|--history' }
if ($running) {
    Write-TaskLog 'scanner' "already running (pid $($running.ProcessId -join ', ')), nothing to do"
    exit 0
}
Write-TaskLog 'scanner' 'starting scanner'
Set-Location $Root
$log = Join-Path $Logs ('scanner-console-{0:yyyy-MM-dd}.log' -f (Get-Date))
& .\.venv\Scripts\python.exe -m scalper --config config/dhan.yaml --env-file env/dhan.env *>> $log
$code = $LASTEXITCODE
Write-TaskLog 'scanner' "scanner exited with code $code"
exit $(if ($code) { $code } else { 1 })
