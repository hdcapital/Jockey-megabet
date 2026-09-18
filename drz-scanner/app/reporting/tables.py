"""Terminal output.

Every number shown comes from retrieved data or a calculation over retrieved
data. Anything unavailable renders as an em dash — never as a zero, and never
as a plausible-looking default.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.calibration import Calibration
from app.engine import PlaceValuation
from app.scoring import TIER_BET, TIER_SUSPECT, TIER_WATCH
from app.winprob import MODEL_PRIORITY

console = Console()
DASH = "—"

#: Short column labels so a three-model table still fits an 80-column window.
MODEL_LABEL = {
    "betfair": "betfair",
    "sportsbet_beta": "sb-beta",
    "sportsbet_power": "sb-power",
}

TIER_STYLE = {
    TIER_BET: "bold green",
    TIER_WATCH: "yellow",
    TIER_SUSPECT: "bold red",
}


def _odds(v: float | None) -> str:
    return f"{v:.2f}" if v else DASH


def _pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else DASH


def _signed(v: float | None) -> str:
    return f"{v:+.1%}" if v is not None else DASH


def _countdown(seconds: float | None) -> str:
    if seconds is None:
        return DASH
    if seconds < 0:
        return "jumped"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def unproven_banner(settled_bets: int, threshold: int) -> Panel | None:
    """The banner stays up until the backtester has enough settled BET signals."""
    if settled_bets >= threshold:
        return None
    return Panel(
        Text.from_markup(
            f"[bold]BET signals are UNPROVEN.[/bold] The backtester holds "
            f"[bold]{settled_bets}[/bold] settled BET signals; ROI and calibration "
            f"are not reported as evidence until there are [bold]{threshold}[/bold]. "
            f"Treat every BET row below as a hypothesis under test, not a tip."
        ),
        border_style="red",
        title="UNPROVEN",
    )


def calibration_banner(cal: Calibration) -> Text:
    bits = [f"lam {cal.lam:.3f}  tau {cal.tau:.3f}  ({cal.version}"]
    if cal.n_races:
        bits.append(f", {cal.n_races:,} races")
    if cal.age_days is not None:
        bits.append(f", fitted {cal.age_days}d ago")
    bits.append(")")
    text = Text("".join(bits), style="dim")
    if not cal.loaded_from_file:
        text.append("  [no calibration file — shipped defaults in use]", style="yellow")
    return text


def render_race(
    valuations: list[PlaceValuation],
    show_all: bool = False,
    now: datetime | None = None,
) -> None:
    """One table for one race, runners ordered by Dr Z score."""
    if not valuations:
        return
    now = now or datetime.now(timezone.utc)
    head = valuations[0]
    models = [m for m in MODEL_PRIORITY if any(m in v.drz_by_model for v in valuations)]

    title = (
        f"{head.venue or '?'} R{head.race_number or '?'}"
        f"  ·  jump in {_countdown(head.seconds_to_jump)}"
        f"  ·  {head.active_runner_count} runners, {head.places} places"
    )
    if head.terms_fragile:
        title += "  ·  [yellow]TERMS FRAGILE[/yellow]"
    if head.betfair_delayed:
        title += "  ·  [yellow]betfair_delayed[/yellow]"

    table = Table(title=title, title_justify="left", header_style="bold",
                  show_lines=False, pad_edge=False)
    table.add_column("#", justify="right", width=3)
    table.add_column("Runner", no_wrap=True, overflow="ellipsis",
                     min_width=16, max_width=22)
    table.add_column("Win", justify="right", width=6)
    table.add_column("Place", justify="right", width=6)
    table.add_column("P(place)", justify="right", width=8)
    table.add_column("Fair", justify="right", width=6)
    for m in models:
        table.add_column(f"drz·{MODEL_LABEL.get(m, m)}", justify="right", width=12)
    table.add_column("EV", justify="right", width=7)
    # The exchange column only appears when there is an exchange opinion to
    # put in it; an empty column of em dashes is just width.
    show_bf = any(v.p_place_betfair is not None for v in valuations)
    if show_bf:
        table.add_column("BF place", justify="right", width=8)
    table.add_column("Tier", width=8)
    table.add_column("Stake", justify="right", width=7)
    table.add_column("Note", no_wrap=True, overflow="ellipsis", max_width=38)

    rows = sorted(
        valuations, key=lambda v: (v.drz if v.drz is not None else -1), reverse=True
    )
    shown = 0
    for v in rows:
        interesting = v.tier in (TIER_BET, TIER_WATCH, TIER_SUSPECT)
        if not show_all and not interesting:
            continue
        shown += 1
        style = TIER_STYLE.get(v.tier, "")
        # Notes are for rows that need explaining. A runner that is simply
        # not value needs no sentence telling you so — the Dr Z column
        # already said it, and a wall of identical notes hides the one row
        # that does deserve a second look.
        note_bits: list[str] = []
        if v.p_place_betfair is not None and v.p_place is not None:
            gap = v.p_place - v.p_place_betfair
            if abs(gap) >= 0.03:
                note_bits.append(
                    f"exchange disagrees by {gap:+.1%} (exchange has been right)"
                )
        if interesting and v.tier != TIER_BET and v.tier_reasons:
            note_bits.append(v.tier_reasons[0])
        if v.price_type != "fixed":
            note_bits.append(f"{v.price_code} is indicative, not takeable")
        cells = [
            str(v.saddlecloth or ""),
            v.horse_name,
            _odds(v.win_price),
            _odds(v.place_price),
            _pct(v.p_place),
            _odds(v.fair_place_odds),
        ]
        for m in models:
            d = v.drz_by_model.get(m)
            cells.append(f"{d:.3f}" if d is not None else DASH)
        cells += [_signed(v.ev)]
        if show_bf:
            cells.append(_pct(v.p_place_betfair))
        cells += [
            Text(v.tier, style=style),
            f"${v.suggested_stake:.2f}" if v.suggested_stake else DASH,
            "; ".join(note_bits) or "",
        ]
        table.add_row(*cells, style=style if v.tier == TIER_BET else None)

    if shown == 0:
        console.print(
            f"[dim]{title} — no runner above the WATCH threshold "
            f"(use --show-all to see the whole field).[/dim]"
        )
        return
    console.print(table)


def render_scan(
    by_race: list[list[PlaceValuation]],
    calibration: Calibration,
    settled_bets: int,
    proven_threshold: int,
    retrieved_at: datetime,
    show_all: bool = False,
    skipped: list[str] | None = None,
) -> None:
    banner = unproven_banner(settled_bets, proven_threshold)
    if banner:
        console.print(banner)
    console.print(calibration_banner(calibration))
    console.print()

    if not by_race:
        console.print(
            f"[yellow]No open thoroughbred race could be valued at "
            f"{retrieved_at:%Y-%m-%d %H:%M:%S %Z}.[/yellow]"
        )
    for race in by_race:
        render_race(race, show_all=show_all, now=retrieved_at)
        console.print()

    n_bet = sum(1 for r in by_race for v in r if v.tier == TIER_BET)
    n_watch = sum(1 for r in by_race for v in r if v.tier == TIER_WATCH)
    n_suspect = sum(1 for r in by_race for v in r if v.tier == TIER_SUSPECT)
    console.print(
        f"[bold]{len(by_race)} races valued[/bold] at "
        f"{retrieved_at:%H:%M:%S %Z} · "
        f"[green]{n_bet} BET[/green] · [yellow]{n_watch} WATCH[/yellow] · "
        f"[red]{n_suspect} SUSPECT[/red]"
    )
    if n_bet == 0 and by_race:
        console.print(
            "[dim]No BET rows. Sportsbet's fixed place prices carry a large "
            "margin, so this is the expected result on most days.[/dim]"
        )
    for line in skipped or []:
        console.print(f"[dim]skipped: {line}[/dim]")
