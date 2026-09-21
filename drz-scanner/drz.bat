@echo off
setlocal
rem ---------------------------------------------------------------------
rem  drz-scanner - one click.
rem  Installs requirements on first run (and again whenever they change),
rem  refreshes the calibration when it is missing or stale, then starts
rem  the live loop. Display only: this never places a bet.
rem ---------------------------------------------------------------------
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

where py >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Python launcher 'py' not found. Install Python 3.11 or newer from
  echo   https://www.python.org/downloads/ and tick "Add Python to PATH".
  echo.
  pause
  exit /b 1
)

rem --- install (or re-install when requirements.txt has changed) --------
set NEED_INSTALL=0
if not exist ".installed" set NEED_INSTALL=1
if exist ".installed" (
  fc /b requirements.txt .installed >nul 2>&1
  if errorlevel 1 set NEED_INSTALL=1
)
if "%NEED_INSTALL%"=="1" (
  echo   Installing requirements...
  py -m pip install --upgrade pip
  py -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   Install failed. Fix the error above and run this file again.
    pause
    exit /b 1
  )
  copy /y requirements.txt .installed >nul
)

rem --- refresh the calibration if it is missing or older than 35 days ---
py -c "import sys; sys.path.insert(0,'.'); from app.calibration import load_calibration; c=load_calibration(); a=c.age_days; sys.exit(0 if (c.loaded_from_file and a is not None and a <= 35) else 1)"
if errorlevel 1 (
  echo   Calibration missing or older than 35 days - refitting from Betfair history...
  py -m app.calibrate
  if errorlevel 1 (
    echo.
    echo   Calibration refresh FAILED. Keeping the shipped parameters
    echo   ^(lam 0.71 / tau 0.70^) and continuing. See the error above.
    echo.
  )
)

echo.
echo   Starting the scanner. Ctrl-C to stop.
echo   Live report opens in your browser: data\latest.html   Log: data\logs\drz.log
echo.
py -m app.drz --loop --open --quiet %*

echo.
pause
