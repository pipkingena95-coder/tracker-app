@echo off
rem Double-click to run: installs Playwright + Chromium on first run, then starts discover.py
cd /d "%~dp0.."
where py >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer.
  pause
  exit /b 1
)
py -c "import playwright" >nul 2>nul
if errorlevel 1 py -m pip install playwright
py -m playwright install chromium
if errorlevel 1 (
  echo Failed to install Chromium, see the messages above.
  pause
  exit /b 1
)
py tools\discover.py --probe-lbank
echo.
echo Results are in: %cd%\discover-out
if exist discover-out explorer discover-out
pause
