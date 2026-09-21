@echo off
rem The whole Australian day card in one pass: every race, every runner,
rem written to data\daycard.html and opened in your browser. A snapshot of
rem the prices at the moment you ran it - read the note at the top of the
rem page before acting on it.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
py -m app.drz --day --open --quiet %*
echo.
pause
