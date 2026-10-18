@echo off
REM parley — zero-install shim for the unified CLI (iteration 22).
REM Runs from your current directory (so a literal -cfg path resolves where you are);
REM only puts the repo's src on PYTHONPATH (the script lives in scripts/util/). E.g.:
REM    scripts\util\parley.bat -cfg gmail --default
REM    scripts\util\parley.bat "set me a meeting with x@y.com before friday"
set "PYTHONPATH=%~dp0..\..\src;%PYTHONPATH%"
python -m parley %*
