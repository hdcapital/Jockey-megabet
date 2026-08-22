"""Backtest stored Megabet observations against stored race results.

Run:  python -m app.backtest [--min-ev 0.0] [--model consensus]
                             [--quality HIGH MEDIUM] [--by threshold jockey ...]

Only observations actually captured by the scanner are used; missing
historical prices are never backfilled or invented. A Megabet observation is
settleable only when every race of its meeting/date has a stored result, so
the jockey's actual win count is known exactly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from app.database import models as m
from app.database.repository import session_factory
from app.logging_setup import setup_logging
from app.matching.names import jockey_names_match

log = logging.getLogger(__name__)

PROB_BUCKETS = [(0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.30),
                (0.30, 0.40), (0.40, 1.0001)]


def bucket_label(p: float) -> str:
    for lo, hi in PROB_BUCKETS:
        if lo <= p < hi:
            return f"{lo:.0%}-{hi:.0%}" if hi <= 1 else f"{lo:.0%}+"
    return "?"


@dataclass
class Settled:
    valuation: m.ModelValuation
    megabet: m.MegabetMarket
    actual_wins: int
    won: bool


def settle_observations(session, model: str) -> tuple[list[Settled], int]:
    """Join latest valuation per megabet/model with actual jockey win counts.

    Returns (settled, unsettleable_count).
    """
    vals = session.scalars(
        select(m.ModelValuation)
        .where(m.ModelValuation.probability_model == model)
        .order_by(m.ModelValuation.megabet_id, m.ModelValuation.timestamp)
    ).all()
    # Keep the last observation per megabet (closing observation).
    latest: dict[int, m.ModelValuation] = {}
    for v in vals:
        latest[v.megabet_id] = v

    megabets = {
        mb.megabet_id: mb
        for mb in session.scalars(select(m.MegabetMarket)).all()
    }
    results = session.scalars(select(m.Result)).all()
    races = {r.race_id: r for r in session.scalars(select(m.Race)).all()}
    meetings = {mt.meeting_id: mt for mt in session.scalars(select(m.Meeting)).all()}

    # Group results by (venue-normalised meeting, date) via race -> meeting.
    from app.matching.names import normalize_name

    wins_by_meeting: dict[tuple[str, object], list[str]] = defaultdict(list)
    races_by_meeting: dict[tuple[str, object], int] = defaultdict(int)
    resulted_by_meeting: dict[tuple[str, object], int] = defaultdict(int)
    for race in races.values():
        meeting = meetings.get(race.meeting_id)
        if meeting is None:
            continue
        key = (normalize_name(meeting.venue), meeting.meeting_date)
        if race.status != "abandoned":
            races_by_meeting[key] += 1
    for res in results:
        race = races.get(res.race_id)
        if race is None:
            continue
        meeting = meetings.get(race.meeting_id)
        if meeting is None:
            continue
        key = (normalize_name(meeting.venue), meeting.meeting_date)
        resulted_by_meeting[key] += 1
        if res.winning_jockey:
            wins_by_meeting[key].append(res.winning_jockey)

    settled: list[Settled] = []
    unsettleable = 0
    for mb_id, v in latest.items():
        mb = megabets.get(mb_id)
        if mb is None or v.fair_probability is None:
            unsettleable += 1
            continue
        key = (normalize_name(mb.meeting_name or ""), mb.meeting_date)
        total, resulted = races_by_meeting.get(key, 0), resulted_by_meeting.get(key, 0)
        if total == 0 or resulted < total:
            unsettleable += 1  # incomplete results — never guess an outcome
            continue
        actual = sum(
            1 for wj in wins_by_meeting.get(key, [])
            if jockey_names_match(mb.jockey, wj)
        )
        settled.append(Settled(v, mb, actual, actual >= mb.threshold))
    return settled, unsettleable


def report(settled: list[Settled], unsettleable: int, min_ev: float,
           group_by: list[str]) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(f"Observed megabets (latest obs per market): {len(settled) + unsettleable}")
    console.print(f"Settleable against stored results: {len(settled)}")
    console.print(f"Unsettleable (incomplete stored results): {unsettleable}")
    if not settled:
        console.print("[yellow]Nothing to backtest yet — capture live scans "
                      "and results first.[/yellow]")
        return

    bets = [s for s in settled
            if s.valuation.expected_return is not None
            and s.valuation.expected_return >= min_ev]
    console.print(f"Theoretical bets at EV >= {min_ev:+.1%}: {len(bets)}")
    if bets:
        stake = float(len(bets))
        pnl = sum(
            (s.valuation.sportsbet_odds - 1.0) if s.won else -1.0
            for s in bets if s.valuation.sportsbet_odds
        )
        avg_ev = sum(s.valuation.expected_return for s in bets) / len(bets)
        wins = sum(1 for s in bets if s.won)
        console.print(f"Average model-implied EV: {avg_ev:+.1%}")
        console.print(f"Actual win rate: {wins}/{len(bets)} = {wins/len(bets):.1%}")
        console.print(f"Theoretical turnover (1u level stakes): {stake:.0f}u")
        console.print(f"Theoretical P&L: {pnl:+.2f}u  |  ROI: {pnl/stake:+.1%}")

    # Calibration by fair-probability bucket (all settleable observations).
    cal = Table(title="Calibration — Megabet fair probability vs outcome")
    for c in ("Bucket", "N", "Avg predicted", "Actual rate"):
        cal.add_column(c, justify="right")
    by_bucket: dict[str, list[Settled]] = defaultdict(list)
    for s in settled:
        by_bucket[bucket_label(s.valuation.fair_probability)].append(s)
    for label in sorted(by_bucket):
        grp = by_bucket[label]
        pred = sum(s.valuation.fair_probability for s in grp) / len(grp)
        act = sum(1 for s in grp if s.won) / len(grp)
        cal.add_row(label, str(len(grp)), f"{pred:.1%}", f"{act:.1%}")
    console.print(cal)

    for dim in group_by:
        table = Table(title=f"Performance by {dim}")
        for c in (dim, "N", "Avg EV", "Win rate", "P&L (1u)"):
            table.add_column(c, justify="right")
        groups: dict[str, list[Settled]] = defaultdict(list)
        for s in settled:
            if dim == "threshold":
                key = f"{s.megabet.threshold}+"
            elif dim == "jockey":
                key = s.megabet.jockey
            elif dim == "meeting":
                key = s.megabet.meeting_name or "?"
            elif dim == "rides":
                key = str(s.valuation.number_of_rides)
            elif dim == "ev":
                key = bucket_label(max(0.0, min(0.99, (s.valuation.expected_return or 0))))
            else:
                key = "?"
            groups[key].append(s)
        for key in sorted(groups):
            grp = groups[key]
            evs = [s.valuation.expected_return for s in grp
                   if s.valuation.expected_return is not None]
            pnl = sum((s.valuation.sportsbet_odds - 1.0) if s.won else -1.0
                      for s in grp if s.valuation.sportsbet_odds)
            table.add_row(
                key, str(len(grp)),
                f"{sum(evs)/len(evs):+.1%}" if evs else "—",
                f"{sum(1 for s in grp if s.won)/len(grp):.1%}",
                f"{pnl:+.2f}",
            )
        console.print(table)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backtest")
    parser.add_argument("--model", default="consensus",
                        choices=["consensus", "sportsbet_novig", "betfair"])
    parser.add_argument("--min-ev", type=float, default=0.0)
    parser.add_argument("--quality", nargs="*", default=["HIGH", "MEDIUM"])
    parser.add_argument("--by", nargs="*", dest="group_by",
                        default=["threshold", "rides"],
                        choices=["threshold", "jockey", "meeting", "rides", "ev"])
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)

    Session = session_factory()
    with Session() as session:
        settled, unsettleable = settle_observations(session, args.model)
        settled = [s for s in settled if s.valuation.quality in args.quality]
    report(settled, unsettleable, args.min_ev, args.group_by)
    return 0


if __name__ == "__main__":
    sys.exit(main())
