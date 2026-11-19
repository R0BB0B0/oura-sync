@echo off
rem Weekly Oura sync + CSV export, run by Windows Task Scheduler. Output goes to sync.log.
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
echo ===== %date% %time% >> sync.log
"%PY%" -u oura_sync.py sync >> sync.log 2>&1
"%PY%" -u oura_sync.py export >> sync.log 2>&1
