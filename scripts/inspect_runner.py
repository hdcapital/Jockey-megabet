"""Show how Sportsbet encodes a named runner in today's archived racecards.

Run:  python scripts/inspect_runner.py "Just In Time" [--date YYYY-MM-DD]

Scans data/raw/sportsbet/<date>/ (every response the scanner archived) for
selection nodes whose name matches, and prints each one's status-bearing
fields, price list and what the parser made of it. Paste the output back
when a runner looks scratched on the website but the scanner still prices
it: the fix is a field name in app/sources/sportsbet.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.matching.names import normalize_name  # noqa: E402
from app.sources.sportsbet import (  # noqa: E402
    SportsbetClient,
    _extract_price,
    _find_win_market,
    _first,
    _runner_status,
    _walk_dicts,
)

_INTERESTING = (
    "id", "name", "runnerName", "horseName", "runnerNumber", "statusCode", "status",
    "resultStatus", "runnerStatus", "selectionStatus", "isScratched", "scratched",
    "isOut", "scratchedTime", "scratchingTime", "jockeyName", "jockey", "prices",
    "price", "winPrice", "bettingStatus", "outcomeStatus", "selectionState",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runner")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--max", type=int, default=12, help="nodes to print (default 12)")
    args = ap.parse_args()
    want = normalize_name(args.runner)
    day_dir = get_settings().raw_archive_dir / "sportsbet" / args.date
    files = sorted(day_dir.glob("*.json")) if day_dir.exists() else []
    if not files:
        print(f"no archived Sportsbet responses in {day_dir}")
        return 1
    print(f"{len(files)} archived responses in {day_dir}")
    shown = 0
    for f in files:
        with open(f, "rb") as fh:
            meta = json.loads(fh.readline())
            try:
                payload = json.loads(fh.read())
            except ValueError:
                continue
        hits = []
        # Walk market by market so each selection is shown with its market.
        for market in _walk_dicts(payload):
            sels = market.get("selections")
            if not isinstance(sels, list):
                continue
            for node in sels:
                if not isinstance(node, dict):
                    continue
                name = node.get("runnerName") or node.get("horseName") or node.get("name")
                if isinstance(name, str) and normalize_name(name) == want:
                    hits.append((market, node))
        if not hits:
            continue
        print(f"\n=== {f.name}  ({meta.get('url', '')[:110]})")
        win = _find_win_market(payload)
        if win is not None:
            wsels = [x for x in win.get("selections", []) if isinstance(x, dict)]
            print(f"  parser's win market: {_first(win, 'name', 'marketName')!r} id={win.get('id')} "
                  f"statusCode={win.get('statusCode')!r} selections={len(wsels)} "
                  f"with jockeyName={sum(1 for x in wsels if x.get('jockeyName'))} "
                  f"with runnerNumber={sum(1 for x in wsels if x.get('runnerNumber') is not None)}")
        else:
            print("  parser's win market: NONE found")
        for market, node in hits:
            shown += 1
            mname = _first(market, "name", "marketName")
            print(f"  --- in market {mname!r} id={market.get('id')} "
                  f"marketStatusCode={market.get('statusCode')!r} "
                  f"{'<== the win market' if market is win else ''}")
            for k in _INTERESTING:
                if k in node:
                    print(f"      {k}: {json.dumps(node[k])[:900]}")
            others = sorted(k for k in node if k not in _INTERESTING)
            print(f"      other keys: {', '.join(others)[:400]}")
            print(f"      field rule: status={_runner_status(node)!r} price={_extract_price(node)}")
        try:
            card = SportsbetClient.parse_racecard(
                SportsbetClient.__new__(SportsbetClient), payload, event_id="inspect"
            )
            for race in card.races:
                for r in race.runners:
                    if normalize_name(r.horse_name) == want:
                        print(f"  PARSED RUNNER: status={r.status!r} win_odds={r.win_odds} "
                              f"saddlecloth={r.saddlecloth} jockey={r.jockey_name!r} "
                              f"source_id={r.source_id}")
        except Exception as exc:  # the inspector must still print the raw nodes
            print(f"  parse_racecard failed: {type(exc).__name__}: {exc}")
        if shown >= args.max:
            return 0
    if not shown:
        print(f"runner {args.runner!r} not found in any archived racecard for {args.date}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
