@echo off
rem Best horse for Sportsbet's "bonus bet back if 2nd or 3rd" promotion.
rem Name the promo races, e.g.:   bonusback.bat --promo "Randwick:7" --promo "Flemington:4"
rem or a whole meeting:           bonusback.bat --meeting Randwick
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
py -m app.bonusback_scan %*
echo.
pause
