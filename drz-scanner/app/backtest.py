"""``python -m app.backtest`` — settle stored signals and report what happened.

Everything here works on observations this scanner captured live. There is no
backfill path and there will not be one: a price you did not see at the time
is not evidence about a signal you would have taken at the time.

Reports
-------
* settlement from stored placings, with dead heats divided;
* calibration of P(place) by probability bucket;
* ROI by tier, win model, field size and win-price band;
* closing-line value — the place price at the signal against the last price
  captured before the jump.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from app.config import get_settings
from app.database import models as m
from app.database.repository import Repository, session_factory
from app.logging_setup import setup_logging

log = logging.getLogger("app.backtest")
console = Console()
DASH = "—"


@dataclass
class Settlement:
    placed: bool
    dead_heat_divisor: float
    gross_return: float
    deduction_status: str


def settle_runner(
    saddlecloth: int | None, placings: list[int], places: int, place_price: float | None
) -> Settlement | None:
    """Settle one runner against a race's stored placings.

    ``placings`` is the source's own finishing order by saddlecloth. When it
    lists exactly ``places`` runners there was no dead heat. When it lists
    *more*, the surplus runners dead-heated for the final paying position:
    three dividends listing four numbers means two runners tied for third.
    Australian practice divides the dividend among the runners tying for a
    position, so only those runners carry a divisor — the outright first and
    second are paid in full.

    Deductions are recorded as ``unknown`` unless one was captured: a late
    scratching after a fixed-odds bet is struck reduces the payout under a
    rule this build has never seen a payload for, and inventing the reduction
    would corrupt the very ROI it feeds.
    """
    if saddlecloth is None or not placings or place_price is None:
        return None
    if len(placings) <= places:
        paying = placings[:places]
        placed = saddlecloth in paying
        return Settlement(
            placed=placed,
            dead_heat_divisor=1.0,
            gross_return=place_price if placed else 0.0,
            deduction_status="unknown",
        )
    outright = placings[: places - 1]
    tied = placings[places - 1 :]
    tied_count = float(len(tied))
    if saddlecloth in outright:
        return Settlement(True, 1.0, place_price, "unknown")
    if saddlecloth in tied:
        return Settlement(True, tied_count, place_price / tied_count, "unknown")
    return Settlement(False, tied_count, 0.0, "unknown")


def settle_all(session) -> int:
    """Settle every unsettled valuation whose race has stored placings."""
    repo = Repository(session)
    n = 0
    for v in repo.unsettled_valuations():
        race = repo.race_by_id(v.race_id)
        runner = repo.runner_by_id(v.runner_id)
        if race is None or runner is None or not race.result_placings:
            continue
        placings = [int(x) for x in race.result_placings.split(",") if x.strip().isdigit()]
        s = settle_runner(runner.saddlecloth, placings, v.places, v.place_price)
        if s is None:
            continue
        v.settled = True
        v.placed = s.placed
        v.settled_return = s.gross_return
        v.dead_heat_divisor = s.dead_heat_divisor
        v.deduction_status = s.deduction_status
        n += 1
    session.commit()
    return n


def closing_line_value(session, valuation: m.PlaceValuation) -> float | None:
    """Signal place price against the last place price captured before the jump."""
    last = session.scalar(
        select(m.RunnerPrice)
        .where(
            m.RunnerPrice.runner_id == valuation.runner_id,
            m.RunnerPrice.place_price.isnot(None),
        )
        .order_by(m.RunnerPrice.observed_at.desc())
        .limit(1)
    )
    if last is None or not last.place_price or not valuation.place_price:
        return None
    return valuation.place_price / last.place_price - 1.0


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _roi_row(rows: list[m.PlaceValuation]) -> tuple[int, float, float]:
    n = len(rows)
    staked = float(n)
    returned = sum(r.settled_return or 0.0 for r in rows)
    return n, returned / staked - 1.0 if staked else 0.0, sum(
        1 for r in rows if r.placed
    ) / n if n else 0.0


def _group_report(title: str, groups: dict, console: Console) -> None:
    table = Table(title=title, title_justify="left", header_style="bold")
    table.add_column("Group")
    table.add_column("n", justify="right")
    table.add_column("strike", justify="right")
    table.add_column("ROI (flat $1)", justify="right")
    for key in sorted(groups, key=lambda k: str(k)):
        rows = groups[key]
        n, roi, strike = _roi_row(rows)
        table.add_row(str(key), str(n), f"{strike:.1%}", f"{roi:+.1%}")
    console.print(table)
    console.print()


def calibration_report(rows: list[m.PlaceValuation]) -> None:
    edges = [0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0]
    table = Table(title="P(place) calibration (settled rows)",
                  title_justify="left", header_style="bold")
    table.add_column("bucket")
    table.add_column("n", justify="right")
    table.add_column("predicted", justify="right")
    table.add_column("actual", justify="right")
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [r for r in rows if r.p_place is not None and lo <= r.p_place < hi]
        if not sel:
            continue
        pred = sum(r.p_place for r in sel) / len(sel)
        act = sum(1 for r in sel if r.placed) / len(sel)
        table.add_row(f"[{lo:.2f},{hi:.2f})", str(len(sel)), f"{pred:.3f}", f"{act:.3f}")
    console.print(table)
    console.print()


def _field_bucket(n: int) -> str:
    if n <= 7:
        return "5-7 (2 places)"
    if n <= 9:
        return "8-9"
    if n <= 12:
        return "10-12"
    return "13+"


def _price_bucket(p: float | None) -> str:
    if p is None:
        return DASH
    for hi, label in ((3.0, "<3.0"), (5.0, "3.0-5.0"), (9.0, "5.0-9.0")):
        if p < hi:
            return label
    return ">9.0"


def run_report(session, min_tier: str | None = None) -> None:
    settings = get_settings()
    rows = list(
        session.scalars(select(m.PlaceValuation).where(m.PlaceValuation.settled.is_(True)))
    )
    if min_tier:
        rows = [r for r in rows if r.tier == min_tier]
    if not rows:
        console.print(
            "[yellow]No settled observations yet.[/yellow] The backtester reports "
            "only what this scanner captured live; there is no backfill path."
        )
        return

    bets = [r for r in rows if r.tier == "BET"]
    console.print(
        f"[bold]{len(rows)} settled observations[/bold], {len(bets)} of them BET signals."
    )
    if len(bets) < settings.proven_signal_threshold:
        console.print(
            f"[red]BET signals remain UNPROVEN:[/red] {len(bets)} settled, "
            f"{settings.proven_signal_threshold} needed before ROI below is "
            f"treated as evidence rather than as noise."
        )
    console.print()

    _group_report("ROI by tier", _by(rows, lambda r: r.tier), console)
    _group_report("ROI by win model", _by(rows, lambda r: r.win_model), console)
    _group_report("ROI by field size", _by(rows, lambda r: _field_bucket(r.active_runner_count)), console)
    _group_report("ROI by win-price band", _by(rows, lambda r: _price_bucket(r.win_price)), console)
    calibration_report(rows)

    clv = [(r, closing_line_value(session, r)) for r in bets]
    clv = [(r, c) for r, c in clv if c is not None]
    if clv:
        mean = sum(c for _, c in clv) / len(clv)
        beat = sum(1 for _, c in clv if c > 0) / len(clv)
        console.print(
            f"[bold]Closing-line value[/bold] on {len(clv)} BET signals: "
            f"mean {mean:+.2%}, beat the close {beat:.0%} of the time."
        )
    else:
        console.print("[dim]Closing-line value needs at least two captures per runner.[/dim]")

    dead_heats = [r for r in rows if (r.dead_heat_divisor or 1.0) != 1.0]
    if dead_heats:
        console.print(f"[dim]{len(dead_heats)} row(s) settled through a dead heat.[/dim]")
    unknown_ded = sum(1 for r in rows if r.deduction_status == "unknown")
    if unknown_ded:
        console.print(
            f"[dim]{unknown_ded} row(s) carry deduction_status=unknown: late-scratching "
            f"deductions were not captured, so ROI here is an upper bound.[/dim]"
        )


def _by(rows, key):
    out = defaultdict(list)
    for r in rows:
        out[key(r)].append(r)
    return dict(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.backtest",
        description="Settle stored signals and report ROI, calibration and CLV.",
    )
    p.add_argument("--settle-only", action="store_true", help="settle, do not report")
    p.add_argument("--tier", default=None, help="restrict the report to one tier")
    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    Session = session_factory()
    with Session() as session:
        n = settle_all(session)
        console.print(f"[dim]settled {n} new observation(s)[/dim]\n")
        if not args.settle_only:
            run_report(session, args.tier)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
