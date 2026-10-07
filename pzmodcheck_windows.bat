@echo off
:: pzpanel mod-update check for Windows (v5.3.0)
:: The Windows equivalent of pzmodcheck.service + pzmodcheck.timer on
:: Linux: checks the Workshop for updated mods and, if any, runs the
:: in-game countdown and restarts the server.
::
:: Run pzpanel_windows.bat once first (it creates the venv). Then schedule
:: this file every 15 minutes with Task Scheduler -- see the README. Task
:: Scheduler's default "do not start a new instance" setting matters: a
:: postponed restart keeps this process running for up to ~25 hours.

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [pzmodcheck] ERROR: venv not found. Run pzpanel_windows.bat once first.
    exit /b 1
)

venv\Scripts\python.exe mod_restart.py %*
exit /b %errorlevel%
