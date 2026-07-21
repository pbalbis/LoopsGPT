#requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$RepoUrl = "https://github.com/pbalbis/LoopsGPT.git",
    [string]$InstallRoot = "C:\QuantOS",
    [string]$PythonVersion = "3.11",
    [string]$Mt5InstallerPath = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Ensure-Winget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is not available. Install Microsoft App Installer and rerun."
    }
}

function Install-WingetPackage([string]$Id) {
    if (-not (winget list --id $Id -e --accept-source-agreements 2>$null | Select-String $Id)) {
        winget install --id $Id -e --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "Failed to install $Id" }
    }
}

Write-Step "Preparing Windows"
Ensure-Winget
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope LocalMachine -Force
powercfg /hibernate off | Out-Null
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change monitor-timeout-ac 0 | Out-Null

Write-Step "Installing base tools"
Install-WingetPackage "Git.Git"
Install-WingetPackage "Python.Python.3.11"
Install-WingetPackage "Microsoft.PowerShell"

$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")

Write-Step "Creating QuantOS directories"
$dirs = @(
    $InstallRoot,
    "$InstallRoot\repo",
    "$InstallRoot\logs",
    "$InstallRoot\secrets",
    "$InstallRoot\runtime"
)
foreach ($dir in $dirs) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

Write-Step "Cloning or updating LoopsGPT"
if (Test-Path "$InstallRoot\repo\.git") {
    git -C "$InstallRoot\repo" fetch origin main
    git -C "$InstallRoot\repo" reset --hard origin/main
} else {
    if (Test-Path "$InstallRoot\repo") { Remove-Item "$InstallRoot\repo" -Recurse -Force }
    git clone --branch main $RepoUrl "$InstallRoot\repo"
}

Write-Step "Creating Python environment"
$python = (Get-Command python.exe -ErrorAction Stop).Source
& $python -m venv "$InstallRoot\venv"
& "$InstallRoot\venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
& "$InstallRoot\venv\Scripts\pip.exe" install -e "$InstallRoot\repo"
& "$InstallRoot\venv\Scripts\pip.exe" install MetaTrader5 psutil

if ($Mt5InstallerPath) {
    Write-Step "Installing MetaTrader 5"
    if (-not (Test-Path $Mt5InstallerPath)) { throw "MT5 installer not found: $Mt5InstallerPath" }
    Start-Process -FilePath $Mt5InstallerPath -ArgumentList "/auto" -Wait
}

Write-Step "Creating environment template"
$envFile = "$InstallRoot\secrets\mt5.env.example"
@"
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=ICMarketsSC-Demo
MT5_TERMINAL_PATH=
MT5_SYMBOL=XAUUSD
MT5_VOLUME=0.01
"@ | Set-Content -Path $envFile -Encoding UTF8

Write-Step "Creating health-check task"
$healthScript = "$InstallRoot\runtime\healthcheck.ps1"
@"
`$ErrorActionPreference = 'Stop'
`$status = [ordered]@{
  recorded_at = (Get-Date).ToUniversalTime().ToString('o')
  computer = `$env:COMPUTERNAME
  python = (& '$InstallRoot\venv\Scripts\python.exe' --version 2>&1 | Out-String).Trim()
  repo_head = (git -C '$InstallRoot\repo' rev-parse HEAD 2>&1 | Out-String).Trim()
  mt5_processes = @((Get-Process terminal64 -ErrorAction SilentlyContinue)).Count
}
`$status | ConvertTo-Json | Set-Content '$InstallRoot\logs\health.json' -Encoding UTF8
"@ | Set-Content -Path $healthScript -Encoding UTF8

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$healthScript`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "QuantOS-Health" -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null

Write-Step "Running verification"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $healthScript
Get-Content "$InstallRoot\logs\health.json"

Write-Host "`nBootstrap complete." -ForegroundColor Green
Write-Host "Next required step: install IC Markets MT5 if not supplied, log in once, and register the GitHub self-hosted runner."