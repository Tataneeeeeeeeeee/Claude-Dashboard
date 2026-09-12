@echo off
REM Development launcher for Windows.
REM
REM Creates .venv if missing, installs requirements.txt, then opens the app.
REM Pass --headless to serve the UI without a native window, or --check to
REM report which webview backend is available.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY=python"
where %PY% >nul 2>nul
if errorlevel 1 (
  set "PY=py -3"
  where py >nul 2>nul
  if errorlevel 1 (
    echo error: Python was not found on PATH. Install Python 3.11 or newer.
    pause
    exit /b 1
  )
)

for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])"') do set VER=%%v
if %VER% LSS 311 (
  echo error: Python 3.11 or newer is required.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo creating virtualenv in .venv
  %PY% -m venv .venv
  if errorlevel 1 goto :failed
)

set "VPY=.venv\Scripts\python.exe"

if not exist ".venv\.requirements-stamp" goto :install
for /f %%i in ('dir /b /o-d requirements.txt ".venv\.requirements-stamp" 2^>nul') do (
  if "%%i"=="requirements.txt" goto :install
  goto :run
)

:install
echo installing dependencies
"%VPY%" -m pip install --quiet --upgrade pip
"%VPY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :failed
echo. > ".venv\.requirements-stamp"

:run
"%VPY%" -m claude_dashboard.app %*
exit /b %errorlevel%

:failed
echo.
echo Setup failed. See the messages above.
pause
exit /b 1
