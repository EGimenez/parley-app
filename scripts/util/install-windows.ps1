# install-windows.ps1 -- set up Parley on a Windows machine (iteration 22, D4).
# ASCII only: PowerShell 5.1 reads BOM-less scripts as ANSI, so non-ASCII breaks parsing.
#
#   .\scripts\util\install-windows.ps1              # editable install with the desktop extra
#   .\scripts\util\install-windows.ps1 -Autostart   # + start the tray app (parley_desktop) at login
#   .\scripts\util\install-windows.ps1 -NoDesktop   # CLI only (no pystray/Pillow/windows-toasts)
#
# Blocked by ExecutionPolicy? Run scripts\util\install-windows.bat instead (one-shot bypass).
# Uses whatever `python` resolves in THIS shell -- activate your venv first if you
# want the install to land there.

param(
    [switch]$Autostart,
    [switch]$NoDesktop,
    # Non-interactive mode (iteration 26 A2): the windowless GUI installer
    # (install_gui.py) collects the choices up front and passes them as these
    # switches, so this script asks NOTHING via Read-Host. Run directly (double-
    # click util\install-windows.bat) it stays fully interactive.
    [switch]$NonInteractive,
    [switch]$Configure,       # run the setup wizard at the end
    [switch]$StartTrayNow,    # launch the tray in this session
    [switch]$NoStopRunning    # do NOT stop running parley (default under -NonInteractive: stop)
)

$ErrorActionPreference = "Stop"
# The script lives in scripts/util/; the repo root (pyproject.toml) is two up.
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent

# One prompt helper: honour the passed switch under -NonInteractive, else ask.
function Confirm-Step([string]$prompt, [bool]$paramValue, [bool]$defaultYes) {
    if ($NonInteractive) { return $paramValue }
    $suffix = if ($defaultYes) { "[Y/n]" } else { "[y/N]" }
    $ans = Read-Host "$prompt $suffix"
    if ($defaultYes) { return ($ans -notmatch '^[nN]') } else { return ($ans -match '^[yY]') }
}

# 1. Python >= 3.11.
try {
    $ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
} catch {
    Write-Error "python not found on PATH. Install Python 3.11+ (python.org or winget) and rerun."
}
$parts = $ver.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    Write-Error "Python $ver found, but Parley needs 3.11+."
}
Write-Host "Python $ver - ok."

# 1b. Running Parley processes hold a lock on Scripts\parley.exe, which makes the
# pip upgrade below die with WinError 32. Detect and offer to stop them first.
# /F-style stop is safe: daemons are restart-safe (journal on disk) and respawn
# on the next parley call; the tray restarts from its shortcut.
$running = Get-Process -Name "parley", "parley_desktop", "parley-server" -ErrorAction SilentlyContinue
if ($running) {
    $names = ($running | Select-Object -ExpandProperty Name -Unique) -join ", "
    Write-Host "Parley is currently running ($names) - the upgrade cannot replace files in use."
    if (Confirm-Step "Stop all Parley processes now (agents restart cleanly on the next parley call)?" (-not $NoStopRunning) $true) {
        $running | Stop-Process -Force
        Start-Sleep -Seconds 1
        Write-Host "Stopped."
    } else {
        Write-Error "Cannot install while Parley is running - stop it (tray icon > Exit, or: taskkill /F /IM parley.exe) and rerun."
    }
}

# 2. Editable install (+ desktop extra unless -NoDesktop).
$target = if ($NoDesktop) { "." } else { ".[desktop]" }
Write-Host "pip install -e $target ..."
Push-Location $repo
& python -m pip install -e $target
$pipOk = ($LASTEXITCODE -eq 0)
Pop-Location
if (-not $pipOk) { Write-Error "pip install failed - see output above." }

# 2b. Make sure the launcher exes are actually reachable. A system Python without
# admin rights makes pip fall back to a USER install, whose Scripts dir is usually
# NOT on PATH - the number one "parley is not recognized" cause.
$candidates = @(
    (& python -c "import sysconfig; print(sysconfig.get_path('scripts'))"),
    (& python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))")
)
$scriptsDir = $candidates | Where-Object { $_ -and (Test-Path (Join-Path $_ "parley.exe")) } | Select-Object -First 1
if ($scriptsDir) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $onPath = (($env:Path + ";" + $userPath) -split ";") -contains $scriptsDir
    if (-not $onPath) {
        [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $scriptsDir), "User")
        Write-Host "Added to your user PATH: $scriptsDir"
        Write-Host ">> Open a NEW terminal for the parley / parley_desktop commands to resolve."
    }
    Write-Host "Installed: parley, parley_desktop, parley-server ($scriptsDir)."
} else {
    Write-Warning "Install finished but parley.exe was not found - run via: python -m parley"
}

# 2c. Seed ~/.parley (logs/ + the cfg.<backend>.example.yaml templates) NOW, so the
# next-steps below are already true. Otherwise it only appears on the first
# `parley -cfg ...` run -- which for a new user is a failing one.
& python -c "from parley.client_server.config import scaffold_home, PARLEY_HOME; scaffold_home(); print('Seeded ' + PARLEY_HOME + ' with the example configs.')"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not seed $env:USERPROFILE\.parley - it will be created on the first 'parley -cfg ...' run."
}

# 3. Shortcuts (unless -NoDesktop). parley_desktop is a GUI script (pythonw-based):
# no console, doesn't block a terminal.
function New-ParleyShortcut([string]$lnkPath) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($lnkPath)
    if ($scriptsDir -and (Test-Path (Join-Path $scriptsDir "parley_desktop.exe"))) {
        $lnk.TargetPath = (Join-Path $scriptsDir "parley_desktop.exe")
    } else {
        $pythonw = Join-Path (Split-Path (& python -c "import sys; print(sys.executable)")) "pythonw.exe"
        if (-not (Test-Path $pythonw)) { $pythonw = "pythonw.exe" }
        $lnk.TargetPath = $pythonw
        $lnk.Arguments = "-m parley.client_server.parley_desktop"
    }
    $lnk.WorkingDirectory = $repo
    $lnk.Description = "Parley tray - questions, toasts, Win-Alt-P prompt"
    $icoPath = Join-Path $repo "src\parley\client_server\assets\parley.ico"
    if (Test-Path $icoPath) { $lnk.IconLocation = "$icoPath,0" }
    $lnk.Save()
}

if (-not $NoDesktop) {
    # 3a. Start Menu entry: double-clickable, and findable by pressing Win and
    # typing "parley".
    $programs = [Environment]::GetFolderPath("Programs")
    $startMenuLnk = Join-Path $programs "Parley Desktop.lnk"
    New-ParleyShortcut $startMenuLnk
    Write-Host "Start Menu shortcut written: $startMenuLnk  (press Win, type 'parley')"

    # 3b. Tray at login: -Autostart forces it; otherwise ask.
    $wantAutostart = $Autostart
    if (-not $Autostart) {
        $wantAutostart = Confirm-Step "Start Parley Desktop automatically when you log in?" $Autostart $false
    }
    if ($wantAutostart) {
        $startupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Parley Desktop.lnk"
        New-ParleyShortcut $startupLnk
        Write-Host "Autostart shortcut written: $startupLnk  (delete it there to undo)"
    }
} elseif ($Autostart) {
    Write-Error "-Autostart needs the desktop extra (drop -NoDesktop)."
}

# 4. First agent (iteration 23): the setup wizard asks for the LLM key and an
# account (gmail / outlook / yahoo), verifies EVERYTHING live, and writes
# ~/.parley/cfg_<name>.yaml. Launched under pythonw with PARLEY_SETUP_GUI=1 so
# the Setup Window opens with NO black console behind it (iteration 26 A1).
Write-Host ""
if (Confirm-Step "Configure your first agent now (LLM key + one account, ~5-10 min)?" $Configure $true) {
    $pythonwExe = Join-Path (Split-Path (& python -c "import sys; print(sys.executable)")) "pythonw.exe"
    if (-not (Test-Path $pythonwExe)) { $pythonwExe = "pythonw" }
    $env:PARLEY_SETUP_GUI = "1"
    $setup = Start-Process $pythonwExe -ArgumentList "-m", "parley", "--setup" -Wait -PassThru
    Remove-Item Env:\PARLEY_SETUP_GUI -ErrorAction SilentlyContinue
    if ($setup.ExitCode -ne 0) {
        Write-Warning "Setup did not finish - rerun it anytime with:  parley --setup"
    }
} elseif (-not $NonInteractive) {
    # (Under -NonInteractive the GUI installer opens the wizard itself, in its
    # own window - so this "later" note would contradict it.)
    Write-Host "Later: run  parley --setup  (guided, everything verified live) - or copy a"
    Write-Host "cfg.<backend>.example.yaml from $env:USERPROFILE\.parley to cfg_<name>.yaml by hand."
}

# 5. Tray NOW (iteration 23 user feedback): the login shortcut only helps at the
# NEXT login - offer to start Parley Desktop in this session too.
if (-not $NoDesktop) {
    if (Confirm-Step "Start Parley Desktop (tray icon) now?" $StartTrayNow $true) {
        if ($scriptsDir -and (Test-Path (Join-Path $scriptsDir "parley_desktop.exe"))) {
            Start-Process (Join-Path $scriptsDir "parley_desktop.exe")
        } else {
            Start-Process pythonw -ArgumentList "-m", "parley.client_server.parley_desktop"
        }
        Write-Host "Parley Desktop started - look for the sail icon in the system tray."
    }
}

Write-Host ""
Write-Host "Then (open a NEW terminal so parley resolves):"
Write-Host "  parley set me a meeting with x@y.com friday about sync"
if (-not $Autostart) {
    Write-Host "  parley_desktop                     (tray: toasts, questions, Win-Alt-P)"
}
Write-Host ""
Write-Host "Uninstall anytime with scripts\util\uninstall-windows.bat (your ~/.parley configs stay)."
