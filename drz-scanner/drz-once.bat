@echo off
rem One scan, then stop. The window stays open so you can read the table.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
py -m app.drz %*
echo.
pause
