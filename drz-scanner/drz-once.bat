@echo off
rem One scan, then stop. The window stays open so you can read the table.
setlocal
cd /d "%~dp0"
py -m app.drz %*
echo.
pause
