@echo off
REM uninstall-windows.bat — double-clickable uninstaller wrapper (iteration 22, D4).
REM Runs uninstall-windows.ps1 with a one-shot ExecutionPolicy bypass.
REM Pass-through args:  scripts\util\uninstall-windows.bat -PurgeData   (ALSO delete ~/.parley)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-windows.ps1" %*
pause
