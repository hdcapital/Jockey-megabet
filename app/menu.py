"""The START menu: everything the race value scanner does, by number.

Run: double-click START.bat (Windows) or start.command (Mac).
"""

from __future__ import annotations

import os
import subprocess
import sys

from app.config import PROJECT_ROOT, Settings

MENU = """
 ┌──────────────────────────────────────────────────────────────┐
 │  RACE VALUE SCANNER — Sportsbet vs Betfair                   │
 ├──────────────────────────────────────────────────────────────┤
 │  1  Start the live scanner (races in the next 20 minutes)    │
 │  2  Results report — are the edges real?                     │
 │  3  Set up / change my Betfair login                         │
 │  4  Test my Betfair login                                    │
 │  5  Open the results spreadsheet                             │
 │  6  Open the settings file                                   │
 │  7  Play the alert sound                                     │
 │  Q  Quit                                                     │
 └──────────────────────────────────────────────────────────────┘
"""


def _run(module: str, *args: str) -> int:
    proc = subprocess.Popen([sys.executable, "-m", module, *args], cwd=PROJECT_ROOT)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue  # Ctrl+C reaches the child too; let it shut down cleanly


def _open(path) -> None:
    if not path.exists():
        print(f"\n{path.name} doesn't exist yet.")
        return
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.call(["open", str(path)])
    else:
        subprocess.call(["xdg-open", str(path)])


def betfair_configured() -> bool:
    s = Settings()
    return bool(s.betfair_app_key and s.betfair_username and s.betfair_password)


def main() -> int:
    if not betfair_configured():
        print("\nWelcome! First, let's connect your Betfair account "
              "(the scanner uses Betfair as the true price).")
        _run("app.setup_wizard")
    while True:
        print(MENU)
        try:
            choice = input(" Choose 1-7 or Q: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return 0
        if choice == "1":
            print("\nStarting... (press Ctrl+C to stop and come back here)\n")
            _run("app.race_value")
        elif choice == "2":
            _run("app.race_value", "--report")
            input("\n Press Enter to go back to the menu...")
        elif choice == "3":
            _run("app.setup_wizard")
            input("\n Press Enter to go back to the menu...")
        elif choice == "4":
            from app.setup_wizard import test_login
            test_login()
            input("\n Press Enter to go back to the menu...")
        elif choice == "5":
            _open(PROJECT_ROOT / "data" / "value_results.csv")
        elif choice == "6":
            env, example = PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.example"
            if not env.exists() and example.exists():
                env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            _open(env)
        elif choice == "7":
            _run("app.value.sound")
        elif choice in ("q", "quit", "exit"):
            return 0


if __name__ == "__main__":
    sys.exit(main())
