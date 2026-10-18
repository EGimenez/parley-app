@echo off
REM install-windows.bat — double-clickable installer wrapper (iteration 22, D4).
REM Runs install-windows.ps1 with a one-shot ExecutionPolicy bypass, so it works on
REM machines where running .ps1 scripts is disabled (the Windows default).
REM Pass-through args:  scripts\util\install-windows.bat -Autostart     (tray at login)
REM                     scripts\util\install-windows.bat -NoDesktop     (CLI only)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" %*
if errorlevel 1 (
    echo.
    echo Install failed - see the messages above.
)
pause
