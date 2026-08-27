@echo off
rem Double-click to scan continuously (every 3 minutes). Close window to stop.
cd /d "%~dp0"
py -m app.scan --loop %*
pause
