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


def render_valuations(
    valuations: list[MegabetValuation],
    retrieved_at: datetime,
    show_low_quality: bool = False,
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
        title=f"Jockey Megabet valuations — retrieved {retrieved_at:%Y-%m-%d %H:%M:%S %Z}",
        caption=(
            "EV = model-implied expected return per unit staked; a positive value is "
            "model-implied positive EV, not guaranteed profit."
            + (f"  ({hidden} LOW-quality row(s) hidden; --show-low to display.)"
               if hidden else "")
        ),
    )
    for col in ("Meeting", "Jockey", "Rides", "Market", "SB odds",
                "Fair p", "SB no-vig fair", "Betfair fair", "Consensus fair",
                "EV", "Quality"):
        justify = "right" if col not in ("Meeting", "Jockey", "Market", "Quality") else "left"
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
