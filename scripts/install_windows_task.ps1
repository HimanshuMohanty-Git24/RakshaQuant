<#
.SYNOPSIS
    Register the RakshaQuant paper session and its dead-man check with Windows Task Scheduler
    (plan M12.4).

.DESCRIPTION
    Two tasks, in the Task Scheduler folder \RakshaQuant\, for the current user:

      * "RakshaQuant paper session": Monday to Friday at 09:05 IST it opens a console window
        running today's session behind the web console:
            python scripts\run_live_trading.py --mode web --exit-after-session
        The window prints the console URL (with this launch's token). The process exits by
        itself after the session (15:50 IST), or at once on an NSE holiday: the application
        skips holidays itself, so the trigger is simply every weekday.
      * "RakshaQuant dead-man check": Monday to Friday at 10:00 and 13:00 IST it runs
            python scripts\deadman_check.py
        which sends a Telegram alarm if the session's heartbeat is more than 3 minutes old.

    Both run only while you are logged on (a locked screen is fine): the session needs a
    visible console for its URL. The trigger times are converted from IST to this machine's
    time zone. Re-running the script replaces both tasks; -Unregister removes them; -DryRun
    only prints what would be registered.

    Nothing here edits .env or any other file. See docs/runbooks/daily-ops.md.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -DryRun
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$Python = "",
    [switch]$DryRun,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
# Windows PowerShell 5.1 has no $PSScriptRoot in parameter defaults: resolve it here.
if ($RepoRoot -eq "") { $RepoRoot = Split-Path -Parent $PSScriptRoot }

$TaskPath = "\RakshaQuant\"
$SessionTask = "RakshaQuant paper session"
$DeadmanTask = "RakshaQuant dead-man check"
$Weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

if ($Unregister) {
    foreach ($name in @($SessionTask, $DeadmanTask)) {
        $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction SilentlyContinue
        if ($null -eq $existing) {
            Write-Host "not registered: $name"
        } elseif ($DryRun) {
            Write-Host "would unregister: $name"
        } else {
            Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $name -Confirm:$false
            Write-Host "unregistered: $name"
        }
    }
    exit 0
}

# -- checks (nothing is registered when one fails) ----------------------------------------------
$RepoRoot = (Resolve-Path $RepoRoot).Path
if ($Python -eq "") { $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe" }
$problems = @()
if (-not (Test-Path (Join-Path $RepoRoot "scripts\run_live_trading.py"))) {
    $problems += "not the repository root: $RepoRoot"
}
if (-not (Test-Path $Python)) {
    $problems += "no Python at $Python (run 'uv sync --extra web --extra decision-local' first)"
}
if ($problems.Count -gt 0 -and -not $DryRun) {
    $problems | ForEach-Object { Write-Error $_ -ErrorAction Continue }
    exit 2
}
$problems | ForEach-Object { Write-Warning $_ }

$envFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Warning ".env not found: the session would run in ENVIRONMENT=dev. Create it from .env.example."
} elseif (-not (Select-String -Path $envFile -Pattern "^\s*ENVIRONMENT\s*=\s*[`"']?paper[`"']?\s*(#.*)?$" -Quiet)) {
    Write-Warning ".env does not set ENVIRONMENT=paper: the month run's state must go to var\paper\."
}
if (-not (Test-Path (Join-Path $RepoRoot "frontend\dist\index.html"))) {
    Write-Warning "the web console is not built: run 'cd frontend; npm ci; npm run build'."
}

# -- IST trigger times in this machine's time zone ----------------------------------------------
$ist = [TimeZoneInfo]::FindSystemTimeZoneById("India Standard Time")
$local = [TimeZoneInfo]::Local
if ($local.Id -ne $ist.Id) {
    $drift = "this machine's time zone is '{0}', not IST: trigger times are converted, and drift by an hour across its daylight-saving changes"
    Write-Warning ($drift -f $local.Id)
}

function Get-LocalTrigger([int]$Hour, [int]$Minute) {
    # Today's IST wall time, converted; a day shift moves the weekdays with it.
    $nowIst = [TimeZoneInfo]::ConvertTime([DateTime]::UtcNow, $ist)
    $at = New-Object DateTime($nowIst.Year, $nowIst.Month, $nowIst.Day, $Hour, $Minute, 0,
                              ([DateTimeKind]::Unspecified))
    $converted = [TimeZoneInfo]::ConvertTime($at, $ist, $local)
    $shift = ($converted.Date - $at.Date).Days
    $days = $Weekdays | ForEach-Object {
        [string][System.DayOfWeek](([int][System.DayOfWeek]$_ + $shift + 7) % 7)
    }
    return @{ At = $converted; Days = $days; Ist = ("{0:D2}:{1:D2} IST" -f $Hour, $Minute) }
}

$session = Get-LocalTrigger 9 5
$checks = @((Get-LocalTrigger 10 0), (Get-LocalTrigger 13 0))

$sessionArgs = "scripts\run_live_trading.py --mode web --exit-after-session"
$deadmanArgs = "scripts\deadman_check.py"

Write-Host ""
Write-Host "Repository : $RepoRoot"
Write-Host "Python     : $Python"
Write-Host ("Session    : {0} -> {1:HH:mm} local, {2}; python {3}" -f
            $session.Ist, $session.At, ($session.Days -join ","), $sessionArgs)
foreach ($c in $checks) {
    Write-Host ("Dead-man   : {0} -> {1:HH:mm} local, {2}; python {3}" -f
                $c.Ist, $c.At, ($c.Days -join ","), $deadmanArgs)
}
Write-Host "Runs as    : $env:USERDOMAIN\$env:USERNAME, only while logged on"
Write-Host ""

if ($DryRun) {
    Write-Host "dry run: nothing registered"
    exit 0
}

# -- register ---------------------------------------------------------------------------------
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$sessionSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)
Register-ScheduledTask -TaskPath $TaskPath -TaskName $SessionTask -Force `
    -Description "RakshaQuant: today's paper session behind the web console (docs/runbooks/daily-ops.md)" `
    -Action (New-ScheduledTaskAction -Execute $Python -Argument $sessionArgs -WorkingDirectory $RepoRoot) `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $session.Days -At $session.At) `
    -Settings $sessionSettings -Principal $principal | Out-Null
Write-Host "registered: $SessionTask"

$deadmanSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$deadmanTriggers = $checks | ForEach-Object {
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek $_.Days -At $_.At
}
Register-ScheduledTask -TaskPath $TaskPath -TaskName $DeadmanTask -Force `
    -Description "RakshaQuant: Telegram alarm when the session's heartbeat is stale (docs/runbooks/incident.md)" `
    -Action (New-ScheduledTaskAction -Execute $Python -Argument $deadmanArgs -WorkingDirectory $RepoRoot) `
    -Trigger $deadmanTriggers -Settings $deadmanSettings -Principal $principal | Out-Null
Write-Host "registered: $DeadmanTask"
Write-Host "Start the session by hand with: schtasks /Run /TN `"$TaskPath$SessionTask`""
