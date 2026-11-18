# Shared environment for the scheduled tasks (dot-sourced by the other scripts).
$Root = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Root 'data\logs'
New-Item -ItemType Directory -Force $Logs | Out-Null

# Local OpenAlgo calls must never go through the corporate proxy.
$noProxy = @('127.0.0.1', 'localhost') + (($env:NO_PROXY -split ',') | Where-Object { $_ })
$env:NO_PROXY = ($noProxy | Select-Object -Unique) -join ','
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

function Write-TaskLog([string]$Name, [string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    Add-Content -Path (Join-Path $Logs "$Name-tasks.log") -Value $line
}

if (-not $env:HTTPS_PROXY) {
    Write-TaskLog 'env' 'HTTPS_PROXY is not set; on the office network Dhan is unreachable without it'
}
