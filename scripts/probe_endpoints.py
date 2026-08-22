"""Diagnostic probe: hit each Sportsbet endpoint and report the REAL response.

Prints HTTP status, content type and a structural summary (key tree plus a
truncated payload sample) for every route in app.sources.sportsbet.ENDPOINTS,
so the live schema can be inspected from CI logs without guessing. Raw
responses are archived via the normal ArchivingClient path as well.

Run: python scripts/probe_endpoints.py
Exit code is 0 even on failures — this is a diagnostic, the output is the
product.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.http import ArchivingClient, SourceUnavailableError  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.sources import sportsbet  # noqa: E402

MAX_SAMPLE = 4000
MAX_KEYS = 30


def key_tree(node, depth=0, max_depth=4):
    """Summarise JSON structure: dict keys and list element types per level."""
    pad = "  " * depth
    lines = []
    if depth > max_depth:
        return [pad + "..."]
    if isinstance(node, dict):
        keys = list(node)
        lines.append(pad + "{" + ", ".join(keys[:MAX_KEYS]) +
                     (", ..." if len(keys) > MAX_KEYS else "") + "}")
        for k in keys[:8]:
            v = node[k]
            if isinstance(v, (dict, list)) and v:
                lines.append(pad + f".{k}:")
                lines.extend(key_tree(v, depth + 1, max_depth))
    elif isinstance(node, list):
        lines.append(pad + f"[list len={len(node)}]")
        if node:
            lines.extend(key_tree(node[0], depth + 1, max_depth))
    else:
        lines.append(pad + f"<{type(node).__name__}> {str(node)[:80]}")
    return lines


def probe(client: ArchivingClient, label: str, url: str):
    print(f"\n=== PROBE {label}: {url}")
    try:
        result = client.get_json(url)
    except SourceUnavailableError as exc:
        print(f"    UNAVAILABLE: {exc}")
        return None
    ctype = result.headers.get("content-type", "?")
    print(f"    HTTP {result.status_code}  content-type={ctype}  "
          f"bytes={len(result.body)}  sha256={result.sha256[:12]}  "
          f"archived={result.archive_path}")
    body = result.body.decode("utf-8", errors="replace")
    if "json" not in ctype and not body.lstrip().startswith(("{", "[")):
        print("    NON-JSON RESPONSE (first 800 chars):")
        print("    " + body[:800].replace("\n", "\n    "))
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"    JSON DECODE FAILED: {exc}; first 800 chars:")
        print("    " + body[:800].replace("\n", "\n    "))
        return None
    print("    KEY TREE:")
    for line in key_tree(payload):
        print("      " + line)
    print(f"    SAMPLE (first {MAX_SAMPLE} chars of compact JSON):")
    print("      " + json.dumps(payload)[:MAX_SAMPLE])
    return payload


def main() -> int:
    setup_logging()
    client = ArchivingClient("sportsbet")
    base = sportsbet.get_settings().sportsbet_base_url

    probe(client, "megabets", sportsbet.ENDPOINTS["megabets"].format(base=base))
    probe(client, "challenges", sportsbet.ENDPOINTS["challenges"].format(base=base))

    # AU racing 'today' can be UTC today or tomorrow; probe both.
    racecard_event = None
    for offset in (0, 1):
        d = (datetime.now(timezone.utc) + timedelta(days=offset)).date()
        payload = probe(
            client, f"all_racing[{d}]",
            sportsbet.ENDPOINTS["all_racing"].format(base=base, date=d.isoformat()),
        )
        if payload is not None and racecard_event is None:
            for node in sportsbet._walk_dicts(payload):
                races = sportsbet._first(node, "races", "events")
                if isinstance(races, list):
                    for rn in races:
                        if isinstance(rn, dict):
                            eid = sportsbet._first(rn, "id", "eventId")
                            if eid is not None:
                                racecard_event = str(eid)
                                break
                if racecard_event:
                    break

    if racecard_event:
        probe(
            client, f"racecard[{racecard_event}]",
            sportsbet.ENDPOINTS["racecard"].format(base=base, event_id=racecard_event),
        )
    else:
        print("\n=== no race event id discovered; racecard probe skipped")

    client.close()
    probe_betfair_reachability()
    print("\n=== probe complete")
    return 0


def probe_betfair_reachability() -> None:
    """Check the official Betfair API endpoints answer, WITHOUT credentials.

    Sends an empty login POST and an unauthenticated JSON-RPC call. Both are
    expected to be *rejected* — the point is to verify the network path and
    that the endpoints speak the documented protocol. No secrets involved.
    """
    from app.config import get_settings

    s = get_settings()
    bf = ArchivingClient("betfair")
    for label, url, kwargs in [
        (
            "identity login (no creds — expect a documented JSON rejection)",
            f"{s.betfair_identity_url}/api/login",
            dict(
                data={"username": "", "password": ""},
                headers={"X-Application": "probe",
                         "Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/json"},
            ),
        ),
        (
            "betting JSON-RPC (no session — expect INVALID_APP_KEY/NO_SESSION)",
            s.betfair_api_url,
            dict(
                json_body={"jsonrpc": "2.0",
                           "method": "SportsAPING/v1.0/listEventTypes",
                           "params": {"filter": {}}, "id": 1},
                headers={"X-Application": "probe", "Content-Type": "application/json"},
            ),
        ),
    ]:
        print(f"\n=== PROBE betfair {label}: {url}")
        try:
            result = bf.post_json(url, **kwargs)
            print(f"    HTTP {result.status_code}  "
                  f"content-type={result.headers.get('content-type', '?')}")
            print("    BODY (first 600 chars): "
                  + result.body.decode("utf-8", errors="replace")[:600])
        except SourceUnavailableError as exc:
            print(f"    UNAVAILABLE: {exc}")
    bf.close()


if __name__ == "__main__":
    sys.exit(main())
