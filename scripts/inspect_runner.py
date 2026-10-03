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
from app.sources.sportsbet import _extract_price, _runner_status, _walk_dicts  # noqa: E402

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
    ap.add_argument("--max", type=int, default=3, help="nodes to print (default 3)")
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
        for node in _walk_dicts(payload):
            name = node.get("runnerName") or node.get("horseName") or node.get("name")
            if not isinstance(name, str) or normalize_name(name) != want:
                continue
            if not any(k in node for k in ("prices", "price", "winPrice", "jockeyName", "runnerNumber")):
                continue
            shown += 1
            print(f"\n--- {f.name}  ({meta.get('url', '')[:110]})")
            for k in _INTERESTING:
                if k in node:
                    print(f"  {k}: {json.dumps(node[k])[:300]}")
            others = sorted(k for k in node if k not in _INTERESTING)
            print(f"  other keys: {', '.join(others)[:400]}")
            print(f"  parser says: status={_runner_status(node)!r} price={_extract_price(node)}")
            if shown >= args.max:
                return 0
    if not shown:
        print(f"runner {args.runner!r} not found in any archived racecard for {args.date}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
