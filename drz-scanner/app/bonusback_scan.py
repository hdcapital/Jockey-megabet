"""``python -m app.bonusback_scan`` — which horse to back under Sportsbet's
"bonus bet back if 2nd or 3rd" promotion.

Sportsbet does not flag the promotion in its racecard API (none of the
payloads this project has captured carry it), so you name the promo races::

    python -m app.bonusback_scan --promo "Randwick:7" --promo "Flemington:4"
    python -m app.bonusback_scan --meeting Randwick           # every open race there
    python -m app.bonusback_scan --all                        # every open race today

For each race it prints every runner's value under the promotion and the one
runner to back, chosen by ``--objective`` (``ev`` by default; ``lock`` for a
fully hedged, risk-free version; ``growth`` for the Kelly view). See
``app/bonusback.py`` for the maths and ``BONUSBACK.md`` for the evidence.

Nothing here places a bet.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone

from rich.table import Table

from app.bonusback import (
    OBJECTIVES,
    BonusBackTerms,
    ExchangeRunner,
    PromoRace,
    PromoRaceError,
    bonus_bet_conversion,
    value_promo_race,
)
from app.calibration import load_calibration
from app.config import get_settings
from app.drz import BetfairSession, _match_quote, racing_today, select_stubs
from app.http import SourceUnavailableError
from app.logging_setup import setup_logging
from app.reporting.tables import DASH, _countdown, _local_hhmm, console
from app.sources.base import RaceStub, SchemaMismatchError
from app.sources.betfair import place_market_for, venues_match

log = logging.getLogger("app.bonusback_scan")


def build_parser() -> argparse.ArgumentParser:
    s = get_settings()
    p = argparse.ArgumentParser(
        prog="python -m app.bonusback_scan",
        description="Best horse for Sportsbet's bonus-back-if-2nd-or-3rd promotion (display only).",
    )
    p.add_argument("--date", type=date.fromisoformat, default=None)
    p.add_argument("--promo", action="append", default=[], metavar="VENUE:RACE",
                   help="a promo race, e.g. 'Randwick:7'. Repeatable")
    p.add_argument("--meeting", default=None, help="every open race at meetings matching this")
    p.add_argument("--race", type=int, default=None, help="with --meeting: only this race")
    p.add_argument("--all", action="store_true", help="every open race today")
    p.add_argument("--bonus-value", type=float, default=s.bonus_bet_value,
                   help="cash value of $1 of bonus bet (default %(default)s)")
    p.add_argument("--max-stake", type=float, default=s.bonus_max_stake,
                   help="largest stake the bonus is paid on (default %(default)s)")
    p.add_argument("--commission", type=float, default=s.betfair_commission,
                   help="Betfair commission (default %(default)s)")
    p.add_argument("--min-ev", type=float, default=s.bonus_min_ev,
                   help="smallest EV per $1 worth a pick (default %(default)s)")
    p.add_argument("--objective", choices=OBJECTIVES, default="ev")
    p.add_argument("--json", default=None, metavar="PATH", help="also write the results as JSON")
    p.add_argument("--no-betfair", action="store_true", help="Sportsbet prices only")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def parse_promo(spec: str) -> tuple[str, int]:
    venue, _, race = spec.rpartition(":")
    if not venue or not race.strip().isdigit():
        raise argparse.ArgumentTypeError(f"--promo wants VENUE:RACE, got {spec!r}")
    return venue.strip(), int(race)


def promo_stubs(stubs: list[RaceStub], args) -> list[RaceStub]:
    """The open races the promotion applies to."""
    open_stubs = select_stubs(stubs, argparse.Namespace(meeting=None, race=None))
    if args.all:
        return open_stubs
    out = []
    wanted = [parse_promo(s) for s in args.promo]
    for st in open_stubs:
        if args.meeting and args.meeting.lower() in st.meeting_name.lower():
            if args.race is None or st.race_number == args.race:
                out.append(st)
                continue
        if any(venues_match(v, st.meeting_name) and st.race_number == n for v, n in wanted):
            out.append(st)
    return out


def exchange_inputs(bf: BetfairSession | None, race, now: datetime):
    """Per-runner Betfair data for one race: ``(runners, delayed, place_winners)``."""
    if bf is None or not bf.markets or bf.stale(now):
        return None, False, None
    active = race.active_runners()
    win = next(
        (m for m in bf.markets if m.market_type == "WIN"
         and m.race_number == race.race_number and venues_match(m.venue, race.venue)),
        None,
    )
    if win is None:
        return None, False, None
    # Only a 3-winner place market covers exactly 1st-3rd, which is what the
    # full lock needs; anything else is ignored rather than approximated.
    place = place_market_for(bf.markets, race.venue, race.race_number, 3)
    out = []
    delayed = False
    for r in active:
        wq = _match_quote(win.runners, r)
        pq = _match_quote(place.runners, r) if place else None
        delayed = delayed or bool(wq and wq.delayed)
        out.append(ExchangeRunner(
            win_probability=wq.probability if wq else None,
            win_reliable=bool(wq and wq.reliable),
            win_lay=wq.best_lay if wq else None,
            win_lay_size=wq.lay_volume if wq else None,
            place_lay=pq.best_lay if pq else None,
            place_lay_size=pq.lay_volume if pq else None,
        ))
    return out, delayed, (place.number_of_winners if place else None)


def _f(v, fmt: str) -> str:
    return DASH if v is None else format(v, fmt)


def render(pr: PromoRace, terms: BonusBackTerms, objective: str) -> None:
    now = datetime.now(timezone.utc)
    secs = (pr.start_time - now).total_seconds() if pr.start_time else None
    title = (
        f"{pr.venue} R{pr.race_number} · {_local_hhmm(pr.start_time)} · jump in "
        f"{_countdown(secs)} · {pr.n_runners} runners · probs: {pr.win_model}"
        + (f" · SB book {pr.sportsbet_overround:.1%}" if pr.sportsbet_overround else "")
        + (" · [yellow]betfair_delayed[/yellow]" if pr.betfair_delayed else "")
    )
    t = Table(title=title, title_justify="left", header_style="bold", pad_edge=False)
    for name, just in (("#", "right"), ("Runner", "left"), ("SB win", "right"),
                       ("P(win)", "right"), ("P(2/3)", "right"), ("EV no promo", "right"),
                       ("+promo", "right"), ("EV", "right"), ("EV cons", "right"),
                       ("r*", "right"), ("Lock", "right"), ("Stake", "right"),
                       ("Note", "left")):
        t.add_column(name, justify=just, no_wrap=True,
                     overflow="ellipsis", max_width=30 if name == "Note" else None)
    pick = pr.pick.best
    rows = sorted(pr.valuations, key=lambda v: -v.ev_back_conservative)
    for v in rows:
        x = v.inputs
        style = "bold green" if v is pick else ("dim" if not v.eligible else None)
        t.add_row(
            str(x.saddlecloth or ""), x.horse_name[:20], f"{x.win_price:.2f}",
            f"{x.p_win:.1%}", f"{x.p23:.1%}", f"{v.ev_no_promo:+.3f}",
            f"{v.promo_value:+.3f}", f"{v.ev_back:+.3f}", f"{v.ev_back_conservative:+.3f}",
            _f(v.breakeven_r, ".2f"), _f(v.lock_profit, "+.3f"),
            f"${v.stake:.2f}" if v.stake else DASH, "; ".join(v.notes),
            style=style,
        )
    console.print(t)
    if pick is None:
        console.print(f"  [yellow]No pick:[/yellow] {pr.pick.reason}\n")
        return
    x = pick.inputs
    stake = terms.max_stake
    msg = (
        f"  [bold green]PICK ({objective}):[/bold green] #{x.saddlecloth} {x.horse_name} "
        f"@ {x.win_price:.2f} — EV {pick.ev_back_conservative:+.3f}/$1, "
        f"≈ ${pick.ev_back_conservative * stake:+.2f} on a ${stake:.0f} promo bet "
        f"(needs bonus conversion ≥ {_f(pick.breakeven_r, '.2f')})"
    )
    console.print(msg)
    if pick.lock_profit is not None:
        console.print(
            f"  hedged: lay win ${pick.lock_win_lay_stake * stake:.2f} @ {x.bf_win_lay:.2f} + "
            f"lay place ${pick.lock_place_lay_stake * stake:.2f} @ {x.bf_place_lay:.2f} "
            f"locks ${pick.lock_profit * stake:+.2f} (given r={terms.bonus_value:.2f})"
        )
    elif pick.lay_win_stake is not None:
        console.print(
            f"  lay-win only: lay ${pick.lay_win_stake * stake:.2f} @ {x.bf_win_lay:.2f}; "
            f"qualifying {pick.lay_win_qualifying * stake:+.2f}, "
            f"+${terms.bonus_value * stake:.2f} bonus value if 2nd/3rd"
        )
    console.print()


def best_conversion(races: list[PromoRace], commission: float):
    """The best bonus-bet conversion visible on the races scanned, as a sanity
    check on ``--bonus-value``: back at Sportsbet, lay at Betfair."""
    best = None
    for pr in races:
        for v in pr.valuations:
            x = v.inputs
            if x.bf_win_lay and x.bf_win_lay > 1.0 and v.eligible:
                c = bonus_bet_conversion(x.win_price, x.bf_win_lay, commission)
                if best is None or c > best[0]:
                    best = (c, f"{pr.venue} R{pr.race_number} {x.horse_name} "
                               f"{x.win_price:.2f}/{x.bf_win_lay:.2f}")
    return best


def to_json(pr: PromoRace) -> dict:
    return {
        "venue": pr.venue, "race_number": pr.race_number,
        "start_time": pr.start_time.isoformat() if pr.start_time else None,
        "win_model": pr.win_model, "objective": pr.pick.objective,
        "pick": pr.pick.best.horse_name if pr.pick.best else None,
        "reason": pr.pick.reason,
        "runners": [
            {
                "saddlecloth": v.inputs.saddlecloth, "horse": v.inputs.horse_name,
                "win_price": v.inputs.win_price, "p_win": v.inputs.p_win,
                "p_second": v.inputs.p_second, "p_third": v.inputs.p_third,
                "p23": v.inputs.p23, "ev_no_promo": v.ev_no_promo,
                "ev": v.ev_back, "ev_conservative": v.ev_back_conservative,
                "breakeven_r": v.breakeven_r, "lock_profit": v.lock_profit,
                "stake": v.stake, "eligible": v.eligible, "notes": v.notes,
            }
            for v in pr.valuations
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.all or args.promo or args.meeting):
        console.print("Name the promo races: --promo VENUE:RACE (repeatable), --meeting, or --all.")
        return 2
    setup_logging(logging.DEBUG if args.verbose else logging.WARNING)
    settings = get_settings()
    calibration = load_calibration()
    terms = BonusBackTerms(
        bonus_value=args.bonus_value, max_stake=args.max_stake,
        commission=args.commission, min_ev=args.min_ev,
    )
    for_date = args.date or racing_today()
    bf = None if args.no_betfair else BetfairSession(for_date, settings)
    if bf is not None and not bf.configured:
        console.print(f"[yellow]Betfair unavailable:[/yellow] {bf.reason_unavailable}. "
                      f"Sportsbet de-vig probabilities only; no hedges.")
        bf = None

    from app.sources.sportsbet import SportsbetClient

    results: list[PromoRace] = []
    try:
        with SportsbetClient() as sb:
            _m, stubs, _raw = sb.fetch_schedule(for_date)
            chosen = promo_stubs(stubs, args)
            if not chosen:
                console.print("No open race matches the promo races given.")
                return 1
            if bf is not None:
                bf.refresh()
            for st in chosen:
                try:
                    race = sb.fetch_racecard(st.event_id)
                except (SourceUnavailableError, SchemaMismatchError) as exc:
                    console.print(f"[red]{st.meeting_name} R{st.race_number}: {exc}[/red]")
                    continue
                race.venue = race.venue or st.meeting_name
                race.race_number = race.race_number or st.race_number
                race.start_time = race.start_time or st.start_time
                if race.status != "open":
                    continue
                ex, delayed, winners = exchange_inputs(bf, race, datetime.now(timezone.utc))
                try:
                    pr = value_promo_race(
                        race, calibration, settings, terms, args.objective,
                        exchange=ex, betfair_delayed=delayed, place_market_winners=winners,
                    )
                except PromoRaceError as exc:
                    console.print(f"[yellow]{exc}[/yellow]")
                    continue
                results.append(pr)
                render(pr, terms, args.objective)
    except (SourceUnavailableError, SchemaMismatchError) as exc:
        console.print(f"[bold red]Sportsbet unavailable:[/bold red] {exc}")
        return 2
    finally:
        if bf is not None:
            bf.close()

    conv = best_conversion(results, terms.commission)
    if conv:
        console.print(f"[dim]Best bonus-bet conversion visible in these races: {conv[0]:.2f} "
                      f"({conv[1]}); you are assuming r={terms.bonus_value:.2f}.[/dim]")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([to_json(r) for r in results], fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
