# QUICKSTART

## Windows, one click

1. Unzip `drz-scanner.zip` somewhere you can find again, e.g.
   `C:\drz-scanner`.
2. Install **Python 3.11 or newer** from <https://www.python.org/downloads/>
   if you have not already. Tick **"Add Python to PATH"** during setup.
3. Double-click **`drz.bat`**.

The first run installs the requirements (a minute or two), checks that
`data\calibration.json` is present and less than 35 days old, refits it from
Betfair's free historical files if it is not, and then starts scanning.
Press **Ctrl-C** to stop. The window stays open so you can read the last
table.

The other three files:

| File | What it does |
|---|---|
| `drz-once.bat` | One scan, then stop |
| `calibrate.bat` | Refit `lam`/`tau` (and the Sportsbet beta) from real results |
| `backtest.bat` | Settle stored signals; report ROI, calibration and closing-line value |

## Running it once in the morning: `daycard.bat`

Double-click **`daycard.bat`** and it scores the entire Australian day
card in one pass — every race, every runner, no time horizon — writes it
to `data\daycard.html`, opens it in your browser, and exits. Takes about
two minutes for a normal day.

**Read the yellow note at the top of that page before acting on it.** A
fixed-odds place bet locks the price you take, so a genuine edge at a
morning price is real — but three things are different in the morning:

1. **Prices move.** Morning place prices are often nowhere near the jump
   price. The score is a statement about *this* price at *this* moment.
2. **Fields change.** A scratching later in the day turns an 8-runner race
   into a 7-runner race that pays two places, not three. Those races are
   flagged **terms fragile**. Sportsbet still pays three on a bet already
   struck, but with deductions.
3. **Betfair is thin in the morning.** The BET tier needs the exchange to
   agree, and most morning place markets have too little in them to
   count. Expect most rows to say `no exchange confirmation` and top out
   at WATCH. That is the safety catch, not a fault — the evidence behind it
   (model-only signals returning 80–88c in the dollar) does not go away
   because it is morning.

So the honest description of the day card is: a ranked list of where the
morning place prices look most generous, with the checks that *can* be done
at that hour done. If you want the checks that need the live market —
exchange agreement, fresh prices, the final field — that is what `drz.bat`
is for.

## The live report — the easy way to read it

After the first sweep, `drz.bat` opens **`data\latest.html`** in your
browser. It is rewritten after every sweep and reloads itself every 45
seconds: every race in jump order, every runner, colour-coded tiers, a
countdown to each jump, and the reason any row was held back. Leave that
tab open and ignore the black window.

If it did not open, double-click `data\latest.html` yourself, or run
`py -m app.drz --loop --open`. Add `--no-report` to skip writing it.

To look at one race on its own in the terminal:

```
drz-once.bat --meeting Randwick --race 3 --show-all
```

## Reading the table

```
Fixtureville R6  ·  jump in 30m00s  ·  8 runners, 3 places  ·  TERMS FRAGILE
  #  Runner          Win   Place   P(place)   Fair   drz·sb-power      EV   Tier    Stake
  1  Ten Bagger     2.60    1.72      72.1%   1.39          1.241  +24.1%   BET    $10.00
  3  Third Rail     6.00    2.55      46.8%   2.14          1.194  +19.4%   BET    $10.00
  4  Fourth Estate  8.50    2.60      37.5%   2.66          0.976   -2.4%   —           —
```

* **P(place)** — the model's probability that this runner is paid a place
  dividend, given the field as it stands right now.
* **Fair** — the place price that probability is worth (`1 / P(place)`).
  Compare it to the **Place** column: the gap is the whole story.
* **drz** — expected return per $1. One column per available win model.
  `1.241` means $1.24 back for every $1 staked, in expectation, *if the
  model is right*.
* **Tier** — BET / WATCH / SUSPECT, or `—` for "nothing here".
* **Note** — why a row is not BET. The tokens worth knowing:
  * `beyond_price_cap` — the win price is past the range where this model's
    probabilities have been checked ($51 on exchange prices, $9 on Sportsbet
    prices, $21 on a delayed exchange key). The row is still valued and
    stored; it just cannot be a BET.
  * `no_exchange_confirmation` — no Betfair "To Be Placed" market with
    matching terms agreed with the model. Measured over 436k runners,
    model-only signals returned 0.80-0.88 per $1, so the exchange has to
    sign off.
  * `band x0.971` — the win-price band correction trimmed this probability.
    It only ever trims, never inflates.
* **Stake** — quarter-Kelly, capped. A suggestion for you to act on or
  ignore. The tool never places anything.
* **TERMS FRAGILE** — exactly 8 active runners. One scratching before you
  bet turns this into a 2-place race and every number above changes.

Add `--show-all` to see the whole field instead of just WATCH and above.

## Why some long-priced runners say "beyond_price_cap"

The maximum win price depends on where the probabilities came from:

| Win model | Cap |
|---|---|
| Betfair exchange | $51 |
| Betfair, delayed app key | $21 |
| Sportsbet (either model) | $9 |

Measured over 44,856 races, the place model is calibrated from about $3 to
$51 on exchange probabilities and collapses above $101. Bookmaker-derived
probabilities carry the longshot overround and are held at $9.

`--max-win-odds` can only **lower** these. Raising one needs `--i-know`, and
the log will tell you that you are acting on probabilities nothing has
validated.

## The banner

```
BET signals are UNPROVEN. The backtester holds 0 settled BET signals...
```

This stays up until `backtest.bat` has settled 300 BET signals. The place
model is calibrated against 44,856 real races; the *betting rule built on
top of it* has no track record yet. Until it does, treat BET rows as
hypotheses.

## Most days you will see no BET rows

That is the expected, correct output. Sportsbet's fixed place prices carry a
big margin. See the README for why a tool that found bets every day would be
a tool with a bug.

## Optional: Betfair

Copy `.env.example` to `.env` and fill in:

```
BETFAIR_APP_KEY=your_key
BETFAIR_USERNAME=your_username
BETFAIR_PASSWORD=your_password
```

This adds a second, independent win model and a place-market cross-check. A
**delayed** application key works fine — the tool notices, switches to a
spread-only reliability gate, tags those rows `betfair_delayed`, and will not
let a delayed price on its own make something a BET in the last five minutes
before a jump.

Without credentials the scanner runs on Sportsbet alone, says so at startup,
and — because a BET needs the exchange's place market to agree — **cannot
show a BET row at all**. Everything tops out at WATCH until you add them.

## What the first sweep looks like

The first full sweep of the day fetches every Australian race inside three
hours of its jump (~30-60 racecards, about a minute at the polite request
rate) and prints one block per race. Then it settles into the cadence: a
full sweep every three minutes, and only the races inside ten minutes of
the jump every ~40 seconds. International and New Zealand meetings are
listed by Sportsbet but not valued — the calibration is Australian-only,
and the log records each class of meeting the first time it appears.

## The morning after a loop

`data\logs\drz.log` has every sweep, every skipped race and every failure,
one file per day, two weeks kept. `backtest.bat` settles whatever resolved
and reports. Runners scratched after you saw them show as **void** (stake
returned), not as losses.

## If a scan fails

```
Scan failed: sportsbet unavailable (https://...): ProxyError: 403 Forbidden
Nothing is displayed rather than anything invented.
```

That is the tool working. It will not show you numbers it could not fetch.
The usual causes:

* **You are outside Australia,** or on a VPN/proxy Sportsbet blocks. Run it
  from an ordinary Australian home connection.
* **A schema change.** The message names the archived payload under
  `data\raw\`; that file is everything needed to fix the parser.

## Where things are kept

```
data\drz.db            every price and valuation, ever (SQLite)
data\raw\              every raw response, hashed and timestamped
data\calibration.json  the fitted parameters and their provenance
data\history\          cached Betfair historical files (~80 MB)
```
