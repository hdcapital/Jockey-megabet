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
* **Stake** — quarter-Kelly, capped. A suggestion for you to act on or
  ignore. The tool never places anything.
* **TERMS FRAGILE** — exactly 8 active runners. One scratching before you
  bet turns this into a 2-place race and every number above changes.

Add `--show-all` to see the whole field instead of just WATCH and above.

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

Without credentials, the scanner runs on Sportsbet alone and says so.

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
