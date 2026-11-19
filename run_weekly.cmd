@echo off
rem Oura sync + CSV export. Run weekly by Task Scheduler, or double-click to run now.
rem Output is appended to data\sync.log.
cd /d "%~dp0"
if not exist data mkdir data

rem Prefer the installed Python; fall back to whatever "python" is on PATH.
set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"

echo ===== %date% %time% >> data\sync.log
"%PY%" -u oura_sync.py sync >> data\sync.log 2>&1
"%PY%" -u oura_sync.py export >> data\sync.log 2>&1
