param(
    [string]$TaskName = "aivf-daily-score",
    [int]$StartupDelayMinutes = 3
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DailyScript = Join-Path $ProjectRoot "scripts\daily_score.py"

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}
if (-not (Test-Path $DailyScript)) {
    throw "Daily score script not found: $DailyScript"
}

$EscapedRoot = $ProjectRoot.Replace("'", "''")
$EscapedPython = $PythonExe.Replace("'", "''")
$EscapedScript = $DailyScript.Replace("'", "''")
$Command = "Set-Location '$EscapedRoot'; & '$EscapedPython' '$EscapedScript'; exit `$LASTEXITCODE"
$Arguments = "-NoProfile -WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -Command `"$Command`""

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $Arguments
$DailyTrigger = New-ScheduledTaskTrigger -Daily -At "08:45"
$StartupTrigger = New-ScheduledTaskTrigger -AtStartup
$StartupTrigger.Delay = "PT${StartupDelayMinutes}M"

$Principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($DailyTrigger, $StartupTrigger) `
    -Principal $Principal `
    -Settings $Settings `
    -Description "AI Video Factory morning maintenance: 08:45 daily and delayed startup" `
    -Force | Out-Null

$Task = Get-ScheduledTask -TaskName $TaskName
$Info = Get-ScheduledTaskInfo -TaskName $TaskName
[PSCustomObject]@{
    TaskName = $Task.TaskName
    State = $Task.State
    Principal = $Task.Principal.UserId
    LogonType = $Task.Principal.LogonType
    StartWhenAvailable = $Task.Settings.StartWhenAvailable
    MultipleInstances = $Task.Settings.MultipleInstances
    NextRunTime = $Info.NextRunTime
    Triggers = ($Task.Triggers | ForEach-Object {
        "$($_.CimClass.CimClassName): Start=$($_.StartBoundary) Delay=$($_.Delay)"
    }) -join "; "
}
