@echo off
setlocal
rem ---------------------------------------------------------------------
rem  BONUS BACK PICKER - just double-click this file.
rem  Picks the best horse for Sportsbet's "bonus bet back if 2nd or 3rd".
rem  The first run installs what it needs (a minute or two). After that
rem  it asks you three things - stake, meeting, race - by number.
rem  Nothing here ever places a bet.
rem ---------------------------------------------------------------------
cd /d "%~dp0"
title Bonus Back Picker
mode con: cols=120 lines=45 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set PY=
where py >nul 2>&1 && set PY=py
if "%PY%"=="" ( where python >nul 2>&1 && set PY=python )
if "%PY%"=="" (
  echo.
  echo   Python isn't installed on this computer yet.
  echo.
  echo   1. Go to  https://www.python.org/downloads/  and click the big Download button.
  echo   2. Run the installer and TICK THE BOX "Add Python to PATH" at the bottom.
  echo   3. Then double-click "BONUS BACK.bat" again.
  echo.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)

rem --- first run (or after an update): install the requirements ---------
set NEED_INSTALL=0
if not exist ".installed" set NEED_INSTALL=1
if exist ".installed" (
  fc /b requirements.txt .installed >nul 2>&1
  if errorlevel 1 set NEED_INSTALL=1
)
if "%NEED_INSTALL%"=="1" (
  echo.
  echo   First run: setting things up. This takes a minute or two, only once...
  echo.
  %PY% -m pip install --quiet --upgrade pip
  %PY% -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   Setup failed - see the message above. Check your internet and try again.
    pause
    exit /b 1
  )
  copy /y requirements.txt .installed >nul
  cls
)

%PY% -m app.bonusback_scan

echo.
echo   Finished. You can close this window.
pause
