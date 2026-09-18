"""Betfair Australia historical prices: download, cache, validate, shape.

Source: the free Betfair Data Scientists ANZ thoroughbred files —
yearly zips ``ANZ_Thoroughbreds_{YYYY}.zip`` and, for the current year,
monthly ``ANZ_Thoroughbreds_{YYYY}_{MM}.csv``. Everything is cached under
``data/history/`` so a refit does not re-download 80 MB.

The header is validated against :data:`REQUIRED_COLUMNS` on every read. If
the publisher renames a column, the loader fails loudly naming the missing
field rather than quietly fitting on the wrong numbers.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import get_settings
from app.http import ArchivingClient, SourceUnavailableError

log = logging.getLogger(__name__)

SOURCE = "betfair_history"

#: Columns the fit depends on. Extra columns are ignored; a missing one is fatal.
REQUIRED_COLUMNS = (
    "LOCAL_MEETING_DATE",
    "TRACK",
    "STATE_CODE",
    "RACE_NO",
    "WIN_MARKET_ID",
    "TAB_NUMBER",
    "SELECTION_NAME",
    "WIN_RESULT",
    "WIN_BSP",
    "PLACE_RESULT",
    "PLACE_BSP",
)

WINNER_TOKEN = "WINNER"


class HistorySchemaError(Exception):
    """The published CSV no longer has the columns the fit depends on."""


class HistoryUnavailableError(Exception):
    """No cached history and the publisher could not be reached."""


def _cache_dir() -> Path:
    d = Path(get_settings().history_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def download(filename: str, client: ArchivingClient | None = None) -> Path:
    """Fetch one history file into the cache, primary URL then mirror."""
    dest = _cache_dir() / filename
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    s = get_settings()
    owned = client is None
    client = client or ArchivingClient(SOURCE, archive=False)
    errors: list[str] = []
    try:
        for base in (s.history_base_url, s.history_mirror_url):
            url = f"{base}/{filename}"
            try:
                result = client.get_json(url)
            except SourceUnavailableError as exc:
                errors.append(f"{url}: {exc.detail}")
                continue
            dest.write_bytes(result.body)
            log.info("history: cached %s (%d bytes) from %s", filename, len(result.body), base)
            return dest
    finally:
        if owned:
            client.close()
    raise HistoryUnavailableError(
        f"could not download {filename}; tried:\n  " + "\n  ".join(errors)
    )


def available_filenames(start: date, end: date) -> list[str]:
    """Yearly zips for complete years, monthly CSVs for the current one."""
    names: list[str] = []
    today = date.today()
    for year in range(start.year, end.year + 1):
        if year < today.year:
            names.append(f"ANZ_Thoroughbreds_{year}.zip")
        else:
            last_month = end.month if end.year == year else 12
            for month in range(1, last_month + 1):
                names.append(f"ANZ_Thoroughbreds_{year}_{month:02d}.csv")
    return names


def _validate_header(columns, label: str) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise HistorySchemaError(
            f"{label}: the Betfair history file no longer carries "
            f"{missing}. Columns present: {sorted(columns)[:40]}. "
            f"Refusing to fit on a schema this code has not been checked against."
        )


def _read_csv(buf, label: str) -> pd.DataFrame:
    df = pd.read_csv(buf, low_memory=False)
    _validate_header(df.columns, label)
    return df[list(REQUIRED_COLUMNS)]


def read_files(paths: list[Path]) -> pd.DataFrame:
    """Concatenate cached zips/CSVs, validating each header."""
    frames: list[pd.DataFrame] = []
    for path in paths:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                for name in sorted(z.namelist()):
                    if name.lower().endswith(".csv"):
                        with z.open(name) as fh:
                            frames.append(_read_csv(io.BytesIO(fh.read()), f"{path.name}:{name}"))
        else:
            frames.append(_read_csv(path, path.name))
    if not frames:
        raise HistoryUnavailableError("no history files to read")
    return pd.concat(frames, ignore_index=True)


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_meeting_dates(values: pd.Series) -> pd.Series:
    """Parse LOCAL_MEETING_DATE, which mixes ISO and day-first formats.

    Most files write ``2025-12-01``; a small number of months write
    ``13/04/2025``. Day-first is established by the data itself (values with a
    first field above 12 exist, values with a second field above 12 do not).
    Anything matching neither shape becomes NaT and is dropped by the caller,
    never silently reinterpreted.
    """
    s = values.astype(str).str.strip()
    is_iso = s.str.match(_ISO)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    out[is_iso] = pd.to_datetime(s[is_iso].str.slice(0, 10), format="%Y-%m-%d", errors="coerce")
    out[~is_iso] = pd.to_datetime(s[~is_iso], dayfirst=True, errors="coerce")
    return out


@dataclass
class RaceArrays:
    """Padded per-race arrays, the shape every fit and evaluation works on."""

    q: np.ndarray          # (R, N) normalised BSP win probabilities
    mask: np.ndarray       # (R, N) True where a real runner sits
    won: np.ndarray        # (R, N)
    placed: np.ndarray     # (R, N)
    place_bsp: np.ndarray  # (R, N), NaN where absent
    names: np.ndarray      # (R, N) selection names, "" where padded
    tab_numbers: np.ndarray  # (R, N) TAB numbers, -1 where padded
    n_runners: np.ndarray  # (R,)
    places: np.ndarray     # (R,) 2 or 3
    dates: np.ndarray      # (R,) datetime64[D]
    keys: pd.DataFrame     # (R, ...) date/track/race_no/market id, for joins

    @property
    def n_races(self) -> int:
        return self.q.shape[0]


RACE_KEY = ["meeting_date", "TRACK", "RACE_NO", "WIN_MARKET_ID"]


def prepare(df: pd.DataFrame, min_runners: int = 5) -> RaceArrays:
    """Apply the documented filters and build padded arrays.

    Filters, in order: drop New Zealand; require every runner to carry
    ``WIN_BSP > 1``; require exactly one winner; require 2 or 3 placed
    runners; require the winner to be among them; require at least
    ``min_runners`` runners. The number of places for a race is the number of
    placed runners in it — taken from the data, not assumed from field size.
    """
    df = df.copy()
    df["meeting_date"] = parse_meeting_dates(df["LOCAL_MEETING_DATE"])
    df = df[df["meeting_date"].notna()]
    df = df[df["STATE_CODE"].astype(str).str.strip().str.upper() != "NZ"]
    df["WIN_BSP"] = pd.to_numeric(df["WIN_BSP"], errors="coerce")
    df["PLACE_BSP"] = pd.to_numeric(df["PLACE_BSP"], errors="coerce")
    df["won"] = df["WIN_RESULT"].astype(str).str.strip().str.upper() == WINNER_TOKEN
    df["placed"] = df["PLACE_RESULT"].astype(str).str.strip().str.upper() == WINNER_TOKEN
    df = df.sort_values(RACE_KEY + ["TAB_NUMBER"])

    grouped = df.groupby(RACE_KEY, sort=False)
    stats = grouped.agg(
        n=("WIN_BSP", "size"),
        n_priced=("WIN_BSP", lambda s: int((s > 1).sum())),
        n_win=("won", "sum"),
        n_place=("placed", "sum"),
        n_win_placed=("won", "size"),
    )
    winner_placed = grouped.apply(
        lambda d: bool((d["won"] & d["placed"]).any()), include_groups=False
    )
    keep = stats[
        (stats.n >= min_runners)
        & (stats.n_priced == stats.n)
        & (stats.n_win == 1)
        & (stats.n_place.isin([2, 3]))
    ].index
    keep = keep.intersection(winner_placed[winner_placed].index)
    log.info("history: %d races in file, %d pass the filters", len(stats), len(keep))

    df = df.set_index(RACE_KEY)
    df = df.loc[df.index.isin(keep)].reset_index()
    grouped = df.groupby(RACE_KEY, sort=False)
    sizes = grouped.size().to_numpy()
    if sizes.size == 0:
        raise HistoryUnavailableError("no races survived the filters")
    max_n = int(sizes.max())
    n_races = sizes.size

    row = np.repeat(np.arange(n_races), sizes)
    col = np.concatenate([np.arange(s) for s in sizes])
    q = np.zeros((n_races, max_n))
    won = np.zeros((n_races, max_n), dtype=bool)
    placed = np.zeros((n_races, max_n), dtype=bool)
    place_bsp = np.full((n_races, max_n), np.nan)
    names = np.full((n_races, max_n), "", dtype=object)
    tabs = np.full((n_races, max_n), -1, dtype=int)
    names[row, col] = df["SELECTION_NAME"].astype(str).to_numpy()
    tabs[row, col] = pd.to_numeric(df["TAB_NUMBER"], errors="coerce").fillna(-1).to_numpy()
    q[row, col] = 1.0 / df["WIN_BSP"].to_numpy()
    won[row, col] = df["won"].to_numpy()
    placed[row, col] = df["placed"].to_numpy()
    place_bsp[row, col] = df["PLACE_BSP"].to_numpy()
    q = q / q.sum(axis=1, keepdims=True)
    mask = np.arange(max_n)[None, :] < sizes[:, None]

    keys = grouped[["meeting_date", "TRACK", "RACE_NO", "WIN_MARKET_ID"]].first()
    keys = keys.reset_index(drop=True)
    dates = pd.to_datetime(keys["meeting_date"]).to_numpy().astype("datetime64[D]")
    return RaceArrays(
        q=q,
        mask=mask,
        won=won,
        placed=placed,
        place_bsp=place_bsp,
        names=names,
        tab_numbers=tabs,
        n_runners=sizes,
        places=placed.sum(axis=1),
        dates=dates,
        keys=keys,
    )


def load_history(
    start: date, end: date, download_missing: bool = True, min_runners: int = 5
) -> RaceArrays:
    """Cached history for a date window, ready to fit on."""
    names = available_filenames(start, end)
    paths: list[Path] = []
    missing: list[str] = []
    for name in names:
        path = _cache_dir() / name
        if path.exists() and path.stat().st_size > 0:
            paths.append(path)
        elif download_missing:
            try:
                paths.append(download(name))
            except HistoryUnavailableError as exc:
                missing.append(str(exc).splitlines()[0])
        else:
            missing.append(name)
    if not paths:
        raise HistoryUnavailableError(
            "no history available. Tried:\n  " + "\n  ".join(missing or names)
        )
    if missing:
        log.warning("history: %d file(s) unavailable: %s", len(missing), missing[:3])
    arrays = prepare(read_files(paths), min_runners=min_runners)
    lo = np.datetime64(start)
    hi = np.datetime64(end)
    sel = (arrays.dates >= lo) & (arrays.dates <= hi)
    return RaceArrays(
        q=arrays.q[sel],
        mask=arrays.mask[sel],
        won=arrays.won[sel],
        placed=arrays.placed[sel],
        place_bsp=arrays.place_bsp[sel],
        names=arrays.names[sel],
        tab_numbers=arrays.tab_numbers[sel],
        n_runners=arrays.n_runners[sel],
        places=arrays.places[sel],
        dates=arrays.dates[sel],
        keys=arrays.keys[sel].reset_index(drop=True),
    )
