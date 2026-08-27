@echo off
rem Double-click to run the backtest over everything the scanner has saved.
cd /d "%~dp0"
py -m app.backtest %*
pause
