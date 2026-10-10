"""Are the edges real? Reads ``data/value_results.csv`` and answers in plain
English, with the numbers behind the answer.

The best early test is the closing line: if Sportsbet's price at the time of
the signal beats Betfair's final pre-race price more often than not, the
scanner is finding prices the market later agrees were too long. Profit and
loss needs hundreds of bets before it means much; the closing line needs
far fewer.
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from app.value.scanner import SYDNEY

#: Below this many settled bets the report says it is too early.
MIN_SAMPLE = 30
GOOD_SAMPLE = 100


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def load_results(path: Path) -> list[dict[str, str]]:
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


@dataclass
class Stats:
    label: str
    signals: int
    settled: int
    avg_edge: float | None
    clv_n: int
    avg_clv: float | None
    beat_close: float | None
    bsp_n: int
    avg_vs_bsp: float | None
    beat_bsp: float | None
    hits: int
    expected_hits: float
    pnl: float
    roi: float | None
    close_lead_median: float | None


def stats(label: str, rows: list[dict[str, str]]) -> Stats:
    settled = [r for r in rows if r.get("result") not in ("", None, "void", "unknown")]
    clv = [v for v in (_f(r.get("clv")) for r in rows) if v is not None]
    bsp = [v for v in (_f(r.get("vs_bsp")) for r in rows) if v is not None]
    edges = [v for v in (_f(r.get("bet_ev")) for r in rows) if v is not None]
    pnl = sum(_f(r.get("pnl")) or 0.0 for r in settled)
    leads = [v for v in (_f(r.get("close_seconds_before_start")) for r in rows) if v is not None]
    return Stats(
        label=label,
        signals=len(rows),
        settled=len(settled),
        avg_edge=statistics.fmean(edges) if edges else None,
        clv_n=len(clv),
        avg_clv=statistics.fmean(clv) if clv else None,
        beat_close=sum(1 for v in clv if v > 0) / len(clv) if clv else None,
        bsp_n=len(bsp),
        avg_vs_bsp=statistics.fmean(bsp) if bsp else None,
        beat_bsp=sum(1 for v in bsp if v > 0) / len(bsp) if bsp else None,
        hits=sum(1 for r in settled if r.get("result") in ("won", "placed")),
        expected_hits=sum(_f(r.get("bet_p")) or 0.0 for r in settled),
        pnl=pnl,
        roi=pnl / len(settled) if settled else None,
        close_lead_median=statistics.median(leads) if leads else None,
    )


def verdict(st: Stats) -> list[str]:
    """Plain-English reading of one group's numbers."""
    out: list[str] = []
    if st.settled < MIN_SAMPLE:
        out.append(f"Too early to tell: {st.settled} settled bet(s) so far. Leave the "
                   f"scanner running; the picture starts to mean something after "
                   f"{GOOD_SAMPLE} or so.")
        if st.avg_clv is not None:
            out.append(f"Early closing-price reading: average {st.avg_clv:+.1%}, beat the "
                       f"close {st.beat_close:.0%} of the time.")
        return out
    if st.avg_clv is not None and st.avg_clv > 0 and (st.beat_close or 0) >= 0.55:
        out.append(f"Good sign: the Sportsbet price beat Betfair's final price on "
                   f"{st.beat_close:.0%} of bets (average {st.avg_clv:+.1%}). That is the "
                   "strongest early evidence that the edges are real.")
    elif st.avg_clv is not None and st.avg_clv > 0:
        out.append(f"Mixed: the average bet beat Betfair's final price by {st.avg_clv:+.1%}, "
                   f"but only {st.beat_close:.0%} of bets did. A few big ones may be "
                   "carrying it.")
    elif st.avg_clv is not None:
        out.append(f"Warning: on average the Sportsbet price did NOT beat Betfair's final "
                   f"price ({st.avg_clv:+.1%}). The edges look like prices the market later "
                   "corrected or noise. Don't bet on them yet.")
    out.append(f"Profit so far: {st.pnl:+.2f} units from {st.settled} one-unit bets "
               f"(return {st.roi:+.1%} per bet); {st.hits} landed vs {st.expected_hits:.1f} "
               "expected. With this many bets luck still dominates the profit figure; "
               "trust the closing-price numbers more.")
    if st.close_lead_median is not None and st.close_lead_median > 120:
        out.append(f"Note: closing prices were taken a median {st.close_lead_median / 60:.0f} "
                   "minutes before the jump (the scanner wasn't running at the jump), so "
                   "the closing-price test is weaker than it should be.")
    return out


def _pct(v: float | None) -> str:
    return f"{v:+.1%}" if v is not None else "—"


def _rate(v: float | None) -> str:
    return f"{v:.0%}" if v is not None else "—"


def bucket(edge: float | None) -> str:
    if edge is None:
        return "?"
    for hi, name in ((0.03, "edge < 3%"), (0.06, "edge 3–6%"), (0.10, "edge 6–10%")):
        if edge < hi:
            return name
    return "edge 10%+"


def render_report(path: Path, console: Console | None = None) -> int:
    console = console or Console()
    rows = load_results(path)
    console.rule("[bold]ARE THE EDGES REAL?[/bold]")
    if not rows:
        console.print(
            "No results yet. Results appear here once the live scanner has flagged a bet "
            "and that race has been run and settled on Betfair (usually a few minutes "
            "after the race).\n"
            f"[dim]Results file: {path}[/dim]"
        )
        return 0

    confirmed = [r for r in rows if r.get("confirmed") == "True"]
    main = stats("Confirmed bets", confirmed) if confirmed else stats("All signals", rows)
    console.print(f"[bold]{main.label}[/bold] (what the scanner told you to look at):")
    for line in verdict(main):
        console.print(f"  • {line}")
    console.print()

    groups = [stats("Confirmed bets", confirmed), stats("All signals", rows),
              stats("Win", [r for r in confirmed or rows if r["kind"] == "WIN"]),
              stats("Place", [r for r in confirmed or rows if r["kind"] == "PLACE"])]
    for name in ("edge < 3%", "edge 3–6%", "edge 6–10%", "edge 10%+"):
        groups.append(stats(name, [r for r in confirmed or rows
                                   if bucket(_f(r.get("bet_ev"))) == name]))
    t = Table(box=box.SIMPLE_HEAD, header_style="bold", padding=(0, 1))
    for col in ("Group", "Bets", "Settled", "Avg edge", "Beat close", "Avg vs close",
                "Beat BSP", "Avg vs BSP", "Landed / expected", "Profit (units)", "Per bet"):
        t.add_column(col, justify="left" if col == "Group" else "right", no_wrap=col == "Group")
    for g in groups:
        if not g.signals:
            continue
        t.add_row(g.label, str(g.signals), str(g.settled), _pct(g.avg_edge),
                  _rate(g.beat_close), _pct(g.avg_clv), _rate(g.beat_bsp),
                  _pct(g.avg_vs_bsp), f"{g.hits} / {g.expected_hits:.1f}",
                  f"{g.pnl:+.2f}", _pct(g.roi))
    console.print(t)
    console.print(
        "[dim]Beat close = Sportsbet's price was better than Betfair's last price before "
        "the jump. BSP = Betfair Starting Price. Profit assumes $1 on every bet at the "
        "Sportsbet price when it was flagged (void/scratched = $0). Win/place/edge rows "
        f"use confirmed bets when there are any.  File: {path}[/dim]"
    )
    return 0


def results_line(path: Path, now: datetime) -> str | None:
    """One line for the live screen: today's settled bets so far."""
    today = now.astimezone(SYDNEY).date()

    def local_day(value: str | None):
        try:
            return datetime.fromisoformat(value).astimezone(SYDNEY).date() if value else None
        except ValueError:
            return None

    rows = [r for r in load_results(path)
            if local_day(r.get("start_time") or r.get("first_seen")) == today]
    if not rows:
        return None
    st = stats("today", rows)
    parts = [f"Today: {st.settled} bet(s) settled"]
    if st.clv_n:
        parts.append(f"beat Betfair's closing price {sum(1 for r in rows if (_f(r.get('clv')) or 0) > 0)}"
                     f"/{st.clv_n}")
    parts.append(f"profit {st.pnl:+.2f} units")
    return " · ".join(parts) + "   (full report: START.bat → 2)"
