@echo off
rem Settle stored signals from captured placings and report ROI, calibration
rem and closing-line value. Only ever uses observations captured live.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
py -m app.backtest %*
echo.
pause
