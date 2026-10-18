# Windows PowerShell wrapper (UNVERIFIED on Windows: written and tested only on Linux). Run from the project folder.
$ErrorActionPreference = "Stop"
$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
& $py "$PSScriptRoot\acc.py" start
exit $LASTEXITCODE
