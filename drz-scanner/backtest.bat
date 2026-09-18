@echo off
rem Settle stored signals from captured placings and report ROI, calibration
rem and closing-line value. Only ever uses observations captured live.
setlocal
cd /d "%~dp0"
py -m app.backtest %*
echo.
pause
