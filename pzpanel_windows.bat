@echo off
setlocal EnableDelayedExpansion

:: pzpanel Windows launcher
:: - Checks for Python 3.10+
:: - Creates a virtual environment if one doesn't exist
:: - Installs/updates requirements
:: - Launches pzpanel (main.py)
::
:: Place this file in the same directory as main.py and run it.
:: To run pzpanel at Windows startup, create a Scheduled Task that runs
:: this script as the desired user (with "Start in" set to this directory).

cd /d "%~dp0"

echo [pzpanel] Starting up...

:: -----------------------------------------------------------------------
:: Check Python is available and is 3.10+
:: -----------------------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [pzpanel] ERROR: Python not found. Install Python 3.10+ from https://python.org
    echo          Make sure to tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    set PYMAJ=%%a
    set PYMIN=%%b
)
if !PYMAJ! LSS 3 (
    echo [pzpanel] ERROR: Python 3.10+ required, found !PYVER!
    pause
    exit /b 1
)
if !PYMAJ! EQU 3 if !PYMIN! LSS 10 (
    echo [pzpanel] ERROR: Python 3.10+ required, found !PYVER!
    pause
    exit /b 1
)
echo [pzpanel] Python !PYVER! OK

:: -----------------------------------------------------------------------
:: Create virtual environment if it doesn't exist
:: -----------------------------------------------------------------------
if not exist "venv\Scripts\activate.bat" (
    echo [pzpanel] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [pzpanel] ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [pzpanel] Virtual environment created.
)

:: -----------------------------------------------------------------------
:: Activate venv and install/update requirements
:: -----------------------------------------------------------------------
call venv\Scripts\activate.bat

echo [pzpanel] Installing/updating requirements...
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [pzpanel] ERROR: Failed to install requirements.
    pause
    exit /b 1
)
echo [pzpanel] Requirements OK.

:: -----------------------------------------------------------------------
:: Check pzpanel.ini exists, copy example if not
:: -----------------------------------------------------------------------
if not exist "pzpanel.ini" (
    if exist "pzpanel.ini.example" (
        echo [pzpanel] No pzpanel.ini found -- copying pzpanel.ini.example as a starting point.
        copy "pzpanel.ini.example" "pzpanel.ini" >nul
        echo [pzpanel] IMPORTANT: Edit pzpanel.ini before continuing. At minimum set:
        echo            [rcon] password
        echo            [paths] server_ini, workshop_acf, console_log
        echo            [server] windows_mode and (if process mode) windows_start_script
        pause
        exit /b 0
    ) else (
        echo [pzpanel] WARNING: No pzpanel.ini or pzpanel.ini.example found.
        echo           Create pzpanel.ini manually. See the README for required keys.
    )
)

:: -----------------------------------------------------------------------
:: Launch pzpanel
:: -----------------------------------------------------------------------
echo [pzpanel] Launching on http://localhost:8080 ...
echo           Press Ctrl+C to stop.
echo.
python main.py

if errorlevel 1 (
    echo.
    echo [pzpanel] pzpanel exited with an error (code %errorlevel%).
    pause
)

endlocal
