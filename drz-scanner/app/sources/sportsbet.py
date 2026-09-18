"""Sportsbet public-web racing API adapter: schedule + Win-or-Place prices.

Sportsbet's website is backed by undocumented public JSON endpoints under
``/apigw/sportsbook-racing/``. They are not an official developer API, so
this adapter is deliberately defensive:

* Every endpoint path lives in :data:`ENDPOINTS` — one place to update.
* Every response is archived by :class:`ArchivingClient` before parsing.
* Parsing raises :class:`SchemaMismatchError`, pointing at the archived
  payload, rather than guessing when required structure is missing.

Price codes
-----------
A selection's ``prices`` list carries one entry per *price code*. Only code
``L`` is the live fixed-odds price — the number a bet is actually struck at,
and the only number this tool ever values or stakes against.

The other codes observed in the wild (``MDP``, ``TMD``) are **not** used as
live prices. Sportsbet's racing rules describe a family of tote-derivative
products — Top Tote, Top Tote Plus, Middle Div / Midi Div (the middle of the
three TAB dividends) — whose returns are only known after the totes declare,
and which the site presents as *indicative* odds. The letters are suggestive
(``TMD`` ~ "Top/Mid Div", ``MDP`` ~ "Mid Div Plus"), but the mapping from
code to product has **not** been established from a live response by this
build; see BUILD_STATUS.md. Rather than assume, the adapter captures any
non-``L`` code that carries prices as ``price_type="tote_indicative"``,
records the code verbatim, and refuses to stake it. If a future live payload
establishes the mapping, only :data:`PRICE_CODE_NOTES` needs updating.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Iterator

from app.config import get_settings
from app.http import ArchivingClient, FetchResult
from app.sources.base import (
    MeetingInfo,
    PriceQuote,
    RaceInfo,
    RaceStub,
    RunnerInfo,
    SchemaMismatchError,
)

log = logging.getLogger(__name__)

SOURCE = "sportsbet"

ENDPOINTS = {
    # All meetings and races for a date (YYYY-MM-DD).
    "all_racing": "{base}/apigw/sportsbook-racing/Sportsbook/Racing/AllRacing/{date}",
    # One race event's racecard. priceCode=L asks for the live price; the
    # response has been observed to carry the other codes regardless, which
    # is what lets us capture them without a second request.
    "racecard": (
        "{base}/apigw/sportsbook-racing/Sportsbook/Racing/Events/{event_id}/Racecard"
        "?includePrices=true&includeRacecard=true&priceCode=L"
    ),
}

#: The one code that is a real, takeable fixed price.
LIVE_PRICE_CODE = "L"

#: What we can and cannot say about each observed code. ``established`` means
#: the meaning was confirmed against a live Sportsbet response by this build.
PRICE_CODE_NOTES: dict[str, dict[str, Any]] = {
    "L": {
        "price_type": "fixed",
        "established": True,
        "meaning": "Live fixed-odds price — the price a bet is struck at.",
        "evidence": (
            "Requested explicitly via priceCode=L and observed live on "
            "2026-08-22 as the code whose win price is withdrawn when a "
            "runner is scratched, while MDP/TMD persist."
        ),
    },
    "MDP": {
        "price_type": "tote_indicative",
        "established": False,
        "meaning": (
            "NOT ESTABLISHED. Observed carrying a win price that persists "
            "after a runner is scratched, so it is not a live fixed price. "
            "Consistent with an indicative tote-derivative quote (Sportsbet's "
            "rules describe Mid Div / Middle Div products), but the code has "
            "not been confirmed against a live response."
        ),
        "evidence": "Code observed live 2026-08-22; product mapping unverified.",
    },
    "TMD": {
        "price_type": "tote_indicative",
        "established": False,
        "meaning": (
            "NOT ESTABLISHED. Same status as MDP: a persistent, non-live "
            "win price consistent with a tote-derivative indicative quote "
            "(Top Tote / Mid Div family), unconfirmed."
        ),
        "evidence": "Code observed live 2026-08-22; product mapping unverified.",
    },
}


def classify_price_code(code: str) -> str:
    """``"fixed"`` for the live code, ``"tote_indicative"`` for anything else.

    Unknown codes are treated as indicative — the conservative direction. A
    code we cannot vouch for must never be able to price a bet.
    """
    if (code or "").strip().upper() == LIVE_PRICE_CODE:
        return "fixed"
    return "tote_indicative"


def _url(name: str, **kwargs: str) -> str:
    return ENDPOINTS[name].format(base=get_settings().sportsbet_base_url, **kwargs)


# ---------------------------------------------------------------------------
# Tolerant JSON helpers
# ---------------------------------------------------------------------------

def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _walk_dicts(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dicts(v)


def _to_dt(value: Any) -> datetime | None:
    """Epoch seconds/millis or ISO-8601 -> aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _decimal(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v > 1.0:
        return float(v)
    if isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return None
        return f if f > 1.0 else None
    return None


# Candidate field names for the win and place decimal price inside one
# ``prices`` entry. ``winPrice``/``placePrice`` are the names carried by the
# live-shape racecard fixture inherited from the Jockey-Megabet scanner; the
# alternates are accepted so a rename degrades to "still works" rather than
# "silently wrong". A ``prices`` entry that carries neither is reported.
WIN_PRICE_KEYS = ("winPrice", "winPriceDecimal", "returnWin", "winOdds", "win")
PLACE_PRICE_KEYS = (
    "placePrice", "placePriceDecimal", "returnPlace", "placeOdds", "place",
)


def _price_from_entry(entry: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = _decimal(entry.get(k))
        if v is not None:
            return v
    # Some shapes nest the decimal under a small object, e.g.
    # {"winPrice": {"decimal": 2.30}}.
    for k in keys:
        inner = entry.get(k)
        if isinstance(inner, dict):
            for ik in ("decimal", "decimalPrice", "price", "value"):
                v = _decimal(inner.get(ik))
                if v is not None:
                    return v
    return None


def extract_prices(selection: dict[str, Any]) -> dict[str, PriceQuote]:
    """All price codes carried by one selection, keyed by code.

    Entries with no usable win *or* place price are dropped: a bare
    ``{"priceCode": "L"}`` is how a scratched runner's live price appears.
    """
    out: dict[str, PriceQuote] = {}
    prices = selection.get("prices")
    if not isinstance(prices, list):
        return out
    for entry in prices:
        if not isinstance(entry, dict):
            continue
        code = str(_first(entry, "priceCode", "code", default="") or "").strip().upper()
        if not code:
            continue
        win = _price_from_entry(entry, WIN_PRICE_KEYS)
        place = _price_from_entry(entry, PLACE_PRICE_KEYS)
        if win is None and place is None:
            continue
        out[code] = PriceQuote(
            price_code=code,
            price_type=classify_price_code(code),
            win_price=win,
            place_price=place,
        )
    return out


# Sportsbet's racecard carries several markets per race with the same runners
# at different prices (Win or Place, Top 2, Top 3, ...). Only the win market's
# prices are win prices, so runner extraction is restricted to one market and
# never walks the whole document.
_WIN_MARKET_RANK = {"win or place": 0, "win only": 1, "win": 2}
_WIN_PLACE_MARKET = "win or place"


def find_win_or_place_market(payload: Any) -> dict[str, Any] | None:
    """The racecard's "Win or Place" market (or the closest win market)."""
    candidates: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for node in _walk_dicts(payload):
        name = _first(node, "name", "marketName")
        sels = node.get("selections")
        if not (isinstance(name, str) and isinstance(sels, list) and sels):
            continue
        norm = " ".join(name.strip().lower().split())
        if norm not in _WIN_MARKET_RANK:
            continue
        priced = any(
            isinstance(s, dict) and extract_prices(s) for s in sels
        )
        candidates.append(((_WIN_MARKET_RANK[norm], 0 if priced else 1), node))
    if not candidates:
        return None
    return min(candidates, key=lambda t: t[0])[1]


_SCRATCH_WORDS = ("scratched", "latescratched", "late_scratched", "removed", "withdrawn")


def runner_status(node: dict[str, Any], live: PriceQuote | None) -> str:
    """Scratched = statusCode "S", or isOut, or no live win price.

    All three are honoured because any one of them alone has been observed to
    lag: a runner can carry statusCode "A" with its live price already gone.
    """
    code = str(node.get("statusCode") or "").strip().upper()
    if code == "S":
        return "scratched"
    for key in ("isOut", "isScratched", "scratched"):
        if node.get(key) is True:
            return "scratched"
    for key in ("resultStatus", "runnerStatus", "status", "selectionStatus"):
        v = node.get(key)
        if isinstance(v, str) and v.replace(" ", "").lower() in _SCRATCH_WORDS:
            return "scratched"
    if live is None or live.win_price is None:
        return "scratched"
    return "active"


def parse_result_placings(value: Any) -> list[int]:
    """``"1,16,18"`` -> ``[1, 16, 18]`` (saddlecloth numbers, finishing order).

    Dead heats are carried through as they are written; the settler deals with
    them. An unparseable fragment makes the whole string unusable rather than
    silently truncating the placings.
    """
    if not isinstance(value, str) or not value.strip():
        return []
    parts = [p.strip() for p in re.split(r"[,/]", value) if p.strip()]
    out: list[int] = []
    for p in parts:
        m = re.match(r"^(\d+)", p)
        if not m:
            return []
        out.append(int(m.group(1)))
    return out


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class SportsbetClient:
    def __init__(self, client: ArchivingClient | None = None):
        self.client = client or ArchivingClient(SOURCE)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "SportsbetClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- schedule --------------------------------------------------------
    def fetch_schedule(
        self, for_date: date
    ) -> tuple[list[MeetingInfo], list[RaceStub], FetchResult]:
        result = self.client.get_json(_url("all_racing", date=for_date.isoformat()))
        meetings, stubs = parse_all_racing(result.json(), for_date)
        if not meetings:
            raise SchemaMismatchError(
                SOURCE,
                f"no thoroughbred meetings parsed from AllRacing/{for_date}",
                str(result.archive_path) if result.archive_path else None,
            )
        log.info(
            "sportsbet: %d thoroughbred meetings, %d races (%d open) for %s",
            len(meetings), len(stubs), sum(1 for s in stubs if s.is_open), for_date,
        )
        return meetings, stubs, result

    # -- racecard --------------------------------------------------------
    def fetch_racecard(self, event_id: str) -> RaceInfo:
        result = self.client.get_json(_url("racecard", event_id=event_id))
        race = parse_racecard(
            result.json(),
            event_id=event_id,
            fetched_at=result.fetched_at,
            archive_path=str(result.archive_path) if result.archive_path else None,
        )
        race.raw_sha256 = result.sha256
        return race


# ---------------------------------------------------------------------------
# Parsers (pure functions over already-fetched payloads, so they are testable
# against archived responses without any network)
# ---------------------------------------------------------------------------

#: Only thoroughbreds. Sportsbet's AllRacing sections carry className values
#: like "Horses - Aus/NZ"; harness and greyhound meetings are excluded so a
#: shared venue name can never mix codes.
HORSE_CLASS_PREFIX = "horse"


def is_thoroughbred_class(class_name: Any) -> bool:
    return isinstance(class_name, str) and class_name.strip().lower().startswith(
        HORSE_CLASS_PREFIX
    )


def parse_all_racing(
    payload: Any, for_date: date
) -> tuple[list[MeetingInfo], list[RaceStub]]:
    """Meetings and race stubs from ``AllRacing/{date}``, thoroughbred only."""
    meetings: list[MeetingInfo] = []
    stubs: list[RaceStub] = []
    seen_meetings: set[str] = set()

    for node in _walk_dicts(payload):
        events = _first(node, "events", "races")
        name = _first(node, "name", "venueName", "meetingName")
        if not (isinstance(events, list) and isinstance(name, str) and events):
            continue
        class_name = _first(node, "className", "raceType", "classType")
        if not is_thoroughbred_class(class_name):
            continue
        meeting_id = str(_first(node, "id", "meetingId", default=name))
        if meeting_id in seen_meetings:
            continue
        seen_meetings.add(meeting_id)
        meetings.append(
            MeetingInfo(
                source=SOURCE,
                source_id=meeting_id,
                venue=name.strip(),
                meeting_date=for_date,
                jurisdiction=_first(node, "stateCode", "regionName", "countryCode"),
                race_type=_first(node, "raceType"),
                class_name=class_name,
            )
        )
        for ev in events:
            if not isinstance(ev, dict):
                continue
            event_id = _first(ev, "id", "eventId")
            if event_id is None:
                continue
            rn = _first(ev, "raceNumber", "eventNumber", "number")
            stubs.append(
                RaceStub(
                    event_id=str(event_id),
                    race_number=int(rn) if isinstance(rn, (int, float)) else None,
                    start_time=_to_dt(
                        _first(ev, "startTime", "advertisedStartTime", "displayStartTime")
                    ),
                    name=_first(ev, "name", "eventName", "raceName"),
                    status_code=_first(ev, "statusCode"),
                    betting_status=_first(ev, "bettingStatus", "status"),
                    meeting_id=meeting_id,
                    meeting_name=name.strip(),
                    class_name=class_name,
                    result=_first(ev, "result"),
                )
            )
    return meetings, stubs


def parse_racecard(
    payload: Any,
    event_id: str,
    fetched_at: datetime | None = None,
    archive_path: str | None = None,
) -> RaceInfo:
    """One race's runners with their live win AND place prices.

    Raises :class:`SchemaMismatchError` when the Win-or-Place market is
    missing, when no runner can be parsed, or when no active runner carries a
    place price under the live code — the last of which would otherwise be
    indistinguishable from "this race has no place market", and would quietly
    produce a scanner that never finds anything.
    """
    fetched_at = fetched_at or datetime.now(timezone.utc)
    top = payload if isinstance(payload, dict) else {}

    market = find_win_or_place_market(payload)
    if market is None:
        raise SchemaMismatchError(
            SOURCE,
            f"racecard for event {event_id} has no Win/Win-or-Place market",
            archive_path,
        )
    market_name = " ".join(str(_first(market, "name", "marketName", default="")).lower().split())

    runners: list[RunnerInfo] = []
    seen: set[str] = set()
    for sel in market.get("selections", []):
        if not isinstance(sel, dict):
            continue
        horse = _first(sel, "name", "runnerName", "horseName")
        if not isinstance(horse, str) or not horse.strip():
            continue
        rid = str(_first(sel, "id", "selectionId", "runnerId", default=horse))
        if rid in seen:
            continue
        seen.add(rid)

        quotes = extract_prices(sel)
        live = quotes.get(LIVE_PRICE_CODE)
        status = runner_status(sel, live)
        jockey = _first(sel, "jockey", "jockeyName", "riderName", "rider")
        if isinstance(jockey, dict):
            jockey = _first(jockey, "name", "fullName")
        trainer = _first(sel, "trainer", "trainerName")
        if isinstance(trainer, dict):
            trainer = _first(trainer, "name", "fullName")
        saddle = _first(sel, "runnerNumber", "saddlecloth", "number")
        barrier = _first(sel, "drawNumber", "barrier", "barrierNumber")
        runners.append(
            RunnerInfo(
                source=SOURCE,
                source_id=rid,
                horse_name=horse.strip(),
                saddlecloth=int(saddle) if isinstance(saddle, (int, float)) else None,
                jockey_name=str(jockey).strip() if jockey else None,
                trainer_name=str(trainer).strip() if trainer else None,
                barrier=int(barrier) if isinstance(barrier, (int, float)) else None,
                status=status,
                win_price=live.win_price if live else None,
                place_price=live.place_price if live else None,
                price_type="fixed",
                prices_by_code=quotes,
                price_timestamp=fetched_at if live else None,
            )
        )

    if not runners:
        raise SchemaMismatchError(
            SOURCE, f"racecard for event {event_id} parsed with no runners", archive_path
        )

    status_raw = str(
        _first(top, "bettingStatus", "status", "eventStatus", default="")
    ).lower()
    status_code = str(_first(top, "statusCode", default="")).strip().upper()
    result_raw = _first(top, "result")
    placings = parse_result_placings(result_raw)
    if "abandon" in status_raw:
        status = "abandoned"
    elif status_code == "R" or placings or any(
        w in status_raw for w in ("result", "settled", "final", "paid")
    ):
        status = "resulted"
    else:
        status = "open"

    active = [r for r in runners if r.is_active]
    if status == "open" and active and not any(r.place_price for r in active):
        # The win market parsed but no place price did. Either the field
        # names changed or this really is a win-only market; we cannot tell
        # them apart from here, so we refuse rather than value a race with a
        # place price we invented.
        raise SchemaMismatchError(
            SOURCE,
            f"racecard for event {event_id} ('{market_name}'): {len(active)} active "
            f"runners carry a live win price but none carries a place price under "
            f"price code {LIVE_PRICE_CODE}. Expected one of {PLACE_PRICE_KEYS} in the "
            f"priceCode={LIVE_PRICE_CODE} entry.",
            archive_path,
        )

    start = _to_dt(
        _first(top, "startTime", "advertisedStartTime", "displayStartTime")
    )
    rn = _first(top, "raceNumber", "eventNumber", "number")
    return RaceInfo(
        source=SOURCE,
        source_id=str(event_id),
        race_number=int(rn) if isinstance(rn, (int, float)) else None,
        start_time=start,
        status=status,
        name=_first(top, "name", "eventName", "raceName"),
        venue=_first(top, "competitionName", "venueName", "meetingName"),
        meeting_source_id=str(_first(top, "competitionId", "meetingId", default="") or "")
        or None,
        runners=runners,
        result_placings=placings,
        result_raw=result_raw if isinstance(result_raw, str) else None,
        fetched_at=fetched_at,
        raw_archive_path=archive_path,
    )
