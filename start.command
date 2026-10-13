#!/bin/bash
# Mac: double-click me (first time: right-click → Open). Same menu as START.bat.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python isn't installed. Get it from https://www.python.org/downloads/ then double-click this again."
  open https://www.python.org/downloads/
  read -p "Press Enter to close."; exit 1
fi
if ! python3 -c "import sys; sys.exit(sys.version_info < (3, 11))"; then
  echo "Python 3.11 or newer is needed: https://www.python.org/downloads/"
  read -p "Press Enter to close."; exit 1
fi
if ! python3 -c "import httpx, rich, pydantic_settings, sqlalchemy, tenacity" 2>/dev/null; then
  echo "First run: installing what the scanner needs. This takes a minute..."
  python3 -m pip install --disable-pip-version-check -q -r requirements.txt || {
    read -p "The install didn't finish (reason above). Press Enter to close."; exit 1; }
fi
python3 -m app.menu
