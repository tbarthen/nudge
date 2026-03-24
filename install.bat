@echo off
setlocal enabledelayedexpansion

echo.
echo  ============================================
echo     Nudge — Installer
echo  ============================================
echo.
echo  TIP: If Windows shows a blue "Protected your PC"
echo  screen, click "More info" then "Run anyway".
echo  If you see a "Do you want to allow this app"
echo  prompt, click Yes.
echo.
echo  --------------------------------------------
echo.

set "INSTALL_DIR=%USERPROFILE%\Nudge"
set "REPO_URL=https://github.com/tbarthen/nudge.git"
set "NEED_PATH_REFRESH=0"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

:: -------------------------------------------
:: Pre-flight: check network connectivity
:: (curl works even when ping/ICMP is blocked)
:: -------------------------------------------
curl -sf --max-time 5 https://github.com >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Cannot reach github.com
    echo  Please check your internet connection and try again.
    echo.
    echo  If you are connected, your network may be blocking
    echo  the connection. Try from a different network.
    echo.
    pause
    exit /b 1
)

:: -------------------------------------------
:: Pre-flight: check winget is available
:: -------------------------------------------
winget --version >nul 2>&1
if errorlevel 1 (
    set "HAS_WINGET=0"
) else (
    set "HAS_WINGET=1"
)

:: -------------------------------------------
:: Step 1: Ensure Python is installed
:: -------------------------------------------
echo  [1/5] Checking for Python...

:: Detect the Windows Store "python" alias (opens Store instead of running)
set "PYTHON_OK=0"
python --version >nul 2>&1 && set "PYTHON_OK=1"
if !PYTHON_OK!==1 (
    python -c "import sys" >nul 2>&1
    if errorlevel 1 set "PYTHON_OK=0"
)

if !PYTHON_OK!==0 (
    if !HAS_WINGET!==0 (
        echo.
        echo  Python is not installed. Please install it manually:
        echo    1. Go to https://www.python.org/downloads/
        echo    2. Click the big yellow "Download Python" button
        echo    3. Run the downloaded file
        echo    4. IMPORTANT: Check the box "Add Python to PATH"
        echo       at the BOTTOM of the installer before clicking Install
        echo    5. After it finishes, close this window and
        echo       double-click install.bat again
        echo.
        pause
        exit /b 1
    )
    echo         Not found. Installing Python via winget...
    echo         (If prompted, click Yes to allow the install)
    echo.
    winget install Python.Python.3 --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo.
        echo  Automatic install failed. Please install Python manually:
        echo    1. Go to https://www.python.org/downloads/
        echo    2. Click the big yellow "Download Python" button
        echo    3. Run the downloaded file
        echo    4. IMPORTANT: Check the box "Add Python to PATH"
        echo       at the BOTTOM of the installer before clicking Install
        echo    5. Then close this window and double-click install.bat again
        echo.
        pause
        exit /b 1
    )
    set "NEED_PATH_REFRESH=1"
) else (
    echo         Found.
)
echo.

:: -------------------------------------------
:: Step 2: Ensure Git is installed
:: -------------------------------------------
echo  [2/5] Checking for Git...
git --version >nul 2>&1
if errorlevel 1 (
    if !HAS_WINGET!==0 (
        echo.
        echo  Git is not installed. Please install it manually:
        echo    1. Go to https://git-scm.com/downloads/win
        echo    2. Click "Click here to download" at the top
        echo    3. Run the downloaded file
        echo    4. Click Next on every screen (defaults are fine)
        echo    5. After it finishes, close this window and
        echo       double-click install.bat again
        echo.
        pause
        exit /b 1
    )
    echo         Not found. Installing Git via winget...
    echo         (If prompted, click Yes to allow the install)
    echo.
    winget install Git.Git --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo.
        echo  Automatic install failed. Please install Git manually:
        echo    1. Go to https://git-scm.com/downloads/win
        echo    2. Click "Click here to download" at the top
        echo    3. Run the downloaded file (defaults are fine)
        echo    4. Then close this window and double-click install.bat again
        echo.
        pause
        exit /b 1
    )
    set "NEED_PATH_REFRESH=1"
) else (
    echo         Found.
)
echo.

:: -------------------------------------------
:: Refresh PATH if we installed anything
:: -------------------------------------------
if !NEED_PATH_REFRESH!==1 (
    echo  Refreshing PATH after installations...

    :: Re-read PATH from registry (system + user) and expand variables
    set "NEW_PATH="
    for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do (
        call set "NEW_PATH=%%b"
    )
    for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do (
        call set "NEW_PATH=!NEW_PATH!;%%b"
    )
    if defined NEW_PATH set "PATH=!NEW_PATH!"
    echo.

    :: Verify they actually work now
    python -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo  Python was installed but isn't available in this window yet.
        echo.
        echo  This is normal! Just:
        echo    1. Close this window
        echo    2. Double-click install.bat again
        echo.
        pause
        exit /b 1
    )
    git --version >nul 2>&1
    if errorlevel 1 (
        echo  Git was installed but isn't available in this window yet.
        echo.
        echo  This is normal! Just:
        echo    1. Close this window
        echo    2. Double-click install.bat again
        echo.
        pause
        exit /b 1
    )
    echo  PATH refreshed successfully.
    echo.
)

:: -------------------------------------------
:: Step 3: Clone or update the repository
:: -------------------------------------------
echo  [3/5] Setting up Nudge in %INSTALL_DIR%...
if exist "%INSTALL_DIR%\.git" (
    echo         Already installed — pulling latest updates...

    :: Stop only Nudge, not all pythonw processes (PowerShell because wmic is deprecated)
    powershell -NoProfile -Command "Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object {$_.Path -and (Resolve-Path $_.Path).Path.StartsWith((Resolve-Path '%INSTALL_DIR%').Path)} | Stop-Process -Force" >nul 2>&1

    cd /d "%INSTALL_DIR%"

    :: Reset any accidental local changes, then pull
    git checkout -- . >nul 2>&1
    git clean -fd >nul 2>&1
    git pull
    if errorlevel 1 (
        echo.
        echo  ERROR: Could not download updates.
        echo  Check your internet connection and try again.
        echo.
        echo  If this keeps happening, send Tom a screenshot
        echo  of this window.
        echo.
        pause
        exit /b 1
    )
) else (
    if exist "%INSTALL_DIR%" (
        echo.
        echo  ERROR: A folder called "Nudge" already exists in your
        echo  home folder but it's not a Nudge install.
        echo.
        echo  Please rename or delete:
        echo    %INSTALL_DIR%
        echo  Then double-click install.bat again.
        echo.
        pause
        exit /b 1
    )
    git clone "%REPO_URL%" "%INSTALL_DIR%"
    if errorlevel 1 (
        echo.
        echo  ERROR: Failed to download Nudge.
        echo  Check your internet connection and try again.
        echo.
        echo  If this keeps happening, send Tom a screenshot
        echo  of this window.
        echo.
        pause
        exit /b 1
    )
    cd /d "%INSTALL_DIR%"
)

:: Verify the clone/pull actually worked
if not exist "%INSTALL_DIR%\launcher.py" (
    echo.
    echo  ERROR: Download appeared to succeed but files are missing.
    echo  Please delete the folder:
    echo    %INSTALL_DIR%
    echo  Then double-click install.bat again.
    echo.
    echo  If this keeps happening, send Tom a screenshot
    echo  of this window.
    echo.
    pause
    exit /b 1
)
echo         Done.
echo.

:: -------------------------------------------
:: Step 4: Install Python dependencies
:: -------------------------------------------
echo  [4/5] Installing dependencies...

:: Suppress the "pip is out of date" warning — it's just noise
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

python -m pip install -r "%INSTALL_DIR%\requirements.txt" --quiet
if errorlevel 1 (
    echo         Retrying with --user flag...
    python -m pip install -r "%INSTALL_DIR%\requirements.txt" --user --quiet
    if errorlevel 1 (
        echo.
        echo  ERROR: Failed to install dependencies.
        echo.
        echo  Try right-clicking install.bat and choosing
        echo  "Run as administrator", then try again.
        echo.
        echo  If that doesn't work, send Tom a screenshot
        echo  of this window.
        echo.
        pause
        exit /b 1
    )
)
echo         Done.
echo.

:: -------------------------------------------
:: Step 5: Auto-start + launch
:: -------------------------------------------
echo  [5/5] Setting up auto-start...

:: Instead of copying start_nudge.vbs (which goes stale on updates),
:: create a small launcher that always runs from the install directory
(
    echo Set FSO = CreateObject^("Scripting.FileSystemObject"^)
    echo Set WshShell = CreateObject^("WScript.Shell"^)
    echo NudgeDir = "%INSTALL_DIR%"
    echo If FSO.FileExists^(NudgeDir ^& "\launcher.py"^) Then
    echo     WshShell.CurrentDirectory = NudgeDir
    echo     WshShell.Run "pythonw launcher.py", 0, False
    echo End If
) > "%STARTUP_DIR%\Nudge.vbs"

echo         Nudge will start automatically when you log in.
echo.

:: Find pythonw next to python.exe (in case pythonw isn't on PATH)
set "PYTHONW="
for /f "delims=" %%i in ('python -c "import sys; from pathlib import Path; p = Path(sys.executable).parent / 'pythonw.exe'; print(p) if p.exists() else print('')" 2^>nul') do set "PYTHONW=%%i"

if "!PYTHONW!"=="" (
    :: Last resort: try bare pythonw
    where pythonw >nul 2>&1
    if errorlevel 1 (
        echo  NOTE: Could not find pythonw.exe
        echo  Launching with python.exe instead.
        echo  (A black console window will stay open — this is OK)
        echo.
        set "PYTHONW=python"
    ) else (
        set "PYTHONW=pythonw"
    )
)

echo  Starting Nudge...
cd /d "%INSTALL_DIR%"
start "" "!PYTHONW!" launcher.py

:: Give it a moment to start, then check it's actually running
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command "if (-not (Get-Process pythonw -ErrorAction SilentlyContinue)) { if (-not (Get-Process python -ErrorAction SilentlyContinue)) { exit 1 } }" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  WARNING: Nudge may not have started successfully.
    echo  Try double-clicking start_nudge.vbs in:
    echo    %INSTALL_DIR%
    echo.
    echo  If that doesn't work, send Tom a screenshot
    echo  of any error you see.
    echo.
) else (
    echo         Nudge is running in your system tray!
    echo.
)

echo  ============================================
echo     All done!
echo  ============================================
echo.
echo  Look for the Nudge icon in your system tray
echo  (bottom-right of your screen — you may need
echo  to click the ^ arrow to see it).
echo.
echo  To update later, just double-click this
echo  install.bat file again.
echo.
pause
