@echo off
rem Refit lam/tau (and the Sportsbet beta, if enough prices have been stored)
rem from the free Betfair Australia historical files. Downloads ~80 MB the
rem first time and caches it under data\history.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
py -m app.calibrate %*
echo.
pause
