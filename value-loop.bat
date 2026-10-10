@echo off
rem Double-click: Sportsbet vs Betfair for races in the next 20 minutes, every minute. Close window to stop.
cd /d "%~dp0"
py -m app.race_value %*
pause
