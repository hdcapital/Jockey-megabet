@echo off
setlocal
rem ---------------------------------------------------------------------
rem  drz-scanner - one click.
rem  Installs requirements on first run, refreshes the calibration when it
rem  is missing or stale, then starts the live loop. Display only: this
rem  never places a bet.
rem ---------------------------------------------------------------------
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Python launcher 'py' not found. Install Python 3.11 or newer from
  echo   https://www.python.org/downloads/ and tick "Add Python to PATH".
  echo.
  pause
  exit /b 1
)

if not exist ".installed" (
  echo   First run: installing requirements...
  py -m pip install --upgrade pip
  py -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   Install failed. Fix the error above and run this file again.
    pause
    exit /b 1
  )
  echo installed > .installed
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
echo.
py -m app.drz --loop %*

echo.
pause
