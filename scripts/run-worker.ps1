$ErrorActionPreference = 'Stop'
$jarvisRoot = Split-Path -Parent $PSScriptRoot
$jarvisPython = Join-Path $jarvisRoot '.venv\Scripts\python.exe'
$jarvisLog = Join-Path $jarvisRoot 'data\worker-scheduled.log'
& $jarvisPython -m jarvis --home $jarvisRoot worker run *>> $jarvisLog
exit $LASTEXITCODE
