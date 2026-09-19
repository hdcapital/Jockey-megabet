"""A self-contained HTML report, rewritten after every sweep.

The terminal is a poor place to read a day's racing: tables scroll past,
log lines land between them, and nothing stays put. This writes one page —
every race in jump order, every runner, tier colours, reasons — to
``data/latest.html``. Open it in a browser once; it reloads itself.

No dependencies, no server. Everything the page needs is inline, and the
numbers are the same objects the terminal table was rendered from.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import MODEL_VERSION
from app.calibration import Calibration
from app.engine import PlaceValuation
from app.scoring import TIER_BET, TIER_SUSPECT, TIER_WATCH
from app.winprob import MODEL_PRIORITY

RACING_TZ = ZoneInfo("Australia/Sydney")

_STYLE = """
:root{--bg:#f4f6f8;--paper:#fff;--ink:#1a222c;--soft:#5b6572;--line:#d8dde3;
--bet:#2a7a4b;--bet-bg:#e3f3e8;--watch:#a86f0e;--watch-bg:#fbefd4;--sus:#b5402c;--sus-bg:#f9e2dc;
--mark:#eceff2;--bf:#35577f;--mono:"IBM Plex Mono",Consolas,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#141920;--paper:#1c232c;--ink:#eef1f4;--soft:#a3adb9;
--line:#333d49;--bet:#6cc38f;--bet-bg:#1e3a2a;--watch:#e4a93a;--watch-bg:#3d2f14;--sus:#ec8570;--sus-bg:#43231f;
--mark:#28313c;--bf:#8fb3dc}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif;padding:18px 20px 60px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:17px;margin:0}
.top{display:flex;flex-wrap:wrap;gap:8px 22px;align-items:baseline;color:var(--soft);font-size:14px;margin-bottom:10px}
.top b{color:var(--ink)}
.banner{background:var(--sus-bg);color:var(--sus);border-left:5px solid var(--sus);padding:10px 14px;margin:10px 0 16px;font-size:14px}
.summary{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0 18px}
.chip{padding:5px 12px;border-radius:999px;background:var(--mark);font-weight:600;font-size:14px}
.chip.bet{background:var(--bet-bg);color:var(--bet)}.chip.watch{background:var(--watch-bg);color:var(--watch)}
.chip.sus{background:var(--sus-bg);color:var(--sus)}
.race{background:var(--paper);border:1px solid var(--line);border-radius:8px;margin:0 0 14px;overflow:hidden}
.race.has-bet{border-color:var(--bet);box-shadow:0 0 0 2px var(--bet-bg)}
.rh{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline;padding:10px 14px;border-bottom:1px solid var(--line)}
.rh .meta{color:var(--soft);font-size:13px}.rh .cd{font-family:var(--mono);font-variant-numeric:tabular-nums}
.rh .cd.soon{color:var(--sus);font-weight:700}.rh .frag{color:var(--watch);font-weight:600;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:right;color:var(--soft);font-weight:600;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{text-align:right;padding:6px 10px;border-bottom:1px solid var(--line);font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
th.l,td.l{text-align:left;font-family:inherit}
tr:last-child td{border-bottom:none}
tr.BET td{background:var(--bet-bg)}tr.WATCH td.tier{color:var(--watch)}tr.SUSPECT td{background:var(--sus-bg)}
tr.none{color:var(--soft)}
td.tier{font-weight:700}tr.BET td.tier{color:var(--bet)}tr.SUSPECT td.tier{color:var(--sus)}
td.note{text-align:left;font-family:inherit;color:var(--soft);font-size:13px;white-space:normal;max-width:34ch}
td.bf,th.bf{color:var(--bf)}
.skipped{color:var(--soft);font-size:13px;margin-top:20px}.skipped div{margin:2px 0}
.wrap{overflow-x:auto}
"""

_SCRIPT = """
(function(){
  function tick(){
    var now=Date.now()/1000;
    document.querySelectorAll('[data-jump]').forEach(function(el){
      var s=parseFloat(el.getAttribute('data-jump'))-now;
      if(isNaN(s)){el.textContent='—';return;}
      if(s<0){el.textContent='jumped';el.classList.add('soon');return;}
      var m=Math.floor(s/60),x=Math.floor(s%60);
      el.textContent=(m?m+'m':'')+(x<10?'0':'')+x+'s';
      el.classList.toggle('soon',s<=600);
    });
  }
  tick();setInterval(tick,1000);
})();
"""


def _local(dt: datetime | None, fmt: str = "%H:%M") -> str:
    return dt.astimezone(RACING_TZ).strftime(fmt) if dt else "—"


def _odds(v: float | None) -> str:
    return f"{v:.2f}" if v else "—"


def _pct(v: float | None) -> str:
    return f"{v:.1%}" if v is not None else "—"


def _note(v: PlaceValuation) -> str:
    bits: list[str] = []
    if v.tier != TIER_BET and v.tier_reasons:
        bits.append(v.tier_reasons[0].split(":", 1)[0].replace("_", " "))
    interesting = v.tier in (TIER_BET, TIER_WATCH, TIER_SUSPECT)
    if interesting and v.p_place_betfair is not None and v.p_place is not None:
        gap = v.p_place - v.p_place_betfair
        if abs(gap) >= 0.03:
            bits.append(f"exchange differs by {gap:+.1%}")
    if v.band_shrink < 1.0:
        bits.append(f"band ×{v.band_shrink:.3f}")
    if v.tier == TIER_BET:
        bits.append("all checks passed")
    return "; ".join(bits)


def render_html(
    by_race: list[list[PlaceValuation]],
    calibration: Calibration,
    settled_bets: int,
    proven_threshold: int,
    retrieved_at: datetime,
    skipped: list[str] | None = None,
    refresh_seconds: int = 45,
    heading: str = "drz-scanner — today's Australian races",
    caveat: str | None = None,
) -> str:
    """The full page as a string.

    ``refresh_seconds`` of 0 disables the auto-reload (a one-off day card
    is a snapshot, not a live view). ``caveat`` is a short paragraph shown
    under the heading — the day card uses it to say when its prices were
    taken and what can change after that.
    """
    e = html.escape
    races = sorted(
        by_race,
        key=lambda r: (r[0].start_time is None, r[0].start_time or datetime.max.replace(tzinfo=timezone.utc)),
    )
    n_bet = sum(1 for r in races for v in r if v.tier == TIER_BET)
    n_watch = sum(1 for r in races for v in r if v.tier == TIER_WATCH)
    n_sus = sum(1 for r in races for v in r if v.tier == TIER_SUSPECT)

    out: list[str] = []
    out.append(f'<!doctype html><html lang="en"><head><meta charset="utf-8">')
    if refresh_seconds > 0:
        out.append(f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">')
    out.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    out.append(f"<title>drz {_local(retrieved_at)} · {n_bet} BET · {n_watch} WATCH</title>")
    out.append(f"<style>{_STYLE}</style></head><body>")
    out.append(f"<h1>{e(heading)}</h1>")
    out.append(
        f'<div class="top"><span>as of <b>{_local(retrieved_at, "%H:%M:%S")}</b> AEST/AEDT</span>'
        f'<span>model <b>{e(MODEL_VERSION)}</b></span>'
        f'<span>lam <b>{calibration.lam:.3f}</b> tau <b>{calibration.tau:.3f}</b> ({e(calibration.version)})</span>'
        + (f'<span>page reloads every {int(refresh_seconds)}s</span>' if refresh_seconds > 0
           else '<span>snapshot — does not reload</span>')
        + '</div>'
    )
    if caveat:
        out.append(f'<div class="banner" style="background:var(--watch-bg);color:var(--watch);'
                   f'border-color:var(--watch)">{e(caveat)}</div>')
    if settled_bets < proven_threshold:
        out.append(
            f'<div class="banner"><b>BET signals are UNPROVEN.</b> {settled_bets} settled BET '
            f'signals of the {proven_threshold} needed before ROI is treated as evidence. '
            f'Every BET row is a hypothesis under test, not a tip.</div>'
        )
    out.append(
        f'<div class="summary"><span class="chip">{len(races)} races valued</span>'
        f'<span class="chip bet">{n_bet} BET</span><span class="chip watch">{n_watch} WATCH</span>'
        f'<span class="chip sus">{n_sus} SUSPECT</span></div>'
    )
    if not races:
        out.append('<p class="skipped">No open Australian race could be valued on this sweep.</p>')

    for race in races:
        head = race[0]
        models = [m for m in MODEL_PRIORITY if any(m in v.drz_by_model for v in race)]
        show_bf = any(v.p_place_betfair is not None for v in race)
        has_bet = any(v.tier == TIER_BET for v in race)
        jump_epoch = head.start_time.timestamp() if head.start_time else ""
        out.append(f'<section class="race{" has-bet" if has_bet else ""}">')
        out.append('<div class="rh">')
        out.append(f"<h2>{e(head.venue or '?')} R{head.race_number or '?'}</h2>")
        out.append(f'<span class="meta">jumps {_local(head.start_time)}</span>')
        out.append(f'<span class="cd" data-jump="{jump_epoch}">—</span>')
        out.append(
            f'<span class="meta">{head.active_runner_count} runners · {head.places} places · '
            f'win model {e(head.win_model.replace("sportsbet_", "sb-"))}'
            f'{" · exchange DELAYED" if head.betfair_delayed else ""}</span>'
        )
        if head.terms_fragile:
            out.append('<span class="frag">terms fragile: exactly 8 runners</span>')
        out.append("</div>")
        out.append('<div class="wrap"><table><thead><tr>')
        out.append('<th>#</th><th class="l">Runner</th><th>Win</th><th>Place</th><th>P(place)</th><th>Fair</th>')
        for m in models:
            out.append(f'<th>drz·{e(m.replace("sportsbet_", "sb-"))}</th>')
        if show_bf:
            out.append('<th class="bf">BF place</th><th class="bf">drz·BF</th>')
        out.append('<th>Tier</th><th>Stake</th><th class="l">Note</th></tr></thead><tbody>')
        for v in sorted(race, key=lambda x: (x.drz if x.drz is not None else -1), reverse=True):
            cls = v.tier if v.tier in (TIER_BET, TIER_WATCH, TIER_SUSPECT) else "none"
            out.append(f'<tr class="{cls}">')
            out.append(f"<td>{v.saddlecloth or ''}</td><td class=\"l\">{e(v.horse_name)}</td>")
            out.append(f"<td>{_odds(v.win_price)}</td><td>{_odds(v.place_price)}</td>")
            out.append(f"<td>{_pct(v.p_place)}</td><td>{_odds(v.fair_place_odds)}</td>")
            for m in models:
                d = v.drz_by_model.get(m)
                out.append(f"<td>{d:.3f}</td>" if d is not None else "<td>—</td>")
            if show_bf:
                out.append(f'<td class="bf">{_pct(v.p_place_betfair)}</td>')
                dx = v.drz_exchange_place
                out.append(f'<td class="bf">{dx:.3f}</td>' if dx is not None else '<td class="bf">—</td>')
            out.append(f'<td class="tier">{e(v.tier)}</td>')
            out.append(f"<td>{f'${v.suggested_stake:.2f}' if v.suggested_stake else '—'}</td>")
            out.append(f'<td class="note">{e(_note(v))}</td></tr>')
        out.append("</tbody></table></div></section>")

    if skipped:
        groups: dict[str, list[str]] = {}
        for line in skipped:
            key = line.split(":", 1)[1].strip()[:60] if ":" in line else line[:60]
            groups.setdefault(key, []).append(line.split(":", 1)[0])
        out.append('<div class="skipped"><b>Not valued this sweep</b>')
        for key, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            sample = ", ".join(rs[:4]) + (f" +{len(rs) - 4} more" if len(rs) > 4 else "")
            out.append(f"<div>{len(rs)} · {e(key)} — {e(sample)}</div>")
        out.append("</div>")

    out.append(f"<script>{_SCRIPT}</script></body></html>")
    return "".join(out)


def write_report(path: Path, **kwargs) -> Path:
    """Render and write atomically, so a browser reload never sees half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(render_html(**kwargs), encoding="utf-8")
    tmp.replace(path)
    return path
