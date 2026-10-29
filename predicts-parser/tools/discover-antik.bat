@echo off
rem Double-click to run: opens LBank and MEXC in your anti-detect browser profile
rem and records the prediction market endpoints (see docs\lbank-mexc.md).
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

echo.
echo Which anti-detect browser do you use?
echo   1 - Dolphin Anty
echo   2 - AdsPower
echo   3 - Octo Browser
echo   4 - other: the profile is already open and I know its debug port
set "CHOICE="
set /p CHOICE=Number: 
if "%CHOICE%"=="4" goto cdp

set "ANTIK=dolphin"
if "%CHOICE%"=="2" set "ANTIK=adspower"
if "%CHOICE%"=="3" set "ANTIK=octo"
echo Close this profile in the anti-detect browser first - the script starts it itself.
set "PROFILE="
set /p PROFILE=Profile ID (AdsPower: ID or No. number, Octo: profile UUID): 
set "TOKEN="
set /p TOKEN=API token (press Enter to skip): 
set "TOKENARG="
if not "%TOKEN%"=="" set "TOKENARG=--antik-token %TOKEN%"
py tools\discover.py --probe-lbank --antik %ANTIK% --profile "%PROFILE%" %TOKENARG%
goto done

:cdp
set "PORT="
set /p PORT=Debug port: 
py tools\discover.py --probe-lbank --cdp %PORT%

:done
echo.
echo Results are in: %cd%\discover-out
if exist discover-out explorer discover-out
pause
