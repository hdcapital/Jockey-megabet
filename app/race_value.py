"""Race value scanner: Sportsbet fixed odds against Betfair as the truth.

Easiest: double-click START.bat and pick 1. Or:

    python -m app.race_value            # live: every VALUE_INTERVAL_SECONDS
    python -m app.race_value --once     # one pass
    python -m app.race_value --report   # are the edges real?
    python -m app.race_value --all      # also show rows failing the gates

Every minute it finds the thoroughbred races jumping in the next
``VALUE_WINDOW_MINUTES`` (20) that have both a Sportsbet racecard and a
Betfair market, and compares every runner's Sportsbet fixed WIN and PLACE
price with the probability the exchange gives it, best edge first.

Making the two sources comparable
---------------------------------
* **Betfair becomes a probability from its depth.** For each runner the
  average back and lay odds for a test stake (``VALUE_DEPTH_STAKE``, $100)
  are taken from the three visible price levels, and their midpoint *in
  probability space* is the runner's raw probability, so a few dollars at
  the top of the book cannot move it. The market's raw probabilities are
  then scaled to add up to exactly 1 (win) or the number of places (place),
  which removes the spread.
* **The field is the same field.** A runner Sportsbet has scratched but the
  exchange still lists is taken out of Betfair's market before scaling (what
  Betfair's reduction factor does once it removes the runner). A runner the
  exchange has removed but Sportsbet still prices is reported, not valued.
* **Places only when the terms match**: Sportsbet's number of place
  dividends must equal the Betfair place market's number of winners.
* **Sportsbet is the price you are paid**, so it is not de-vigged; its
  overround is kept for the record.
* **Commission only enters the lock-in column** (back Sportsbet, lay Betfair
  at the average lay price for the test stake, ``BETFAIR_COMMISSION`` off the
  lay winnings), never the fair probability.
* **In-play and suspended markets are never compared**; the time between the
  two snapshots is checked.

Trust gates (a row failing one is hidden and never becomes a bet): spread
wider than ``VALUE_MAX_SPREAD_TICKS`` Betfair price steps, too little matched,
not enough money at the top of the book for the test stake, one-sided book,
a market whose prices don't add up, a delayed key, or an edge too large to
be real. A row becomes a **bet worth a look** only after it shows an edge of
at least ``VALUE_MIN_EV`` on ``VALUE_CONFIRM_SCANS`` scans in a row.

Every signal is then followed to the jump (closing price, polled every
``VALUE_CLOSE_POLL_SECONDS`` near the jump) and to the result (Betfair's
runner status and Starting Price), and written to
``data/value_results.csv`` for ``--report``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from rich.console import Console

from app.config import PROJECT_ROOT, get_settings
from app.sources.betfair import BetfairNotConfiguredError
from app.value.display import render
from app.value.report import render_report, results_line
from app.value.scanner import SYDNEY, ScanResult, ValueScanner
from app.value.sound import Chime

log = logging.getLogger(__name__)

DATA = PROJECT_ROOT / "data"
RESULTS_PATH = DATA / "value_results.csv"


def setup_file_logging(verbose: bool = False) -> None:
    """Log to data/logs/ so the screen stays readable."""
    logs = DATA / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(
        logs / f"race_value-{datetime.now(SYDNEY):%Y-%m-%d}.log", encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def describe_settled(sig) -> str:
    word = {"won": "WON", "placed": "PLACED", "lost": "lost", "unplaced": "missed a place",
            "void": "was scratched (bet void)", "unknown": "result not found"}.get(
        sig.result or "", sig.result)
    bet = "win" if sig.kind == "WIN" else "place"
    line = (f"Result: {sig.horse} ({sig.venue} R{sig.race_number} {bet} at "
            f"${sig.bet_odds:.2f}) {word}")
    if sig.close_p:
        clv = sig.close_p * sig.bet_odds - 1
        line += f" · vs Betfair's closing price {clv:+.1%}"
    return line


def live(args: argparse.Namespace, settings, console: Console) -> int:
    try:
        scanner = ValueScanner(settings, archive=args.archive)
    except BetfairNotConfiguredError:
        console.print(
            "[red]Betfair isn't set up yet.[/red] It is the source of truth for this "
            "scanner, so it can't run without it.\n"
            "Double-click [bold]START.bat[/bold] and choose [bold]3[/bold] to enter your "
            "Betfair login (or run: python -m app.setup_wizard)."
        )
        return 2
    interval = timedelta(seconds=args.interval or settings.value_interval_seconds)
    sound = "off" if args.no_sound else (settings.value_sound or "off").strip().lower()
    chime = Chime(DATA, settings.value_sound_file)
    tick = 1.0  # countdown refresh; Betfair polls are rate-limited inside between_scans
    recent: deque[str] = deque(maxlen=5)  # results settled since the screen last cleared
    try:
        while True:
            try:
                result = scanner.scan_once()
            except Exception as exc:  # a loop must survive one bad pass
                log.exception("scan failed")
                result = ScanResult(at=datetime.now(timezone.utc),
                                    error=f"Unexpected problem: {exc}")
            next_at = result.at + interval
            fresh = set(scanner.tracker.new_confirmations) if result.error is None else set()
            alert = fresh or (sound == "watching" and result.error is None
                              and scanner.tracker.new_signals)
            if alert and sound in ("confirmed", "watching"):
                chime.play()  # once per scan, however many new bets
            render(
                result, settings,
                pending=scanner.tracker.pending_count(),
                results_line=results_line(RESULTS_PATH, result.at),
                recent_results=list(recent),
                next_at=None if args.once else next_at,
                show_all=args.all, top=args.top, console=console, new_keys=fresh,
                clear=not (args.once or args.no_clear),
            )
            if args.once:
                return 0 if result.error is None else 2
            with console.status("") as status:
                while True:
                    now = datetime.now(timezone.utc)
                    left = (next_at - now).total_seconds()
                    if left <= 0:
                        break
                    near = scanner.tracker.markets_near_jump(now, timedelta(seconds=90))
                    extra = (f" · watching {len(near)} race(s) at the jump for closing prices"
                             if near else "")
                    status.update(f"Next update in {left:.0f}s{extra}  (Ctrl+C to stop)")
                    for sig in scanner.between_scans(now):
                        line = describe_settled(sig)
                        recent.append(line)
                        console.print(f"[bold]{line}[/bold]")
                    time.sleep(min(tick, max(0.2, left)))
    except KeyboardInterrupt:
        console.print("\nStopped. Bets still being followed are saved and will be settled "
                      "next time you start the scanner.")
        return 0
    finally:
        scanner.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.race_value",
        description="Sportsbet fixed odds vs Betfair, races in the next N minutes",
    )
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--report", action="store_true", help="results report, then exit")
    parser.add_argument("--window", type=int, help="minutes ahead (default VALUE_WINDOW_MINUTES)")
    parser.add_argument("--interval", type=int, help="seconds between scans")
    parser.add_argument("--all", action="store_true", help="also show rows failing the gates")
    parser.add_argument("--no-places", action="store_true", help="win markets only")
    parser.add_argument("--top", type=int, default=25, help="rows in the detail table")
    parser.add_argument("--no-clear", action="store_true", help="don't clear the screen")
    parser.add_argument("--no-sound", action="store_true", help="no chime for new bets")
    parser.add_argument("--archive", action="store_true",
                        help="archive raw responses (off by default: ~20 files a minute)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug detail in the log")
    args = parser.parse_args(argv)

    setup_file_logging(args.verbose)
    console = Console()
    if args.report:
        return render_report(RESULTS_PATH, console)
    settings = get_settings()
    if args.window:
        settings.value_window_minutes = args.window
    if args.no_places:
        settings.value_include_places = False
    return live(args, settings, console)


if __name__ == "__main__":
    sys.exit(main())
