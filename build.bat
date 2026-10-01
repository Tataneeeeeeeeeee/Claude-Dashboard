@echo off
REM Build the standalone Windows executable.
REM
REM   build.bat            build into dist\
REM   build.bat --clean    remove build\ and dist\ first
REM
REM The result is a single .exe that needs no Python and shows no console.

setlocal
cd /d "%~dp0"

if /i "%~1"=="--clean" (
  echo removing build\ and dist\
  if exist build rmdir /s /q build
  if exist dist rmdir /s /q dist
)

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

if not exist ".venv" (
  echo creating virtualenv
  %PY% -m venv .venv
  if errorlevel 1 goto :failed
)
set "VPY=.venv\Scripts\python.exe"

echo installing build dependencies
"%VPY%" -m pip install --quiet --upgrade pip
"%VPY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :failed
"%VPY%" -m pip install --quiet "pyinstaller>=6.5"
if errorlevel 1 goto :failed

REM Regenerate the icons so the bundle never ships a stale one.
"%VPY%" tools\make_icon.py >nul

echo building
"%VPY%" -m PyInstaller agentboard.spec --noconfirm --log-level WARN
if errorlevel 1 goto :failed

if not exist "dist\Agentboard.exe" (
  echo error: the build produced no dist\Agentboard.exe
  goto :failed
)

echo.
echo built dist\Agentboard.exe
echo Double-click it to run. WebView2 ships with Windows 10 and 11; if the
echo window does not appear, install the Microsoft Edge WebView2 Runtime.
echo.
pause
exit /b 0

:failed
echo.
echo Build failed. See the messages above.
pause
exit /b 1
