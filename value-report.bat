@echo off
title Race Value Scanner - results
rem Double-click: are the edges real?
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto nopython
py -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul
if errorlevel 1 goto oldpython
py -c "import httpx, rich, pydantic_settings, sqlalchemy, tenacity, tzdata" >nul 2>nul
if errorlevel 1 goto install
goto run

:install
echo.
echo  First run: installing what the scanner needs. This takes a minute...
echo.
py -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto installfailed
goto run

:nopython
echo.
echo  Python isn't installed yet.
echo  1. Your browser will open the Python download page.
echo  2. Download and run the installer. On the first screen TICK "Add python.exe to PATH".
echo  3. Then double-click this file again.
echo.
start "" https://www.python.org/downloads/
pause
exit /b 1

:oldpython
echo.
echo  Your Python is too old - version 3.11 or newer is needed.
echo  Install the latest from https://www.python.org/downloads/ then double-click this file again.
echo.
start "" https://www.python.org/downloads/
pause
exit /b 1

:installfailed
echo.
echo  The install didn't finish - the reason is printed above.
echo  Check you're online, then double-click this file again.
pause
exit /b 1

:run
py -m app.race_value --report
pause
