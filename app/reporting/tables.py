"""Terminal output. Every number shown comes from retrieved data or a
calculation over retrieved data; unavailable values render as an em dash."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.table import Table

from app.engine import MegabetValuation

console = Console()

DASH = "—"


def _fmt_odds(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else DASH


def _fmt_pct(v: float | None) -> str:
    return f"{v:+.1%}" if v is not None else DASH


TYPE_LABELS = {"jockey": "Jockey Megabets", "trainer": "Trainer Megabets"}


def render_all(
    valuations: list[MegabetValuation],
    challenge_valuations: list,
    retrieved_at: datetime,
    show_low_quality: bool = False,
) -> None:
    """Full scan output, one section per market type, each sorted by EV."""
    for mtype, label in TYPE_LABELS.items():
        subset = [v for v in valuations if v.offer.market_type == mtype]
        if subset:
            render_valuations(
                subset, retrieved_at, show_low_quality, title=label,
                entity_label="Trainer" if mtype == "trainer" else "Jockey",
            )
        else:
            console.print(
                f"[yellow]No {label} found at "
                f"{retrieved_at:%Y-%m-%d %H:%M:%S %Z}.[/yellow]"
            )
    render_challenges(challenge_valuations, retrieved_at, show_low_quality)


def render_challenges(
    challenge_valuations: list,
    retrieved_at: datetime,
    show_low_quality: bool = False,
) -> None:
    if not challenge_valuations:
        console.print(
            f"[yellow]No Jockey Challenge markets found at "
            f"{retrieved_at:%Y-%m-%d %H:%M:%S %Z}.[/yellow]"
        )
        return
    shown = [v for v in challenge_valuations
             if show_low_quality or v.quality != "LOW"]
    hidden = len(challenge_valuations) - len(shown)
    shown.sort(key=lambda v: (v.expected_return is None,
                              -(v.expected_return or 0.0)))
    table = Table(
        title=f"Jockey Challenge (most wins) — retrieved {retrieved_at:%Y-%m-%d %H:%M:%S %Z}",
        caption=(
            "Fair p from Monte Carlo over the meeting's de-vigged win markets; "
            "settlement assumed 'most winners, dead-heats divided' — verify "
            "Sportsbet's rules before betting."
            + (f"  ({hidden} LOW-quality row(s) hidden.)" if hidden else "")
        ),
    )
    for col in ("Meeting", "Competitor", "Races", "SB odds", "Fair p",
                "Fair odds", "EV", "Quality"):
        table.add_column(col, justify="right" if col not in
                         ("Meeting", "Competitor", "Quality") else "left")
    for v in shown:
        table.add_row(
            v.offer.meeting_name or DASH,
            v.offer.competitor,
            str(v.n_races),
            _fmt_odds(v.offer.odds),
            f"{v.fair_probability:.3f}" if v.fair_probability is not None else DASH,
            _fmt_odds(v.fair_odds),
            _fmt_pct(v.expected_return),
            v.quality,
        )
    if shown:
        console.print(table)
    elif hidden:
        console.print(
            f"[yellow]{hidden} Jockey Challenge valuation(s) hidden as "
            "LOW-quality; --show-low to display.[/yellow]"
        )


def render_valuations(
    valuations: list[MegabetValuation],
    retrieved_at: datetime,
    show_low_quality: bool = False,
    title: str = "Jockey Megabet valuations",
    entity_label: str = "Jockey",
) -> None:
    """Print the opportunities table (consensus-model rows), sorted by EV."""
    rows = [v for v in valuations if v.model == "consensus"]
    by_offer = {}
    for v in valuations:
        by_offer.setdefault(id(v.offer), {})[v.model] = v

    shown = [
        v for v in rows
        if show_low_quality or v.quality != "LOW"
    ]
    hidden = len(rows) - len(shown)
    shown.sort(key=lambda v: (v.expected_return is None,
                              -(v.expected_return or 0.0)))

    table = Table(
        title=f"{title} — retrieved {retrieved_at:%Y-%m-%d %H:%M:%S %Z}",
        caption=(
            "EV = model-implied expected return per unit staked; a positive value is "
            "model-implied positive EV, not guaranteed profit."
            + (f"  ({hidden} LOW-quality row(s) hidden; --show-low to display.)"
               if hidden else "")
        ),
    )
    rides_label = "Races" if entity_label == "Trainer" else "Rides"
    for col in ("Meeting", entity_label, rides_label, "Market", "SB odds",
                "Fair p", "SB no-vig fair", "Betfair fair", "Consensus fair",
                "EV", "Quality"):
        justify = "right" if col not in ("Meeting", entity_label, "Market", "Quality") else "left"
        table.add_column(col, justify=justify)

    for v in shown:
        peers = by_offer[id(v.offer)]
        table.add_row(
            v.offer.meeting_name or DASH,
            v.offer.jockey_name,
            str(len(v.ride_card.rides)),
            f"{v.offer.threshold}+ wins",
            _fmt_odds(v.offer.odds),
            f"{v.fair_probability:.3f}" if v.fair_probability is not None else DASH,
            _fmt_odds((peers.get("sportsbet_novig") or v).alt_fair_odds.get("sportsbet_novig")),
            _fmt_odds(v.alt_fair_odds.get("betfair")),
            _fmt_odds(v.alt_fair_odds.get("consensus")),
            _fmt_pct(v.expected_return),
            v.quality,
        )
    if not shown:
        console.print(
            f"[yellow]No displayable Jockey Megabet valuations at "
            f"{retrieved_at:%Y-%m-%d %H:%M:%S %Z}"
            + (f" ({hidden} LOW-quality row(s) hidden; --show-low to display).[/yellow]"
               if hidden else ".[/yellow]")
        )
        return
    console.print(table)


def print_no_megabets(retrieved_at: datetime) -> None:
    console.print(
        f"[yellow]No active Jockey Megabets found at "
        f"{retrieved_at:%Y-%m-%d %H:%M:%S %Z}.[/yellow]"
    )


def print_source_unavailable(source: str, detail: str) -> None:
    console.print(
        f"[red]Data source unavailable — {source}: {detail}[/red]\n"
        "[red]No data is displayed because no real data could be retrieved.[/red]"
    )
