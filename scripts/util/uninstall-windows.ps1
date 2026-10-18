# uninstall-windows.ps1 -- remove Parley from a Windows machine (iteration 22, D4).
# ASCII only: PowerShell 5.1 reads BOM-less scripts as ANSI, so non-ASCII breaks parsing.
#
#   .\scripts\util\uninstall-windows.bat              # remove the package + autostart shortcut
#   .\scripts\util\uninstall-windows.bat -PurgeData   # ALSO delete ~/.parley (configs, tokens, logs)
#
# Without -PurgeData your ~/.parley stays untouched: it holds your private configs
# (API keys, account secrets) and the agents' state - reinstalling later picks them
# right back up.

param(
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"

# 1. Stop what's running (best effort): every registered daemon, then stray trays.
$registry = Join-Path $env:USERPROFILE ".parley\servers.json"
if (Test-Path $registry) {
    try {
        $reg = Get-Content $registry -Raw | ConvertFrom-Json
        foreach ($p in $reg.PSObject.Properties) {
            $entry = $p.Value
            try {
                Invoke-RestMethod -Method Post -TimeoutSec 3 `
                    -Uri ("http://{0}:{1}/v1/shutdown" -f $entry.host, $entry.port) | Out-Null
                Write-Host ("stopped daemon '{0}' (port {1})." -f $entry.name, $entry.port)
            } catch { }
        }
    } catch { }
}

# 1b. The tray app itself, if it's running (pid file written by parley_desktop).
$pidFile = Join-Path $env:USERPROFILE ".parley\desktop.pid"
if (Test-Path $pidFile) {
    try {
        $desktopPid = [int](Get-Content $pidFile -Raw).Trim()
        Stop-Process -Id $desktopPid -Force -ErrorAction Stop
        Write-Host "stopped Parley Desktop (pid $desktopPid)."
    } catch { }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# 2. Shortcuts the installer created (Startup + Start Menu).
$lnkPaths = @(
    (Join-Path ([Environment]::GetFolderPath("Startup")) "Parley Desktop.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Parley Desktop.lnk")
)
foreach ($lnkPath in $lnkPaths) {
    if (Test-Path $lnkPath) {
        Remove-Item $lnkPath -Force
        Write-Host "removed shortcut: $lnkPath"
    }
}

# 2b. The /parley and /myorganizer skills, if setup installed them into Claude Code.
# Done BEFORE the pip uninstall, because the code that knows which files it owns lives
# in the package. It removes only the files it put there, and keeps a directory if you
# have put anything of your own beside them.
try {
    & python -c "from parley.install_config.skill_install import uninstall; ok, detail = uninstall(); print(('removed Parley skills: ' if ok else 'could not remove the Parley skills: ') + str(detail))"
} catch {
    Write-Host "could not remove the Parley skills (Parley already gone?) - delete ~/.claude/skills/parley and ~/.claude/skills/myorganizer yourself if they are still there."
}

# 3. The package + its console scripts (parley, parley_desktop, parley-server).
& python -m pip uninstall -y parley
Write-Host "package removed. (Shared libraries like pystray/Pillow stay - other tools may use them;"
Write-Host " remove them yourself with: python -m pip uninstall pystray Pillow windows-toasts)"

# 4. Private data - only on explicit request, with a confirm.
if ($PurgeData) {
    $home_parley = Join-Path $env:USERPROFILE ".parley"
    if (Test-Path $home_parley) {
        Write-Host ""
        Write-Warning "-PurgeData will DELETE $home_parley - your configs (API keys, account"
        Write-Warning "secrets), OAuth tokens, negotiation state and journals. Not recoverable."
        $answer = Read-Host "Type DELETE to confirm"
        if ($answer -ceq "DELETE") {
            Remove-Item $home_parley -Recurse -Force
            Write-Host "deleted $home_parley"
        } else {
            Write-Host "kept $home_parley (nothing deleted)."
        }
    }
} else {
    Write-Host "your ~/.parley (configs, tokens, state, logs) was left untouched."
}

Write-Host ""
Write-Host "Parley uninstalled."
