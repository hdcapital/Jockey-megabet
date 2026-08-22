"""Deep diagnostic: print the exact runner/market-level Sportsbet schema.

Follow-up to probe_endpoints.py. Fetches REAL current data and prints:

1. The Jockey Extras events in the Megabets listing, and the FULL racecard
   payload of the first ones (these hold the actual Megabet markets).
2. For one upcoming, priced, ordinary race: its market names, the full JSON
   of the first Win-market selections, any dict mentioning a jockey/rider,
   and an inventory of interesting key names with the path where each first
   appears — everything needed to finish the parser without guessing.

Run:  python scripts/probe_deep.py     (paste ALL output back for analysis)
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.http import ArchivingClient, SourceUnavailableError  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.sources import sportsbet  # noqa: E402

INTEREST = re.compile(
    r"price|jockey|rider|scratch|isout|out$|runner|number|barrier|weight|"
    r"status|result|market|selection|racecard|form|silk|handicap|win|place",
    re.I,
)


def walk_with_path(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k, v
            yield from walk_with_path(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node[:3]):  # sample first few elements
            yield from walk_with_path(v, f"{path}[{i}]")


def key_inventory(payload, limit=120):
    seen: dict[str, str] = {}
    for path, key, _ in walk_with_path(payload):
        if key not in seen and INTEREST.search(key):
            seen[key] = path
        if len(seen) >= limit:
            break
    print(f"    KEY INVENTORY ({len(seen)} interesting keys, first path each):")
    for key in sorted(seen):
        print(f"      {key:30s} {seen[key][:150]}")


def dump(label, obj, cap):
    text = json.dumps(obj)
    print(f"    {label} ({len(text)} chars{', TRUNCATED' if len(text) > cap else ''}):")
    print("      " + text[:cap])


def get(client, url):
    try:
        return client.get_json(url).json()
    except SourceUnavailableError as exc:
        print(f"    UNAVAILABLE: {exc}")
        return None


def find_markets(payload):
    """All dicts that look like markets: a name plus a selections list."""
    out = []
    for node in sportsbet._walk_dicts(payload):
        name = node.get("name") or node.get("marketName")
        sels = node.get("selections")
        if isinstance(name, str) and isinstance(sels, list) and sels:
            out.append((name, node))
    return out


def main() -> int:
    setup_logging()
    client = ArchivingClient("sportsbet")
    base = sportsbet.get_settings().sportsbet_base_url

    def racecard_url(event_id):
        return sportsbet.ENDPOINTS["racecard"].format(base=base, event_id=event_id)

    # ------------------------------------------------------------------ 1
    print("\n=== 1) Jockey Extras events in the Megabets listing")
    listing = get(client, sportsbet.ENDPOINTS["megabets"].format(base=base))
    stubs = sportsbet.SportsbetClient.jockey_extras_events(listing) if listing else []
    for s in stubs:
        print(f"    id={s.get('id')}  name={s.get('name')!r}  "
              f"numMarkets={s.get('numMarkets')}  statusCode={s.get('statusCode')}")
    if not stubs:
        print("    (none found right now)")

    for s in stubs[:2]:
        print(f"\n=== 2) FULL racecard of Jockey Extras event {s.get('id')} ({s.get('name')!r})")
        card = get(client, racecard_url(s["id"]))
        if card is not None:
            dump("FULL JSON", card, 14000)
            key_inventory(card)

    # ------------------------------------------------------------------ 3
    print("\n=== 3) One upcoming PRICED ordinary race")
    today = datetime.now(timezone.utc).date()
    target = None
    for offset in (0, 1):
        d = today + timedelta(days=offset)
        allr = get(
            client,
            sportsbet.ENDPOINTS["all_racing"].format(base=base, date=d.isoformat()),
        )
        if allr is None:
            continue
        for node in sportsbet._walk_dicts(allr):
            if (
                node.get("type") == "horse"
                and node.get("statusCode") == "A"
                and node.get("bettingStatus") == "PRICED"
                and node.get("id") is not None
            ):
                target = node
                break
        if target:
            break
    if target is None:
        print("    no upcoming priced horse race found in today/tomorrow listings")
    else:
        print(f"    chosen: id={target['id']} {target.get('displayName')!r} "
              f"start={target.get('startTime')}")
        card = get(client, racecard_url(target["id"]))
        if card is not None:
            print("    TOP-LEVEL KEYS:", sorted(card.keys() if isinstance(card, dict) else []))
            markets = find_markets(card)
            print(f"    MARKET-LIKE NODES ({len(markets)}):")
            for name, _ in markets[:30]:
                print(f"      - {name}")
            win = next((m for n, m in markets if n.strip().lower() == "win"), None)
            if win is None and markets:
                win = markets[0][1]
            if win is not None:
                sels = win.get("selections", [])
                for i, sel in enumerate(sels[:2]):
                    dump(f"WIN MARKET SELECTION {i} FULL", sel, 4000)
            jock = next(
                (n for n in sportsbet._walk_dicts(card)
                 if any(re.search(r"jockey|rider", k, re.I) for k in n)),
                None,
            )
            if jock is not None:
                dump("FIRST DICT WITH A JOCKEY/RIDER KEY", jock, 3000)
            else:
                print("    NO dict with a jockey/rider key found in this racecard")
            key_inventory(card)

    client.close()
    print("\n=== deep probe complete — paste everything above back for analysis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
