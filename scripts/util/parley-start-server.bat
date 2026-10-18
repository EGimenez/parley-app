@echo off
REM Start the Parley client-server server from the repo root with src on PYTHONPATH.
REM Pass through args, e.g.:  scripts\util\parley-start-server.bat --config-file config\cfg_gmail.yaml
REM                          scripts\util\parley-start-server.bat memory        (credential-free demo)
cd /d "%~dp0..\.."
set "PYTHONPATH=src;%PYTHONPATH%"
python -m parley --serve %*
