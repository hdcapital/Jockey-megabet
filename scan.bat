@echo off
rem Double-click to run one scan. Window stays open so you can read the table.
cd /d "%~dp0"
py -m app.scan %*
pause
