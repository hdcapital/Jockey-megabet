# drz-scanner

A locally-run Windows tool that pulls today's Australian thoroughbred
schedule from Sportsbet and computes a **Dr Z place-value score** for every
active runner in every open race.

```
drz = P(place) x place price          (expected return per $1 staked)
ev  = drz - 1
```

It displays. It does not bet. There is no bet-placement code in this
repository and none is planned.

---

## What is measured fact, and what is not

This section is the most important one in the file. Everything below is
either a number that was measured, or a statement clearly labelled as an
assumption.

### Measured — 44,856 Australian thoroughbred races, Jan 2024 – Aug 2026

Source: the free Betfair Australia historical files
(`betfair-datascientists` ANZ Thoroughbreds), filtered to thoroughbreds
outside New Zealand where every runner was priced, exactly one runner won,
two or three runners placed including the winner, and the field was at least
five. Fitted on everything before the last twelve months, measured on the
last twelve. Reproduce all of it with `python -m app.calibrate`, or check it
with `python -m pytest -m data`.

| Quantity | Value |
|---|---|
| Model version stored with every row | **1.1** |
| Discounted-Harville exponents `lam` / `tau` | **0.703 / 0.708** (shipped as 0.71 / 0.70) |
| Out-of-sample log-loss of P(place), this model | **0.4999** |
| Out-of-sample log-loss, plain Harville | **0.5062** |
| Out-of-sample log-loss, place-BSP implied | **0.4992** |
| Conditional-logit exponent on Betfair BSP win probabilities | **1.0125** |
| Plain Harville's top place bucket: predicted vs actual | **0.94 vs 0.86** |
| This model, runners with win probability > 0.5: predicted vs actual place | **0.89 vs 0.86** |
| Global logistic recalibration of P(place) | slope **1.00**, intercept **0.00** — finds nothing |

### Measured — how far out the probabilities hold

Same sample, win probabilities from Betfair prices, `lam` 0.71 / `tau` 0.70.
Actual place rate divided by modelled place probability, by win-price band:

| Band | (1,2] | (2,3] | (3,5] | (5,9] | (9,15] | (15,21] | (21,31] | (31,51] | (51,101] | (101+] |
|---|---|---|---|---|---|---|---|---|---|---|
| BSP | 0.97 | 0.98 | 0.99 | 1.00 | 1.03 | 1.02 | 1.00 | 1.00 | 0.99 | **0.84** |
| Live-style | 0.97 | 0.98 | 0.99 | 1.00 | 1.03 | 1.02 | 1.01 | 1.00 | 0.96 | **0.84** |

"Live-style" repeats the measurement on the prices actually showing at the
scheduled off — the probability-space midpoint of best back and best lay,
counting only runners whose own relative spread is within 10%. It is the
same picture, which is what makes the first row usable for a live tool.

So under Betfair probabilities the model is **calibrated from about $3 to
$51**, runs 2-3% hot below $3, and is badly hot above $101. That is the
entire justification for the win-price caps below.

**This says nothing about whether an edge exists at any price.** It says the
probabilities are trustworthy in that range. Whether a trustworthy
probability meets a generous enough place price is a separate question that
only the backtester can answer, and it has not answered it yet.

Three consequences follow directly, and they shape the whole tool:

1. **Plain Harville is badly wrong at the top.** It tells you a short-priced
   favourite places 94% of the time when it actually places 86% of the time.
   Bet into that and you are buying an 8-point illusion. The discounted form
   is not a refinement; it is the difference between a usable model and an
   unusable one.
2. **Betfair's win prices need no favourite-longshot correction.** The fitted
   exponent is 1.01. Where the exchange is available, its normalised
   midpoints are used as-is.
3. **The exchange's own place market is still slightly sharper than this
   model** (0.4992 against 0.4999). The model is not better informed than the
   market. It is a way of pricing a *bookmaker's* place market, which is a
   different and much softer thing.

Also measured, on the same data: betting every runner where
`model_p x place_BSP > 1.0` returned **0.98** gross, and `> 1.2` returned
**0.90**. Disagreeing harder with the exchange lost more money. That is why
a large gap against the Betfair place market is shown as a **warning** in
this tool, never as an edge.

### Assumptions — not measured, and labelled as such in the code

* **The Sportsbet place-price field name.** The parser reads the place price
  from the `priceCode=L` entry, trying `placePrice` first. That field name
  comes from a fixture built to mirror a live-verified racecard, but it was
  **not** itself re-verified against a live response by this build (no
  network access to sportsbet.com.au from the build environment). The parser
  raises `SchemaMismatchError` naming the field names it tried rather than
  guessing. See BUILD_STATUS.md.
* **What `MDP` and `TMD` mean.** Both codes were observed live carrying win
  prices that persist after a runner is scratched, so neither is a live
  fixed price. Sportsbet's rules describe tote-derivative products (Top
  Tote, Top Tote Plus, Mid Div) shown as *indicative* odds, and the letters
  are suggestive — but **the mapping from code to product has not been
  established**, so this tool does not claim it. Any non-`L` code carrying
  prices is captured as `price_type="tote_indicative"`, recorded verbatim,
  never used as a live price, and never given a stake suggestion.
* **Place terms by field size** (3 dividends at 8+, 2 at 5-7, none below 5).
  This is Sportsbet's published structure. The terms are *locked at bet
  time*, so a race that has exactly 8 active runners now is flagged
  `terms_fragile`: one scratching before you bet turns it into a two-place
  race with completely different value.
* **Deductions.** Late-scratching deductions to a fixed-odds payout are
  recorded as `unknown`. The backtester says so, and the ROI it reports is
  therefore an upper bound.
* **Everything about whether the BET signal makes money.** Nothing.
  See below.

### The banner you will see, and why

Until the backtester holds **300 settled BET signals**, every scan prints an
UNPROVEN banner. The place model is calibrated; the *signal* is not. A model
that predicts place probability well and a rule that finds profitable bets
are separate claims, and only the first has evidence behind it.

---

## What you should expect to see

**Most days, few or no BET rows. That is the correct output.**

Sportsbet's fixed-odds place prices carry a large margin — typically enough
that a well-calibrated model prices essentially the whole field below 1.00.
A scan that returns nothing is a scan that worked. A tool that produced a
dozen confident bets every afternoon would be a tool with a bug in it,
almost certainly in its win probabilities.

This is also why proportional de-vig is refused outright. Dividing every
implied probability by the same overround leaves the longshot margin sitting
inside the probabilities, and a place model fed those numbers will report
large, entirely fictitious edges on outsiders — the exact rows that look
most exciting and are most wrong.

---

## How it works

### 1. Schedule

`AllRacing/{date}` for the **Australian** calendar date (AEST/AEDT, not
UTC — otherwise every Australian morning fetches yesterday's card),
thoroughbreds only (`className` beginning "Horses"), open races only. A race is open when its `statusCode` is `A`, its betting
status says nothing about being resolved, and it carries no `result`.

Races that have resolved since an earlier scan get a separate settlement
pass, which writes their outcome so stored signals can be settled. Without
it the backtester would have nothing to work on, ever.

### 2. Win probabilities

In priority order; the one used is stored with every row as `win_model`.

| Model | What it is | Status |
|---|---|---|
| `betfair` | Exchange win midpoints under spread/liquidity gates, normalised | Exponent measured at 1.01 — no correction applied |
| `sportsbet_beta` | `p_i ∝ raw_i^B`, with `B` fitted by conditional-logit ML on **your own stored** Sportsbet prices joined to results | Used once ≥ 1,500 races are joined |
| `sportsbet_power` | Power de-vig: solve `k` per race so `sum(raw_i^k) = 1` | Default; rows labelled **UNCALIBRATED** |

Nobody sells historical Sportsbet prices, which is why `sportsbet_beta`
cannot exist on day one. Every scan stores `runner_prices` whether or not it
valued anything, so the fit becomes possible after you have been running for
a while. `python -m app.calibrate` performs the join (date + track + race
number + TAB number, with the runner name as a check) and records every
unmatched race rather than guessing.

### 3. Place probabilities — discounted Harville (Lo / Bacon-Shone)

With `a = q^lam`, `b = q^tau`, `A = sum(a)`, `B = sum(b)`:

```
P(j 1st)                = q_j
P(i 2nd | j 1st)        = a_i / (A - a_j)
P(k 3rd | j 1st, i 2nd) = b_k / (B - b_j - b_i)
```

Vectorised as `M[j,i] = q_j a_i/(A-a_j)` with a zeroed diagonal,
`T = M/(B-b_j-b_i)`, and
`third_k = b_k (T.sum() - T[k,:].sum() - T[:,k].sum())`. The test suite
checks this against brute-force enumeration of every finishing order for
fields up to seven runners, at five different `(lam, tau)` pairs, for both
two and three places — and checks that `lam = tau = 1` reproduces closed-form
Harville exactly.

Each race's place probabilities sum to its number of dividends, to machine
precision.

Two deliberate exceptions, applied in this order and neither renormalised: after the model runs, an additive correction by
win-probability bucket is applied (the `win_prob_bucket_correction` block in
`calibration.json`). It exists because the model over-predicts the place
chance of runners with a win probability above 0.5 by about three points —
0.892 predicted against 0.859 actual. The correction is only written into
the file when it improves out-of-sample log-loss, which it did, marginally
(0.499882 → 0.499708). Because it is an additive per-runner shift it leaves
a race's corrected probabilities a percent or two short of the dividend
count; renormalising that away would undo the improvement, so it is left
alone and documented here instead.

Second, a **shrink-only win-price band correction**. `p_place` is multiplied
by `min(1, band_ratio)` for the runner's price band, from the table above.
It trims short favourites by 1-3% and is a no-op everywhere the model is
already flat or cold. A band whose ratio exceeds 1 is *never* applied: scaling
probabilities **up** on that evidence would manufacture edges out of sampling
noise in exactly the bands where prices are longest and payoffs most skewed.
Both the raw and corrected probabilities are stored, with the factor used.

The band table was measured on exchange probabilities, so it is applied only
to exchange-priced rows. Sportsbet-priced rows get no band correction until
the same table can be built from our own stored prices with at least 300
runners per band — at which point `app.calibrate` will report it, again
without changing any configured value.

### 4. Score and tiers

The **maximum win price depends on the win model**, because the cap is a
statement about where that model's probabilities are trustworthy:

| Win model | Cap | Why |
|---|---|---|
| `betfair` | **51.0** | exchange probabilities hold to about $51 (table above) |
| `betfair`, delayed key | **21.0** | no matched volume to corroborate a long price |
| `sportsbet_beta` | **9.0** | bookmaker probabilities carry the longshot overround |
| `sportsbet_power` | **9.0** | same, and uncalibrated besides |

The price tested is the Sportsbet **live win price**. The old flat 9.0 was
only ever justified for Sportsbet-derived probabilities; applying it to
exchange-derived ones threw away sound runners between $9 and $51.

`--max-win-odds` may only **lower** these. Raising one needs `--i-know`,
which says in the log that you are betting on probabilities nothing has
validated. `python -m app.calibrate` re-derives the recommended cap from the
live-style table and warns if it lands below what you have configured — it
never raises your setting for you.

A runner beyond the cap is still valued, still stored and still shown; the
cap gates the **BET tier only**, holding such a row at WATCH with the reason
`beyond_price_cap`. That is deliberate: the backtester needs those rows in
order to test the cap itself.

| Tier | Rule |
|---|---|
| **BET** | `drz >= DRZ_MIN` (1.10) under **every** available model, win price within the cap for its model, **a Betfair "To Be Placed" market with matching terms agreeing** (`drz >= DRZ_MIN` on the exchange's own place probability), price under 60 s old, quality HIGH, win model not UNCALIBRATED unless `--allow-uncalibrated` |
| **WATCH** | `1.03 <= drz < 1.10`, or the BET bar cleared under only one of several models |
| **SUSPECT** | `drz > 1.30` — **never** BET. Logged and archived. On a market this heavily margined, a score that high is a stale price, a scratching in flight, or a parse error |

Staking, display only: quarter-Kelly of `f = (drz-1)/(price-1)`, capped at 1%
of `BANKROLL` per bet and 2% per race, floored to the cent so the cap cannot
be exceeded by rounding. Tote-indicative rows never get a stake.

### 5. Betfair place market — now a requirement, not just a second opinion

Where a "To Be Placed" market exists **whose number of winners equals the
place terms being valued**, its midpoint gives an independent place
probability. A mismatch in terms means no second opinion rather than a
misleading one.

**A row can only reach BET if that market agrees**, i.e. the exchange's own
place probability also clears `DRZ_MIN` against the Sportsbet place price.
Model-only rows are held at WATCH with the reason `no_exchange_confirmation`.

The evidence: over 436,000 runners, model-only signals priced against
exchange place prices less a 10-15% margin returned **0.80-0.88 per $1**. The
model is fitted to win prices; the exchange place market is the only check on
it that does not come from it. When the two disagree, the exchange has been
right.

`--no-exchange-confirmation` removes the requirement and says so in the log.

A **delayed application key** is supported: the adapter detects the missing
matched volume, falls back to a tighter spread-only gate, tags those rows
`betfair_delayed`, and refuses to let a delayed price alone promote a row to
BET inside the final five minutes before the jump.

---

## Install and run

See QUICKSTART.md. Short version on Windows: double-click **`drz.bat`**.

```
python -m app.drz [--meeting NAME] [--race N] [--min-drz X] [--max-win-odds X]
                  [--show-all] [--no-db] [--loop] [--date YYYY-MM-DD]
                  [--allow-uncalibrated]
python -m app.calibrate      # refit lam/tau (and beta) from real results
python -m app.backtest       # settle stored signals, report ROI/calibration/CLV
python -m pytest             # the full suite
python -m pytest -m data     # + the calibration acceptance test (needs the CSVs)
python -m pytest -m live     # + live Sportsbet checks (needs AU network access)
```

In `--loop`, the full schedule is swept every 180 s and races within ten
minutes of the jump refresh every ~40 s, all through the same per-host
throttle. Do not lower `HTTP_MIN_REQUEST_INTERVAL_SECONDS`.

---

## Rules this tool keeps

* **Real data only.** Nothing is ever fabricated. When a source cannot be
  reached or a payload does not parse, the scanner fails loudly with the
  path to the archived raw response and displays nothing.
* **Every response is archived** to `data/raw/` before it is parsed, so any
  displayed number can be traced back to the exact bytes it came from.
* **Geo and bot controls are never bypassed.** If Sportsbet will not serve
  this machine, the answer is to run it from a machine Sportsbet will serve.
* **No automated betting.**
* **No backfill.** The backtester uses only observations captured live. A
  price you did not see at the time is not evidence about a signal you would
  have taken at the time.
* **No inferred dead heats.** The race-level placings string is a finishing
  order and cannot express a dead heat, so one is never guessed from its
  length. Dead heats are resolved from per-runner finishing positions; where
  those are missing, the first N placings are paid in full and the row is
  marked `settlement_basis = "placings_string"` so the report can say which
  evidence it rests on.

## Layout

```
app/place_model.py    discounted Harville, vectorised + a loop oracle
app/devig.py          power / beta / (reference) proportional
app/winprob.py        model priority and the UNCALIBRATED label
app/scoring.py        Dr Z, tiers, Kelly staking
app/engine.py         one racecard -> scored rows (no I/O, so it is testable)
app/calibrate.py      the lam/tau and beta fits
app/history.py        Betfair historical files: download, validate, shape
app/sources/          Sportsbet and Betfair adapters
app/database/         SQLAlchemy models + append-only repository
data/calibration.json every fitted parameter, with its provenance
```

## Credits

The HTTP layer (throttle, retry, raw archival), the Sportsbet endpoint map
and racecard shape, the Betfair adapter and the repository pattern are
carried over from the Jockey-Megabet scanner, where they were exercised
against live Sportsbet and Betfair endpoints on 2026-08-22.
