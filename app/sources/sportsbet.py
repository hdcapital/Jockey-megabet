"""Sportsbet public-web racing API adapter.

Sportsbet's website is backed by undocumented public JSON endpoints under
``/apigw/sportsbook-racing/``. They are not an official developer API, so
this adapter is deliberately defensive:

* All endpoint paths live in :data:`ENDPOINTS` — one place to update.
* Parsing tolerates several candidate field names per attribute and raises
  :class:`SchemaMismatchError` (pointing at the archived raw payload)
  instead of guessing when required structure is missing.
* Every response is archived via :class:`ArchivingClient` before parsing.

IMPORTANT (schema verification status): these routes and field names are
based on prior public research on the Sportsbet web app. They must be
re-verified against live responses from a network that can reach
``www.sportsbet.com.au`` — see BUILD_STATUS.md. Nothing in this module
invents data: if the live schema differs, the adapter fails loudly with the
raw payload preserved for adaptation.
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
    MegabetOffer,
    RacecardInfo,
    RaceInfo,
    RunnerInfo,
    SchemaMismatchError,
)

log = logging.getLogger(__name__)

SOURCE = "sportsbet"

# All Sportsbet routes in one place. {base} is the site origin.
# Schema verified against live responses on 2026-08-22 (see BUILD_STATUS.md):
#   * "megabets" returns a JSON LIST of Racing Extras event stubs
#     ({id, name, competitionName, startTime, statusCode, ...}). Jockey
#     Megabets are the events with competitionName == "Jockey Extras",
#     one per meeting ("Jockey Extras - Sandown"); their actual markets
#     live in that event's Racecard.
#   * A "/Racing/Challenges" route no longer exists (live 404
#     ResourceNotFound), so it is not listed here.
#   * "all_racing" returns {dates: [{meetingDate, sections: [{raceType,
#     meetings: [{id, name, className, events: [{id, raceNumber, ...}]}]}]}]}.
ENDPOINTS = {
    "megabets": "{base}/apigw/sportsbook-racing/Sportsbook/Racing/Megabets",
    # All meetings for a date (YYYY-MM-DD).
    "all_racing": "{base}/apigw/sportsbook-racing/Sportsbook/Racing/AllRacing/{date}",
    # Racecard for one event (races AND Jockey Extras events), prices included.
    "racecard": (
        "{base}/apigw/sportsbook-racing/Sportsbook/Racing/Events/{event_id}/Racecard"
        "?includePrices=true&includeRacecard=true&priceCode=L"
    ),
}

# competitionName that carries Jockey Megabet markets (live-verified).
JOCKEY_EXTRAS_COMPETITION = "jockey extras"


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
    """Depth-first iterator over every dict in a nested JSON structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dicts(v)


def _extract_price(node: dict[str, Any]) -> float | None:
    """Pull a decimal win price out of a selection/runner dict.

    Sportsbet has used several shapes: a bare ``winPrice``/``price`` number,
    or a nested price object with ``winPrice``, or a list of price objects
    per bet type. Returns None when no positive decimal price is found.
    """
    for key in ("winPrice", "returnWin", "price", "odds", "decimalPrice"):
        v = node.get(key)
        if isinstance(v, (int, float)) and v > 1.0:
            return float(v)
        if isinstance(v, dict):
            inner = _extract_price(v)
            if inner:
                return inner
    prices = node.get("prices")
    if isinstance(prices, list):
        for p in prices:
            if isinstance(p, dict):
                inner = _extract_price(p)
                if inner:
                    return inner
    return None


_SCRATCH_WORDS = ("scratched", "latescratched", "late_scratched", "removed")


def _runner_status(node: dict[str, Any]) -> str:
    for key in ("resultStatus", "runnerStatus", "status", "selectionStatus"):
        v = node.get(key)
        if isinstance(v, str):
            low = v.replace(" ", "").lower()
            if low in _SCRATCH_WORDS:
                return "scratched"
    for key in ("isScratched", "scratched"):
        if node.get(key) is True:
            return "scratched"
    return "active"


def _to_dt(value: Any) -> datetime | None:
    """Parse epoch seconds/millis or ISO-8601 strings to aware UTC datetimes."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Megabet market-name parsing
# ---------------------------------------------------------------------------

# Observed naming patterns for Jockey Megabets, e.g.
#   "James McDonald to ride 2+ winners", "J McDonald 3+ Wins",
#   "Jockey Megabet - Craig Williams To Ride A Double".
_THRESHOLD_PATTERNS = [
    re.compile(r"(?P<jockey>.+?)\s+to\s+ride\s+(?P<k>\d+)\s*(?:\+|or\s+more)\s*win(?:ner)?s?", re.I),
    re.compile(r"(?P<jockey>.+?)\s+(?P<k>\d+)\s*\+\s*win(?:ner)?s?", re.I),
    re.compile(r"(?P<jockey>.+?)\s+to\s+ride\s+a\s+(?P<word>winner|double|treble|quaddie)", re.I),
    re.compile(r"(?P<jockey>.+?)\s+to\s+win\s+(?:on\s+)?(?P<k>\d+)\s*(?:\+|or\s+more)\s*(?:races|rides)", re.I),
]
_WORD_THRESHOLDS = {"winner": 1, "double": 2, "treble": 3, "quaddie": 4}
_NAME_PREFIX = re.compile(r"^(?:jockey\s+megabet\s*[-:–]\s*|jockey\s*[-:–]\s*)", re.I)
# Win-count markets that are not a *single named jockey's* win count.
_NOT_JOCKEY = re.compile(
    r"\b(trainer|stable|sire|owner|favourite|favorite)s?\b|\bany\s+jockey\b", re.I
)


def parse_jockey_threshold(market_name: str) -> tuple[str, int] | None:
    """Extract (jockey name, wins threshold) from a Megabet market name.

    Returns None when the name doesn't look like a jockey win-count market
    (e.g. trainer Megabets, favourites Megabets) — callers log and skip.
    """
    if _NOT_JOCKEY.search(market_name):
        return None
    cleaned = _NAME_PREFIX.sub("", market_name.strip())
    for pat in _THRESHOLD_PATTERNS:
        m = pat.search(cleaned)
        if not m:
            continue
        jockey = m.group("jockey").strip(" -–:")
        k = m.groupdict().get("k")
        if k is not None:
            return jockey, int(k)
        word = m.groupdict().get("word")
        if word:
            return jockey, _WORD_THRESHOLDS[word.lower()]
    return None


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

    # -- Phase 1: Megabet discovery -------------------------------------
    def fetch_megabets_raw(self) -> FetchResult:
        return self.client.get_json(_url("megabets"))

    @staticmethod
    def jockey_extras_events(payload: Any) -> list[dict[str, Any]]:
        """Event stubs from the Megabets listing that carry Jockey Megabets.

        Live schema (2026-08-22): the endpoint returns a list of event stubs;
        Jockey Megabet events have competitionName "Jockey Extras" and names
        like "Jockey Extras - Sandown".
        """
        stubs = []
        for node in _walk_dicts(payload):
            comp = node.get("competitionName")
            name = node.get("name")
            is_jockey = (
                isinstance(comp, str) and comp.strip().lower() == JOCKEY_EXTRAS_COMPETITION
            ) or (
                isinstance(name, str)
                and re.match(r"^\s*jockey\s+(extras|megabets?)\b", name, re.I)
            )
            if is_jockey and node.get("id") is not None:
                stubs.append(node)
        return stubs

    @staticmethod
    def _stub_meeting_name(stub: dict[str, Any]) -> str | None:
        name = stub.get("name")
        if isinstance(name, str) and " - " in name:
            return name.split(" - ", 1)[1].strip() or None
        return None

    def discover_jockey_megabets(self) -> list[MegabetOffer]:
        """Find Jockey Megabet offers: listing stubs -> per-event racecards."""
        result = self.fetch_megabets_raw()
        payload = result.json()
        stubs = self.jockey_extras_events(payload)
        log.info("sportsbet: %d Jockey Extras events in Megabets listing", len(stubs))

        offers: list[MegabetOffer] = []
        for stub in stubs:
            event_id = str(stub["id"])
            try:
                card = self.client.get_json(_url("racecard", event_id=event_id))
            except SourceUnavailableError as exc:
                log.error("sportsbet: Jockey Extras racecard %s failed: %s", event_id, exc)
                continue
            stub_offers = self.parse_megabets(card.json(), fetched_at=card.fetched_at)
            meeting_name = self._stub_meeting_name(stub)
            start = _to_dt(stub.get("startTime"))
            for o in stub_offers:
                o.meeting_name = o.meeting_name or meeting_name
                o.meeting_source_id = o.meeting_source_id or event_id
                o.meeting_date = o.meeting_date or (start.date() if start else None)
                o.raw_sha256 = card.sha256
            if not stub_offers:
                log.warning(
                    "sportsbet: Jockey Extras event %s (%s) yielded no parseable "
                    "markets — raw payload archived at %s",
                    event_id, stub.get("name"), card.archive_path,
                )
            offers.extend(stub_offers)

        # Fallback for the older layout where markets sat in the listing itself.
        inline = self.parse_megabets(payload, fetched_at=result.fetched_at)
        for o in inline:
            o.raw_sha256 = result.sha256
        offers.extend(inline)

        deduped: list[MegabetOffer] = []
        seen: set[tuple[str, str, int]] = set()
        for o in offers:
            key = (o.jockey_name.lower(), (o.meeting_name or "").lower(), o.threshold)
            if key not in seen:
                seen.add(key)
                deduped.append(o)
        log.info("sportsbet: %d jockey megabet offers discovered", len(deduped))
        return deduped

    def parse_megabets(self, payload: Any, fetched_at: datetime | None = None) -> list[MegabetOffer]:
        """Parse the Megabets payload into Jockey Megabet offers.

        Strategy: walk the whole document; any dict that has a market-like
        name plus selections (or is itself a priced selection under a named
        market) is considered. Non-jockey Megabets are skipped. This survives
        moderate re-nesting of the envelope, though field renames will still
        require adaptation (raw payload is archived for that).
        """
        fetched_at = fetched_at or datetime.now(timezone.utc)
        offers: list[MegabetOffer] = []
        seen: set[tuple[str, str, int]] = set()
        unparsed_names: list[str] = []

        for node in _walk_dicts(payload):
            name = _first(node, "name", "marketName", "eventName", "displayName")
            if not isinstance(name, str):
                continue
            selections = _first(node, "selections", "outcomes", "markets")
            parsed = parse_jockey_threshold(name)

            # Case A: the market name itself carries jockey + threshold and
            # the market has priced selections (typically a single "Yes").
            if parsed and isinstance(selections, list):
                jockey, k = parsed
                for sel in selections:
                    if not isinstance(sel, dict):
                        continue
                    price = _extract_price(sel)
                    if price is None:
                        continue
                    offers.append(
                        self._offer(node, sel, name, jockey, k, price, fetched_at)
                    )
                    break
                continue

            # Case B: a market groups selections whose *selection* names carry
            # jockey + threshold.
            if isinstance(selections, list):
                for sel in selections:
                    if not isinstance(sel, dict):
                        continue
                    sel_name = _first(sel, "name", "selectionName", "displayName")
                    if not isinstance(sel_name, str):
                        continue
                    sparsed = parse_jockey_threshold(sel_name)
                    if not sparsed:
                        if re.search(r"\bjockey\b", name, re.I):
                            unparsed_names.append(sel_name)
                        continue
                    price = _extract_price(sel)
                    if price is None:
                        continue
                    jockey, k = sparsed
                    offers.append(
                        self._offer(node, sel, sel_name, jockey, k, price, fetched_at)
                    )

        deduped: list[MegabetOffer] = []
        for o in offers:
            key = (o.jockey_name.lower(), (o.meeting_name or "").lower(), o.threshold)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(o)
        if unparsed_names:
            log.warning(
                "sportsbet: %d jockey-market selection names not parsed: %s",
                len(unparsed_names),
                "; ".join(sorted(set(unparsed_names))[:10]),
            )
        return deduped

    def _offer(
        self,
        market: dict[str, Any],
        selection: dict[str, Any],
        market_name: str,
        jockey: str,
        threshold: int,
        price: float,
        fetched_at: datetime,
    ) -> MegabetOffer:
        meeting_name = _first(market, "meetingName", "venueName", "competitionName", "venue")
        event_id = _first(market, "eventId", "raceEventId", "linkedEventId")
        raw_date = _first(
            market, "meetingDate", "eventDate", "startTime", "displayStartTime"
        )
        dt = _to_dt(raw_date)
        status = _first(market, "status", "marketStatus", default="open")
        return MegabetOffer(
            source=SOURCE,
            market_id=str(_first(market, "marketId", "id", default="")),
            selection_id=str(_first(selection, "id", "selectionId", default="")),
            meeting_name=str(meeting_name) if meeting_name else None,
            meeting_source_id=str(event_id) if event_id is not None else None,
            meeting_date=dt.date() if dt else None,
            jockey_name=jockey,
            threshold=threshold,
            odds=price,
            market_name=market_name,
            fetched_at=fetched_at,
            market_status=str(status).lower(),
        )

    # -- Phase 2/3: meetings and racecards ------------------------------
    def fetch_meetings(self, for_date: date) -> tuple[list[dict[str, Any]], FetchResult]:
        """Return raw meeting dicts for a date (thoroughbred only is filtered
        by callers; we keep raw dicts because race event ids live inside)."""
        result = self.client.get_json(_url("all_racing", date=for_date.isoformat()))
        payload = result.json()
        meetings: list[dict[str, Any]] = []
        for node in _walk_dicts(payload):
            # A meeting node references races and has a venue-ish name.
            races = _first(node, "races", "events")
            name = _first(node, "name", "venueName", "meetingName")
            if isinstance(races, list) and isinstance(name, str) and races:
                # Live schema: thoroughbred meetings carry className
                # "Horses - Aus/NZ" etc.; skip harness/greyhound meetings so
                # a shared venue name can't mix codes.
                cls = _first(node, "className", "raceType")
                if isinstance(cls, str) and not cls.lower().startswith("horse"):
                    continue
                if any(isinstance(r, dict) and _first(r, "id", "eventId") for r in races):
                    meetings.append(node)
        if not meetings:
            raise SchemaMismatchError(
                SOURCE,
                "no meeting nodes with races found in AllRacing payload",
                str(result.archive_path) if result.archive_path else None,
            )
        return meetings, result

    def fetch_racecard(self, event_id: str) -> RacecardInfo:
        result = self.client.get_json(_url("racecard", event_id=event_id))
        payload = result.json()
        card = self.parse_racecard(payload, event_id=event_id, fetched_at=result.fetched_at)
        card.raw_sha256 = result.sha256
        if not card.races or not any(r.runners for r in card.races):
            raise SchemaMismatchError(
                SOURCE,
                f"racecard for event {event_id} parsed with no runners",
                str(result.archive_path) if result.archive_path else None,
            )
        return card

    def parse_racecard(
        self, payload: Any, event_id: str, fetched_at: datetime | None = None
    ) -> RacecardInfo:
        """Parse one race event's racecard into runners with jockeys/prices."""
        fetched_at = fetched_at or datetime.now(timezone.utc)
        top = payload if isinstance(payload, dict) else {}
        venue = _first(top, "venueName", "meetingName", "competitionName", default="")
        if not venue:
            for node in _walk_dicts(payload):
                venue = _first(node, "venueName", "meetingName")
                if venue:
                    break
        start = _to_dt(_first(top, "startTime", "displayStartTime", "advertisedStartTime"))
        race_number = _first(top, "raceNumber", "eventNumber", "number")
        # Live schema: statusCode "A" (active) / "R" (resulted) plus a
        # bettingStatus word ("PRICED", "RESULTED", ...).
        status_raw = str(
            _first(top, "bettingStatus", "status", "eventStatus", "resultStatus", default="")
        ).lower()
        status_code = str(_first(top, "statusCode", default="")).strip().upper()
        if "abandon" in status_raw:
            status = "abandoned"
        elif status_code == "R" or any(
            w in status_raw for w in ("result", "settled", "final", "paid")
        ):
            status = "resulted"
        else:
            status = "open"

        runners: list[RunnerInfo] = []
        winners: list[str] = []
        seen_ids: set[str] = set()
        for node in _walk_dicts(payload):
            horse = _first(node, "runnerName", "horseName")
            if horse is None and "jockey" in {k.lower() for k in node}:
                horse = _first(node, "name")
            if not isinstance(horse, str) or not horse.strip():
                continue
            rid = str(_first(node, "id", "runnerId", "selectionId", default=horse))
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            jockey = _first(node, "jockeyName", "jockey", "riderName", "rider")
            if isinstance(jockey, dict):
                jockey = _first(jockey, "name", "fullName")
            saddle = _first(node, "runnerNumber", "saddlecloth", "number", "barrierNumber")
            price = _extract_price(node)
            rstatus = _runner_status(node)
            runners.append(
                RunnerInfo(
                    source=SOURCE,
                    source_id=rid,
                    horse_name=horse.strip(),
                    saddlecloth=int(saddle) if isinstance(saddle, (int, float)) else None,
                    jockey_name=str(jockey).strip() if jockey else None,
                    status=rstatus,
                    win_odds=price,
                    odds_timestamp=fetched_at if price else None,
                )
            )
            place = _first(node, "finishPlace", "placeNumber", "result", "finishingPosition")
            if place in (1, "1") and rstatus == "active":
                winners.append(horse.strip())

        # Live schema: resulted races carry result as placings by saddlecloth,
        # e.g. "1,16,18" — the first number is the winner.
        result_str = _first(top, "result")
        if not winners and isinstance(result_str, str) and result_str.strip():
            first = result_str.split(",")[0].strip()
            if first.isdigit():
                win_no = int(first)
                winner = next((r for r in runners if r.saddlecloth == win_no), None)
                if winner is not None:
                    winners.append(winner.horse_name)

        meeting = MeetingInfo(
            source=SOURCE,
            source_id=str(_first(top, "meetingId", "competitionId", default=event_id)),
            venue=str(venue),
            meeting_date=start.date() if start else None,
            jurisdiction=_first(top, "regionName", "countryCode", "stateCode"),
            race_type=_first(top, "raceType", "classType", "sportName"),
        )
        race = RaceInfo(
            source=SOURCE,
            source_id=str(event_id),
            race_number=int(race_number) if isinstance(race_number, (int, float)) else None,
            start_time=start,
            status=status,
            name=_first(top, "eventName", "raceName", "name"),
            runners=runners,
            winner_names=winners,
        )
        return RacecardInfo(meeting=meeting, races=[race], fetched_at=fetched_at)
