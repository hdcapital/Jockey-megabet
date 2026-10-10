"""Set up (or change) the Betfair login, step by step, and test it.

Run: double-click START.bat and choose 3, or ``python -m app.setup_wizard``.

Answers are written to the project's ``.env`` file (never committed, never
logged). Press Enter at any question to keep what is already there.
"""

from __future__ import annotations

import getpass
import logging
import sys
from pathlib import Path

from app.config import PROJECT_ROOT, Settings, get_settings

ENV_PATH = PROJECT_ROOT / ".env"


def quote(value: str) -> str:
    """A .env value that survives any character a password can hold."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def update_env(path: Path, values: dict[str, str]) -> None:
    """Set ``KEY='value'`` for each item, replacing an existing active line
    and leaving every other line (comments included) untouched."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            if key not in done:
                out.append(f"{key}={quote(values[key])}")
                done.add(key)
            continue
        out.append(line)
    for key, value in values.items():
        if key not in done:
            out.append(f"{key}={quote(value)}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _mask(v: str | None) -> str:
    if not v:
        return "not set"
    return v[:3] + "…" + v[-2:] if len(v) > 6 else "***"


def ask(prompt: str, current: str | None, secret: bool = False,
        mask: bool = False) -> str | None:
    if not current:
        shown = "not set"
    elif secret:
        shown = "(hidden)"
    else:
        shown = _mask(current) if mask else current
    text = f"{prompt} [{shown}]: "
    answer = getpass.getpass(text) if secret else input(text)
    answer = answer.strip()
    return answer or None


def test_login(console_print=print) -> bool:
    """Log in with what is in .env now; say plainly whether it worked."""
    from app.http import SourceUnavailableError
    from app.sources.betfair import BetfairClient, BetfairLoginError, BetfairNotConfiguredError

    root = logging.getLogger()
    if not root.handlers:  # keep the client's log lines off this screen
        root.addHandler(logging.NullHandler())
    get_settings.cache_clear()
    settings = Settings()
    try:
        client = BetfairClient(settings=settings)
    except BetfairNotConfiguredError:
        console_print("✗ The app key or username is missing.")
        return False
    try:
        client.login()
        console_print(f"✓ Betfair login works (via {client.status.identity_host}).")
        return True
    except BetfairLoginError as exc:
        console_print(f"✗ Betfair refused the login: {exc.code}\n  {exc.hint}")
    except SourceUnavailableError as exc:
        console_print(f"✗ Couldn't reach Betfair: {exc.detail}\n  Check your internet "
                      "connection; Betfair blocks some VPNs and cloud servers.")
    finally:
        client.close()
    return False


def main() -> int:
    print("\nBETFAIR SETUP\n=============\n"
          "The scanner needs three things from your Betfair account:\n"
          "  1. An application key  — log in at https://developer.betfair.com, then\n"
          "     My Account → API keys (the 'Live' key gives real-time prices; the\n"
          "     'Delayed' key works but its prices can be minutes old).\n"
          "  2. Your Betfair username (not your email).\n"
          "  3. Your Betfair password (typing is hidden).\n"
          "Press Enter at any question to keep the current value.\n")
    s = Settings()
    answers: dict[str, str] = {}
    try:
        for key, prompt, current, secret in (
            ("BETFAIR_APP_KEY", "Application key", s.betfair_app_key, False),
            ("BETFAIR_USERNAME", "Username", s.betfair_username, False),
            ("BETFAIR_PASSWORD", "Password", s.betfair_password, True),
        ):
            v = ask(prompt, current, secret, mask=key == "BETFAIR_APP_KEY")
            if v:
                answers[key] = v
        print("\nOptional (Enter to skip):")
        bank = ask("Your betting bank in $, for stake suggestions",
                   str(int(s.value_bankroll)) if s.value_bankroll else None)
        if bank:
            bank = bank.replace("$", "").replace(",", "")
            try:
                answers["VALUE_BANKROLL"] = str(float(bank))
            except ValueError:
                print(f"  '{bank}' isn't a number; skipped.")
        comm = ask("Your Betfair commission rate in %", f"{s.betfair_commission * 100:g}")
        if comm:
            try:
                answers["BETFAIR_COMMISSION"] = str(float(comm.rstrip("%")) / 100)
            except ValueError:
                print(f"  '{comm}' isn't a number; skipped.")
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; nothing changed.")
        return 1

    if answers:
        example = PROJECT_ROOT / ".env.example"
        if not ENV_PATH.exists() and example.exists():  # keep the explanations
            ENV_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        update_env(ENV_PATH, answers)
        print(f"\nSaved to {ENV_PATH}")
    else:
        print("\nNothing changed.")
    print("\nTesting the Betfair login...")
    ok = test_login()
    if ok:
        print("\nAll set. Start the scanner from START.bat (choice 1).")
    else:
        print("\nFix the item above and run this setup again (START.bat, choice 3).")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
