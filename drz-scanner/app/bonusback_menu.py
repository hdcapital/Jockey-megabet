"""The no-typing front end for the bonus-back picker.

Double-click ``BONUS BACK.bat`` and this asks, in plain words:

1. how much you are betting (Enter keeps the last amount),
2. which meeting (a numbered list of today's meetings with races left),
3. which race (a numbered list, or Enter for every race left there),

then prints the horse to back in large type and opens a page in the browser
with every runner's numbers. Enter goes round again; Q quits.

Nothing here places a bet.
"""

from __future__ import annotations

import html
import json
import logging
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from rich.panel import Panel
from rich.text import Text

from app.bonusback import (
    BonusBackTerms,
    BonusValueEstimate,
    ConversionOption,
    PromoRace,
    conversion_options,
    low_liability_alternative,
)
from app.calibration import load_calibration
from app.config import DATA_DIR, get_settings
from app.drz import racing_today
from app.http import SourceUnavailableError
from app.logging_setup import setup_logging
from app.reporting.tables import _local_hhmm, console
from app.sources.base import RaceStub, SchemaMismatchError

log = logging.getLogger("app.bonusback_menu")

PREFS_PATH = DATA_DIR / "bonusback_prefs.json"
REPORT_PATH = DATA_DIR / "bonusback.html"


# --- small helpers -----------------------------------------------------------

def load_stake(default: float) -> float:
    try:
        return float(json.loads(PREFS_PATH.read_text())["stake"])
    except (OSError, ValueError, KeyError, TypeError):
        return default


def save_stake(stake: float) -> None:
    try:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(json.dumps({"stake": stake}))
    except OSError:
        pass


def parse_money(text: str) -> float | None:
    t = text.strip().replace("$", "").replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 0 else None


def money(v: float) -> str:
    """``+$4.11`` / ``-$2.50``."""
    return f"{'-' if v < 0 else '+'}${abs(v):.2f}"


def minutes_until(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return ""
    mins = int((dt - now).total_seconds() // 60)
    if mins < 0:
        return "jumping now"
    if mins < 60:
        return f"in {mins} min"
    return f"in {mins // 60}h {mins % 60:02d}m"


def meetings_with_races(stubs: list[RaceStub]) -> list[tuple[str, list[RaceStub]]]:
    """Open races grouped by meeting, meetings ordered by their next race."""
    from app.drz import EXCLUDED_EVENT_IDS

    groups: dict[str, list[RaceStub]] = {}
    far = datetime.max.replace(tzinfo=timezone.utc)
    for s in sorted(stubs, key=lambda s: s.start_time or far):
        if s.is_open and s.event_id not in EXCLUDED_EVENT_IDS:
            groups.setdefault(s.meeting_name, []).append(s)
    return sorted(groups.items(), key=lambda kv: kv[1][0].start_time or far)


# --- what the person sees -----------------------------------------------------

def show_pick(pr: PromoRace, stake: float) -> None:
    now = datetime.now(timezone.utc)
    head = (f"{(pr.venue or '').upper()}  RACE {pr.race_number}   "
            f"jumps {_local_hhmm(pr.start_time)} ({minutes_until(pr.start_time, now)})")
    best = pr.pick.best
    body = Text()
    if best is None:
        body.append("NO BET\n", style="bold yellow")
        body.append("None of the horses in this race is worth backing for the promo.\n")
        ranked = [v for v in pr.pick.ranked if v.eligible]
        if ranked:
            top = max(ranked, key=lambda v: v.ev_back_conservative)
            body.append(f"Closest was #{top.inputs.saddlecloth} {top.horse_name} "
                        f"({money(top.ev_back_conservative * stake)} on ${stake:.0f}).", style="dim")
        console.print(Panel(body, title=head, border_style="yellow", padding=(1, 2)))
        return
    x = best.inputs
    body.append("BACK THIS HORSE (win bet):\n\n", style="bold")
    body.append(f"   #{x.saddlecloth}  {x.horse_name.upper()}   @ ${x.win_price:.2f}\n\n",
                style="bold green")
    body.append(f"Bet ${stake:.0f}. Expected profit about {money(best.ev_back_conservative * stake)} "
                f"(including the bonus bet if it runs 2nd or 3rd).\n")
    body.append(f"Chance it wins: {x.p_win:.0%}   Chance of 2nd or 3rd: {x.p23:.0%}\n")
    others = sorted((v for v in pr.valuations if v is not best and v.eligible),
                    key=lambda v: -v.ev_back_conservative)
    if others and others[0].ev_back_conservative > 0:
        o = others[0]
        body.append(f"Next best: #{o.inputs.saddlecloth} {o.horse_name} @ ${o.inputs.win_price:.2f} "
                    f"({money(o.ev_back_conservative * stake)})\n", style="dim")
    if pr.start_time and (pr.start_time - now).total_seconds() > 3600:
        body.append("\nThis race is more than an hour away. Prices move, so check it again "
                    "closer to the jump.", style="yellow")
    console.print(Panel(body, title=head, border_style="green", padding=(1, 2)))


def show_bonus_value(est: BonusValueEstimate, stake: float) -> None:
    """One plain line on what a bonus bet is worth today, and why."""
    if est.option is None:
        console.print(f"[dim]A ${stake:.0f} bonus bet is counted as ${est.value * stake:.2f} "
                      f"cash ({est.detail}).[/dim]\n")
        return
    x = est.option.inputs
    pr = est.option.race
    if est.method == "lay":
        how = (f"backing #{x.saddlecloth} {x.horse_name} @ ${x.win_price:.2f} in "
               f"{pr.venue} R{pr.race_number} and laying it on Betfair at {est.option.lay_price:.2f}")
    else:
        how = (f"betting it on #{x.saddlecloth} {x.horse_name} @ ${x.win_price:.2f} in "
               f"{pr.venue} R{pr.race_number} ({x.p_win:.0%} to win)")
    console.print(f"[dim]A ${stake:.0f} bonus bet is worth about [bold]${est.value * stake:.2f}[/bold] "
                  f"today: {how} turns it into ${(est.raw or 0) * stake:.2f}, less "
                  f"{1 - get_settings().bonus_value_haircut:.0%} for prices moving.[/dim]\n")


def write_html(results: list[PromoRace], stake: float, path: Path | None = None,
               estimate: BonusValueEstimate | None = None) -> Path:
    path = path or REPORT_PATH
    r = estimate.value if estimate else get_settings().bonus_bet_value
    basis = (f" ({html.escape(estimate.detail)})" if estimate else "")
    now = datetime.now(timezone.utc)
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Bonus Back picks</title><style>"
        "body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:16px;background:#fafafa;color:#222}"
        "h1{font-size:22px}h2{font-size:18px;margin:28px 0 6px}"
        ".pick{background:#e7f6ea;border:2px solid #2e8b47;padding:12px 16px;border-radius:8px;font-size:18px}"
        ".nobet{background:#fff6dd;border:2px solid #c99a00;padding:12px 16px;border-radius:8px;font-size:18px}"
        "table{border-collapse:collapse;margin-top:8px;font-size:14px;width:100%;max-width:900px}"
        "th,td{padding:4px 8px;border-bottom:1px solid #ddd;text-align:right}"
        "th:nth-child(2),td:nth-child(2){text-align:left}"
        "tr.best td{background:#e7f6ea;font-weight:bold}tr.off td{color:#999}"
        ".wrap{overflow-x:auto}.dim{color:#666;font-size:13px}</style></head><body>",
        f"<h1>Bonus back if 2nd or 3rd — picks for a ${stake:.0f} bet</h1>",
        f"<p class='dim'>Prices as at {_local_hhmm(now)}. Expected profit includes the bonus bet "
        f"valued at {r:.0%} of its face value{basis}. Nothing here places a bet.</p>",
    ]
    for pr in results:
        title = (f"{html.escape(pr.venue or '')} Race {pr.race_number} — jumps "
                 f"{_local_hhmm(pr.start_time)} ({minutes_until(pr.start_time, now)})")
        parts.append(f"<h2>{title}</h2>")
        best = pr.pick.best
        if best:
            x = best.inputs
            parts.append(
                f"<div class='pick'>Back <b>#{x.saddlecloth} {html.escape(x.horse_name)}</b> "
                f"@ ${x.win_price:.2f} — expected profit about "
                f"<b>{money(best.ev_back_conservative * stake)}</b> on ${stake:.0f}</div>")
        else:
            parts.append("<div class='nobet'><b>No bet</b> — no horse here is worth it for the promo.</div>")
        parts.append("<div class='wrap'><table><tr><th>#</th><th>Horse</th><th>Price</th>"
                     "<th>Win chance</th><th>2nd/3rd chance</th>"
                     f"<th>Expected profit on ${stake:.0f}</th></tr>")
        for v in sorted(pr.valuations, key=lambda v: -v.ev_back_conservative):
            x = v.inputs
            cls = "best" if v is best else ("off" if not v.eligible else "")
            parts.append(
                f"<tr class='{cls}'><td>{x.saddlecloth or ''}</td><td>{html.escape(x.horse_name)}</td>"
                f"<td>${x.win_price:.2f}</td><td>{x.p_win:.0%}</td><td>{x.p23:.0%}</td>"
                f"<td>{money(v.ev_back_conservative * stake)}</td></tr>")
        parts.append("</table></div>")
    parts.append("</body></html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")
    return path


# --- the loop -------------------------------------------------------------------

def _choose(prompt: str, n: int, ask) -> int | str | None:
    """A number 1..n, '' for Enter, 'q' to quit, or None if not understood."""
    ans = ask(prompt).strip().lower()
    if ans in ("q", "quit", "exit"):
        return "q"
    if ans == "":
        return ""
    if ans.isdigit() and 1 <= int(ans) <= n:
        return int(ans)
    return None


def run_menu(ask=input, sb_factory=None, open_browser=webbrowser.open) -> int:
    setup_logging(logging.WARNING)
    settings = get_settings()
    calibration = load_calibration()
    from app.bonusback_scan import open_betfair, value_stubs

    if sb_factory is None:
        from app.sources.sportsbet import SportsbetClient as sb_factory

    console.print(Panel(Text(
        "Picks the best horse for Sportsbet's\n"
        "\"money back as a bonus bet if your horse runs 2nd or 3rd\" offer,\n"
        "and shows you how to turn the bonus bet into cash.\n\n"
        "Just type a number and press Enter. Type Q to quit at any time.",
        justify="center"), title="BONUS BACK PICKER", border_style="cyan", padding=(1, 4)))

    for_date = racing_today()
    bf = open_betfair(for_date, settings, quiet=True)
    if bf is None:
        console.print("[dim](No Betfair login set up, so Sportsbet's own prices are used. "
                      "Betfair is only needed to lock in cash from a bonus bet.)[/dim]")
    try:
        with sb_factory() as sb:
            while True:
                console.print("\n[bold]What do you want to do?[/bold]")
                console.print("  [bold cyan] 1[/bold cyan]  Pick a horse for the 2nd/3rd bonus-back promo")
                console.print("  [bold cyan] 2[/bold cyan]  Use a bonus bet I've got (turn it into cash)")
                mode = _choose("\nType 1 or 2 (Q to quit): ", 2, ask)
                if mode == "q":
                    return 0
                if mode not in (1, 2):
                    if mode is None:
                        console.print("[yellow]Type 1 or 2.[/yellow]")
                    continue
                if mode == 1:
                    done = _promo_flow(sb, bf, for_date, calibration, settings, ask,
                                       open_browser, value_stubs)
                else:
                    done = _convert_flow(sb, bf, for_date, calibration, settings, ask,
                                         value_stubs)
                if done:
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if bf is not None:
            bf.close()


def _ask_amount(question: str, default: float, ask) -> float | None:
    """A dollar amount; Enter keeps ``default``; None means quit."""
    while True:
        ans = ask(f"{question} Press Enter for ${default:.0f}: ").strip()
        if ans.lower() in ("q", "quit"):
            return None
        if ans == "":
            return default
        v = parse_money(ans)
        if v is not None:
            return v
        console.print("[yellow]Just type an amount, like 50.[/yellow]")


#: The schedule from the most recent ``_pick_races`` call, so the bonus value
#: can be measured on the races after the chosen one without refetching it.
LAST_SCHEDULE: list[RaceStub] = []


def _pick_races(sb, for_date, ask, heading: str, allow_next_hour: bool = False):
    """Meeting then race, by number. Returns stubs, ``"q"`` to quit, or
    ``None`` to go back to the start."""
    while True:
        console.print("\n[dim]Loading today's races...[/dim]")
        try:
            _m, stubs, _raw = sb.fetch_schedule(for_date)
        except (SourceUnavailableError, SchemaMismatchError) as exc:
            console.print(f"[bold red]Couldn't reach Sportsbet.[/bold red] Check your "
                          f"internet and try again.\n[dim]{exc}[/dim]")
            if _choose("Press Enter to try again, or Q to quit: ", 0, ask) == "q":
                return "q"
            continue
        LAST_SCHEDULE[:] = stubs
        meetings = meetings_with_races(stubs)
        if not meetings:
            console.print("No Australian races left to bet on today.")
            return None
        now = datetime.now(timezone.utc)
        console.print(f"\n[bold]{heading}[/bold]")
        for i, (name, races) in enumerate(meetings, 1):
            nxt = races[0]
            console.print(f"  [bold cyan]{i:>2}[/bold cyan]  {name:<22} next is Race "
                          f"{nxt.race_number} at {_local_hhmm(nxt.start_time)} "
                          f"({minutes_until(nxt.start_time, now)}), "
                          f"{len(races)} race{'s' if len(races) != 1 else ''} left")
        hint = ("Enter = every race in the next hour" if allow_next_hour
                else "Enter to refresh")
        pick = _choose(f"\nType the meeting number ({hint}, Q to quit): ", len(meetings), ask)
        if pick == "q":
            return "q"
        if pick == "" and allow_next_hour:
            soon = [st for _n, rs in meetings for st in rs
                    if st.start_time and (st.start_time - now).total_seconds() <= 3600]
            if soon:
                return soon
            console.print("[yellow]No races in the next hour; pick a meeting instead.[/yellow]")
            continue
        if pick in ("", None):
            if pick is None:
                console.print("[yellow]That's not one of the numbers.[/yellow]")
            continue
        name, races = meetings[pick - 1]

        console.print(f"\n[bold]Which race at {name}?[/bold]")
        for i, st in enumerate(races, 1):
            console.print(f"  [bold cyan]{i:>2}[/bold cyan]  Race {st.race_number:<3} "
                          f"{_local_hhmm(st.start_time)}  "
                          f"({minutes_until(st.start_time, now)})")
        rp = None
        while rp is None:
            rp = _choose("\nType the number from the list "
                         "(Enter for ALL races at this meeting, Q to quit): ",
                         len(races), ask)
            if rp is None:
                console.print("[yellow]That's not one of the numbers.[/yellow]")
        if rp == "q":
            return "q"
        return races if rp == "" else [races[rp - 1]]


def _promo_flow(sb, bf, for_date, calibration, settings, ask, open_browser, value_stubs) -> bool:
    """Option 1. Returns True when the person asked to quit."""
    stake = _ask_amount("How much are you betting?", load_stake(settings.bonus_max_stake), ask)
    if stake is None:
        return True
    save_stake(stake)
    terms = BonusBackTerms(
        bonus_value=settings.bonus_bet_value, max_stake=stake,
        commission=settings.betfair_commission, min_ev=settings.bonus_min_ev,
    )
    while True:
        chosen = _pick_races(sb, for_date, ask, "Which meeting is the promo on?")
        if chosen == "q":
            return True
        if chosen is None:
            return False
        console.print("[dim]Working it out (checking later races to value the bonus bet)...[/dim]\n")
        from app.bonusback_scan import live_bonus_value, with_bonus_value

        if bf is not None:
            bf.refresh()
        est = live_bonus_value(sb, bf, LAST_SCHEDULE, chosen[0].start_time, calibration,
                               settings, terms.commission)
        terms = with_bonus_value(terms, est.value)
        show_bonus_value(est, stake)
        results, problems = value_stubs(
            sb, bf, chosen, calibration, settings, terms, "ev",
            show=lambda pr: show_pick(pr, stake), refresh=False,
        )
        for msg in problems:
            console.print(f"[yellow]{msg}[/yellow]")
        if results:
            if any(pr.pick.best for pr in results):
                console.print(f"[dim]If your horse runs 2nd or 3rd you'll get a ${stake:.0f} bonus "
                              f"bet. Run this again and choose 2 to turn it into cash.[/dim]")
            try:
                report = write_html(results, stake, estimate=est)
                console.print(f"[dim]Full details for every horse opened in your browser "
                              f"({report.name}).[/dim]")
                open_browser(report.resolve().as_uri())
            except OSError as exc:
                log.warning("report not written: %s", exc)
        ans = _choose("\nPress Enter to check another race, M for the main menu, or Q to quit: ",
                      0, ask)
        if ans == "q":
            return True
        if ans is None:   # anything else typed, e.g. "m"
            return False


def _convert_flow(sb, bf, for_date, calibration, settings, ask, value_stubs) -> bool:
    """Option 2: where to put a bonus bet. Returns True when asked to quit."""
    bonus = _ask_amount("How big is your bonus bet?", load_stake(settings.bonus_max_stake), ask)
    if bonus is None:
        return True
    terms = BonusBackTerms(
        bonus_value=settings.bonus_bet_value, max_stake=bonus,
        commission=settings.betfair_commission, min_ev=settings.bonus_min_ev,
    )
    while True:
        chosen = _pick_races(sb, for_date, ask, "Where do you want to use it?",
                             allow_next_hour=True)
        if chosen == "q":
            return True
        if chosen is None:
            return False
        console.print(f"[dim]Checking {len(chosen)} race{'s' if len(chosen) != 1 else ''}...[/dim]\n")
        results, problems = value_stubs(sb, bf, chosen, calibration, settings, terms, "ev")
        for msg in problems:
            console.print(f"[yellow]{msg}[/yellow]")
        show_conversion(results, bonus, settings.betfair_commission, betfair=bf is not None)
        ans = _choose("\nPress Enter to look again, M for the main menu, or Q to quit: ", 0, ask)
        if ans == "q":
            return True
        if ans is None:
            return False


def _where(c: ConversionOption) -> str:
    pr, x = c.race, c.inputs
    return (f"{pr.venue} Race {pr.race_number} ({_local_hhmm(pr.start_time)}): "
            f"#{x.saddlecloth} {x.horse_name.upper()}")


def show_conversion(results: list[PromoRace], bonus: float, commission: float,
                    betfair: bool) -> None:
    lay, keep = conversion_options(results, bonus, commission)
    fillable = [c for c in lay if c.enough_on_offer]
    if fillable:
        best = fillable[0]
        x = best.inputs
        win_side = bonus * (x.win_price - 1) - best.liability
        lose_side = best.lay_stake * (1 - commission)
        body = Text()
        body.append("LOCK IN CASH (same result whatever happens)\n\n", style="bold")
        body.append("Step 1 - Sportsbet: ", style="bold")
        body.append(f"put your ${bonus:.0f} BONUS BET on\n   {_where(best)} to WIN @ ${x.win_price:.2f}\n\n",
                    style="green")
        body.append("Step 2 - Betfair: ", style="bold")
        body.append(f"LAY the same horse in the WIN market\n   at {best.lay_price:.2f} "
                    f"with a lay stake of ${best.lay_stake:.2f}\n", style="green")
        body.append(f"   (Betfair will hold ${best.liability:.2f} of your balance while the race is run)\n\n",
                    style="dim")
        body.append(f"You keep about ${best.cash:.2f} either way ({best.per_dollar:.0%} of the bonus):\n")
        body.append(f"   if it wins:  Sportsbet pays ${bonus * (x.win_price - 1):.2f}, Betfair costs "
                    f"${best.liability:.2f}  ->  {money(win_side)}\n", style="dim")
        body.append(f"   if it loses: Betfair pays ${lose_side:.2f} after commission  ->  "
                    f"{money(lose_side)}\n", style="dim")
        body.append("\nDo Step 1 first, then Step 2 straight away. If the Betfair price has moved, "
                    "run this again for a new lay stake.", style="yellow")
        console.print(Panel(body, title="USE YOUR BONUS BET", border_style="green", padding=(1, 2)))
        alt = low_liability_alternative(lay)
        if alt is not None:
            console.print(f"  Not that much in Betfair? {_where(alt)} @ ${alt.inputs.win_price:.2f}, "
                          f"lay ${alt.lay_stake:.2f} at {alt.lay_price:.2f} — needs only "
                          f"${alt.liability:.2f} and keeps about ${alt.cash:.2f}.")
        if keep:
            k = keep[0]
            console.print(f"[dim]  Or skip Betfair and just bet it: best is {_where(k)} "
                          f"@ ${k.inputs.win_price:.2f}, worth about ${k.cash:.2f} on average "
                          f"but usually it just loses.[/dim]")
        return
    if lay and not fillable:
        console.print("[yellow]Betfair doesn't have enough money on offer to lay these yet. "
                      "Try again closer to the jump, when the markets fill up.[/yellow]")
    if not keep:
        console.print("[yellow]Nothing suitable in those races. Try another meeting.[/yellow]")
        return
    k = keep[0]
    body = Text()
    body.append("JUST BET IT\n\n", style="bold")
    body.append("Sportsbet: ", style="bold")
    body.append(f"put your ${bonus:.0f} BONUS BET on\n   {_where(k)} to WIN @ ${k.inputs.win_price:.2f}\n\n",
                style="green")
    body.append(f"It wins about {k.inputs.p_win:.0%} of the time and would pay "
                f"${bonus * (k.inputs.win_price - 1):.2f}. On average that's worth about ${k.cash:.2f}, "
                f"but most of the time it will lose.\n")
    if not betfair:
        body.append("\nTo turn a bonus bet into guaranteed cash (usually 70-80% of it), you need a "
                    "Betfair account: add your Betfair login to the .env file and this will show "
                    "you the exact lay to place.", style="yellow")
    console.print(Panel(body, title="USE YOUR BONUS BET", border_style="green", padding=(1, 2)))
