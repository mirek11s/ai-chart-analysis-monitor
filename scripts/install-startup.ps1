param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = 'JarvisChartAgent'
)
$ErrorActionPreference = 'Stop'
$jarvisRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$jarvisPython = Join-Path $jarvisRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $jarvisPython)) {
    throw 'Create the project virtual environment and install Jarvis first.'
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task already exists: $TaskName. Inspect or remove it before installing."
}
$jarvisRunner = Join-Path $jarvisRoot 'scripts\run-worker.ps1'
$jarvisArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $jarvisRunner + '"'
$jarvisAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $jarvisArguments -WorkingDirectory $jarvisRoot
$jarvisUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$jarvisTrigger = New-ScheduledTaskTrigger -AtLogOn -User $jarvisUser
$jarvisPrincipal = New-ScheduledTaskPrincipal -UserId $jarvisUser -LogonType Interactive -RunLevel Limited
$jarvisSettings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $jarvisAction -Trigger $jarvisTrigger -Principal $jarvisPrincipal -Settings $jarvisSettings -Description 'Jarvis local price monitor; persistent jobs in project data directory.'
Write-Output "Installed $TaskName for next sign-in. Start now with: jarvis worker start"
