@echo off
setlocal

echo.
echo  ============================================
echo     Nudge — Setup
echo  ============================================
echo.

:: Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python is not installed or not in PATH.
    echo  Download it from https://www.python.org/downloads/
    echo  IMPORTANT: Check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo  [1/3] Installing dependencies...
python -m pip install -r "%~dp0requirements.txt" --quiet 2>nul
if errorlevel 1 (
    python -m pip install -r "%~dp0requirements.txt" --user --quiet
    if errorlevel 1 (
        echo  ERROR: Failed to install dependencies.
        pause
        exit /b 1
    )
)
echo         Done.
echo.

echo  [2/3] Setting up auto-start...
copy /Y "%~dp0start_nudge.vbs" "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Nudge.vbs" >nul
echo         Nudge will start automatically when you log in.
echo.

echo  [3/3] Launching Nudge...
for /f "delims=" %%i in ('python -c "import sys, pathlib; print(pathlib.Path(sys.executable).parent / 'pythonw.exe')"') do set "PYTHONW=%%i"
if not exist "%PYTHONW%" set "PYTHONW=pythonw"
cd /d "%~dp0"
start "" "%PYTHONW%" launcher.py
echo.
echo  ============================================
echo     Setup complete! Nudge is in your system tray.
echo  ============================================
pause
