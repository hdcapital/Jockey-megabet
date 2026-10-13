# Race Value Scanner: how to use it

The scanner compares **Sportsbet's fixed odds** with **Betfair's exchange
prices** for every horse in races jumping in the next 20 minutes. It treats
Betfair as the true price. When Sportsbet pays more than Betfair says a horse
is worth, on two scans in a row, it shows it as a **bet worth a look**. It
then follows each one to the result so you can check whether the edges are
real before betting serious money.

It never places a bet. You decide.

## What you need

* A Windows PC or a Mac on an **Australian internet connection**. Sportsbet
  blocks overseas connections, so turn off any VPN.
* A **Betfair account** and a free **application key**: log in at
  <https://developer.betfair.com> and go to *My Account → API keys*. Use the
  **Live** key if you can. The Delayed key works, but its prices can be
  minutes old, so the scanner won't confirm any bets with it.

## Start it

1. Unzip the folder anywhere (Desktop is fine).
2. Double-click **START.bat** (Mac: right-click **start.command** → *Open*).
   * No Python yet? It opens the download page. Install it, ticking
     **"Add python.exe to PATH"**, then double-click START.bat again.
   * The first run installs what it needs (about a minute).
3. **Already have a `.env` with your Betfair details** (as in your other
   projects)? Copy it into this folder, next to START.bat. It's used as is
   and step 4 is skipped. It needs `BETFAIR_APP_KEY`, `BETFAIR_USERNAME` and
   `BETFAIR_PASSWORD`.
4. Otherwise, the first time it asks for your Betfair **application key**, **username**
   and **password**, plus (optionally) your betting bank and commission rate.
   It tests the login straight away and tells you plainly if something's
   wrong.
5. Choose **1** from the menu. Leave the window open; it updates every
   minute. Press **Ctrl+C** to stop.

## The menu

| Choice | What it does |
|---|---|
| 1 | Live scanner: races in the next 20 minutes, updated every minute |
| 2 | Results report: are the edges real? |
| 3 | Set up or change your Betfair login, bank and commission |
| 4 | Test your Betfair login |
| 5 | Open the results spreadsheet (every flagged bet and how it went) |
| 6 | Open the settings file |
| 7 | Play the alert sound |

## Reading the screen

**BETS WORTH A LOOK** is the part to act on:

```
▶ BACK  Horse A (#1)  to WIN   Randwick R1 · jumps 4m
     Sportsbet $3.40   fair $2.97   edge +14.6%   stake $15   lock-in +5.3%
```

* **fair**: the price Betfair says the horse should be.
* **edge**: how much more Sportsbet pays than that. +10% means you'd get
  back $1.10 for every $1 bet, on average over many bets.
* **stake**: a quarter-Kelly stake from the bank you entered (or as a % of
  your bank if you didn't enter one).
* **lock-in**: shown when backing at Sportsbet and laying the same horse on
  Betfair straight away guarantees a profit.
* **to PLACE (top 2)**: a place bet, which pays if the horse runs in the top
  2 (or top 3 in fields of 8 or more).

When a horse becomes a bet worth a look you hear a short **chime** (once
per bet) and it's tagged **NEW** on screen. Change it with `VALUE_SOUND`:
`confirmed` (default), `watching` (also chime the first time a horse shows
value) or `off`. You can also point `VALUE_SOUND_FILE` at your own .wav,
or start with `--no-sound`.

**Watching** lists horses showing value for the first time. They're
confirmed if the value is still there one scan later. A gap that shows up
once and disappears is usually a price Sportsbet hasn't updated yet, or a
scratching that's still going through.

The **table** below shows every horse compared, best edge first. Rows with
a **flag** are hidden unless you start with `--all`, because Betfair's price
for them can't be trusted yet. The key under the table says what each flag
means. Rows like that never become bets.

When a flagged bet's race has run you'll see a line like
`Result: Horse A (Randwick R1 win at $3.40) WON · vs Betfair's closing price +9.1%`.

## Are the edges real? (menu 2)

For each bet worth a look, the scanner records:

* **the price when it was flagged**,
* **Betfair's last price before the jump** (the "closing price"),
* **Betfair's Starting Price (BSP)**, and
* **the result**.

The report answers in plain English. The key number is **"beat close"**: how
often the Sportsbet price was better than Betfair's final price. Above about
55% over 100+ bets is a good sign the edges are real. Profit and loss takes
far longer to mean anything because luck dominates it. Everything is in
`data/value_results.csv`, which opens in Excel.

Leave the scanner running at the jump for the closing-price numbers to be
accurate. The report warns you if the closing prices were taken early.

## Settings (menu 6)

The settings file has a short explanation above each value. The ones you're
most likely to change:

| Setting | Default | Meaning |
|---|---|---|
| `VALUE_MIN_EV` | 0.02 | Smallest edge that counts (2%) |
| `VALUE_CONFIRM_SCANS` | 2 | Scans in a row before it's a bet worth a look |
| `VALUE_BANKROLL` | 0 | Your bank in $, for stake suggestions |
| `VALUE_KELLY_FRACTION` | 0.25 | Fraction of full Kelly to suggest |
| `VALUE_WINDOW_MINUTES` | 20 | How far ahead to look |
| `VALUE_INCLUDE_PLACES` | true | Compare place prices too |
| `BETFAIR_COMMISSION` | 0.08 | Your Betfair commission (lock-in column only) |
| `VALUE_SOUND` | confirmed | Chime for new bets: confirmed / watching / off |

## If something goes wrong

The screen says what's wrong and what to do in a red box. The common cases:

* **"Can't reach Sportsbet"**: you're not on an Australian connection, or a
  VPN is on.
* **"Betfair refused the login"**: re-enter it with menu 3. The box shows
  Betfair's own reason, for example a wrong password or the account needing
  attention on the Betfair website. After a refusal the scanner waits 10
  minutes before trying again, so Betfair doesn't lock the account.
* **"DELAYED key"**: you're using Betfair's delayed application key. Switch
  to the Live key (menu 3).

Detailed logs are in `data/logs/`, one file per day.

## Honest limits

* The edge is only as right as Betfair. Close to the jump on busy races
  Betfair is very good. On small country races 20 minutes out it's thinner,
  and those rows are usually flagged.
* Sportsbet limits accounts that keep beating its prices. A real edge doesn't
  mean you'll keep being allowed to bet it.
* Prices move fast in the last minutes. An edge on screen can be gone by the
  time you place the bet.
* Not modelled: Sportsbet promotions, and the fine print of dead-heat and
  late-scratching deductions.
