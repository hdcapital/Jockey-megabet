"""The live screen: plain-English bets first, then the detail table."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.value.compare import FLAG_HELP, ValueRow
from app.value.scanner import SYDNEY, ScanResult


def local_time(dt: datetime) -> str:
    return f"{dt.astimezone(SYDNEY):%I:%M:%S %p}".lstrip("0").lower()


def jump_text(row: ValueRow, now: datetime) -> str:
    if row.start_time is None:
        return "—"
    mins = (row.start_time - now).total_seconds() / 60.0
    if mins < -0.5:
        return f"{-mins:.0f}m late"
    if mins < 1:
        return "now"
    return f"{mins:.0f}m"


def runner_text(row: ValueRow) -> str:
    return f"{row.saddlecloth}. {row.horse}" if row.saddlecloth else row.horse


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def stake_text(row: ValueRow, settings: Any) -> str:
    frac = settings.value_kelly_fraction * row.kelly
    if frac <= 0:
        return ""
    if settings.value_bankroll > 0:
        return f"stake ${max(1, round(settings.value_bankroll * frac)):,}"
    return f"stake {frac:.1%} of bank"


def error_help(error: str) -> str:
    low = error.lower()
    if "login" in low:
        return ("Your Betfair login was refused. Close this window, run START.bat "
                "and choose 3 to re-enter it. The reason Betfair gave is above.")
    if "sportsbet" in low:
        return ("Sportsbet only answers Australian internet connections. Turn off "
                "any VPN and check you're online in Australia.")
    if "betfair" in low:
        return ("Check your internet connection. Betfair also blocks some VPNs and "
                "cloud servers. The scanner keeps retrying every minute.")
    return "The scanner keeps retrying every minute; the details are in data/logs."


def render(
    result: ScanResult,
    settings: Any,
    *,
    pending: int = 0,
    results_line: str | None = None,
    recent_results: list[str] | None = None,
    next_at: datetime | None = None,
    show_all: bool = False,
    new_keys: set[str] | None = None,
    top: int = 25,
    console: Console | None = None,
    clear: bool = False,
) -> None:
    console = console or Console()
    if clear:
        console.clear()
    now = result.at
    title = f"SPORTSBET vs BETFAIR   {now.astimezone(SYDNEY):%a %d %b}  {local_time(now)}"
    if next_at:
        title += f"   (next update {local_time(next_at)})"
    console.rule(f"[bold]{title}[/bold]")

    if result.error:
        console.print(Panel(
            Text.assemble((result.error + "\n\n", "bold red"), (error_help(result.error), "")),
            title="Can't compare right now", border_style="red",
        ))
        return

    bf = ("[yellow]● Betfair: DELAYED key — prices can be minutes old, nothing will "
          "be confirmed[/yellow]" if result.delayed else "[green]● Betfair connected[/green]")
    console.print(
        f"{bf}   [green]● Sportsbet connected[/green]   "
        f"{plural(result.races_in_window, 'race')} in the next "
        f"{settings.value_window_minutes} min, {result.races_compared} compared"
        + (f"   · following {plural(pending, 'bet')} to the result" if pending else "")
    )

    need = settings.value_confirm_scans
    value = [r for r in result.rows if r.reliable and r.ev >= settings.value_min_ev]
    confirmed = [r for r in value if r.streak >= need]
    watching = [r for r in value if r.streak < need]

    # -- the part to act on ------------------------------------------------
    lines: list[Text] = []
    for r in confirmed:
        what = "to WIN" if r.kind == "WIN" else f"to PLACE (top {r.places})"
        horse = f"{r.horse} (#{r.saddlecloth})" if r.saddlecloth else r.horse
        line = Text.assemble(
            ("NEW ", "bold black on yellow") if new_keys and r.key in new_keys else "",
            ("▶ BACK  ", "bold"), (horse, "bold cyan"), (f"  {what}", "bold"),
            (f"   {r.venue} R{r.race_number} · jumps {jump_text(r, now)}", ""),
        )
        detail = Text.assemble(
            "     Sportsbet ", (f"${r.sb_odds:.2f}", "bold green"),
            f"   fair ${r.fair_odds:.2f}   edge ", (f"{r.ev:+.1%}", "bold green"),
            f"   {stake_text(r, settings)}",
            (f"   lock-in {r.lock:+.1%}" if r.lock is not None and r.lock > 0 else ""),
        )
        lines += [line, detail]
    if not lines:
        lines.append(Text("Nothing confirmed right now.", style="dim"))
    console.print(Panel(Group(*lines), border_style="green",
                        title=f"BETS WORTH A LOOK (value on {need} scans in a row, "
                              f"edge ≥ {settings.value_min_ev:.0%})"))
    if watching:
        names = ", ".join(
            f"{runner_text(r)} ({r.venue} R{r.race_number} {r.kind.lower()}, {r.ev:+.1%})"
            for r in watching[:6]
        )
        console.print(f"[yellow]Watching:[/yellow] {names} — confirmed if it holds next scan.")

    # -- detail ----------------------------------------------------------------
    rows = result.rows if show_all else [r for r in result.rows if r.reliable]
    hidden = len(result.rows) - len([r for r in result.rows if r.reliable])
    if rows:
        wide = console.width >= 130  # narrow windows drop the secondary columns
        t = Table(box=box.SIMPLE_HEAD, padding=(0, 1), header_style="bold",
                  title="All compared runners, best edge first", title_justify="left")
        cols = [("Jump", "right"), ("Race", "left"), ("Bet", "left"),
                ("Runner", "left"), ("Sportsbet", "right"), ("Fair", "right"),
                ("Edge", "right"), ("Edge@lay", "right"), ("Seen", "right"),
                ("Betfair back/lay", "right"), ("Lock-in", "right"), ("Flags", "left")]
        narrow_drop = {"Edge@lay", "Betfair back/lay", "Lock-in"}
        for col, just in cols:
            if wide or col not in narrow_drop:
                t.add_column(col, justify=just, no_wrap=col != "Flags")
        for r in rows[:top]:
            good = r.reliable and r.ev >= settings.value_min_ev
            colour = "green" if good else ("yellow" if r.ev > 0 else "dim")
            cells = {
                "Jump": jump_text(r, now), "Race": f"{r.venue} R{r.race_number}",
                "Bet": "win" if r.kind == "WIN" else f"place{r.places}",
                "Runner": runner_text(r), "Sportsbet": f"{r.sb_odds:.2f}",
                "Fair": f"{r.fair_odds:.2f}", "Edge": f"[{colour}]{r.ev:+.1%}[/{colour}]",
                "Edge@lay": f"{r.ev_at_lay:+.1%}" if r.ev_at_lay is not None else "—",
                "Seen": f"{r.streak}×" if r.streak else "",
                "Betfair back/lay": f"{r.bf_back or 0:.2f} / {r.bf_lay or 0:.2f}",
                "Lock-in": f"{r.lock:+.1%}" if r.lock is not None else "—",
                "Flags": ", ".join(r.flags),
            }
            t.add_row(*(cells[c.header] for c in t.columns))
        console.print(t)
        if len(rows) > top:
            console.print(f"[dim]… {len(rows) - top} more rows not shown.[/dim]")
    elif not result.rows:
        console.print("No runners to compare in this window.")
    if hidden and not show_all:
        console.print(f"[dim]{hidden} rows hidden because Betfair's price for them isn't "
                      "trustworthy yet (start with --all to see them).[/dim]")

    used = sorted({f.split()[0] if not f.startswith("8 ") else "8 RUNNERS"
                   for r in rows[:top] for f in r.flags})
    key = ("[dim]Edge = how much more Sportsbet pays than Betfair says it should "
           "(+10% = $1.10 back per $1 on average).  Edge@lay = the same against Betfair's "
           "worst price.  Seen = scans in a row with value.  Lock-in = guaranteed profit "
           "if you lay it straight back on Betfair.")
    if used:
        key += "  Flags: " + "; ".join(f"{f} = {FLAG_HELP.get(f, '')}" for f in used)
    console.print(key + "[/dim]", highlight=False)

    if result.notes:
        for n in result.notes[:4]:
            console.print(f"[yellow]· {n}[/yellow]", highlight=False)
        if len(result.notes) > 4:
            console.print(f"[dim]· … {len(result.notes) - 4} more notes in data/logs[/dim]")
    for line in recent_results or []:
        console.print(f"[bold]{line}[/bold]")
    if results_line:
        console.print(f"[bold]{results_line}[/bold]")
