# Keeps OpenAlgo (http://127.0.0.1:5001) running. Scheduled task "TradeAnalyse\OpenAlgo".
. (Join-Path $PSScriptRoot 'task_env.ps1')

if (Get-NetTCPConnection -LocalPort 5001 -State Listen -ErrorAction SilentlyContinue) {
    Write-TaskLog 'openalgo' 'already listening on 5001, nothing to do'
    exit 0
}
Write-TaskLog 'openalgo' 'starting OpenAlgo'
Set-Location (Join-Path $Root 'openalgo-main')
$log = Join-Path $Logs ('openalgo-{0:yyyy-MM-dd}.log' -f (Get-Date))
& .\.venv\Scripts\python.exe app.py *>> $log
$code = $LASTEXITCODE
Write-TaskLog 'openalgo' "OpenAlgo exited with code $code"
exit $(if ($code) { $code } else { 1 })
