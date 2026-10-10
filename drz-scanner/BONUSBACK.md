# Bonus back if 2nd or 3rd — which horse to back

Sportsbet regularly runs a promotion on nominated races: back a horse with a
fixed-odds win bet, and if it runs **2nd or 3rd** your stake comes back as a
**bonus bet** (capped, typically $50). This module values every runner under
that offer, using the same probabilities as the place scanner, and picks the
one to back.

```
python -m app.bonusback_scan --promo "Randwick:7" --promo "Flemington:4"
python -m app.bonusback_scan --meeting Randwick --race 7
python -m app.bonusback_scan --all --objective lock --bonus-value 0.75
```

Windows: `bonusback.bat --promo "Randwick:7"`. Sportsbet's API carries no
promotion flag, so you name the races. Nothing here places a bet.

## The maths

Per $1 on a runner at Sportsbet win price `O`, with `p1 = P(win)`,
`p23 = P(2nd) + P(3rd)` and `r` the cash value of $1 of bonus bet:

| outcome | probability | profit |
|---|---|---|
| wins | `p1` | `O - 1` |
| 2nd or 3rd | `p23` | `-1 + r` |
| otherwise | `1 - p1 - p23` | `-1` |

```
EV          = p1*O + p23*r - 1
EV no promo = p1*O - 1               (minus Sportsbet's margin on this runner)
r*          = (1 - p1*O) / p23       (bonus conversion needed to break even)
```

The promotion pays `p23 * r`; the runner costs you its own margin
`1 - p1*O`. The best bet is where the first most exceeds the second. `r*`
ranks runners **without any assumption about r**: lower is better.

**Probabilities.** `p1` is the Betfair win-market midpoint (normalised)
when every runner has a reliable one, else Sportsbet's power de-vig —
the place scanner's priority order. `p2`, `p3` come from the same
discounted-Harville model (`lam` 0.71, `tau` 0.70). `p23` is the model's
P(top 3) after the place scanner's two measured corrections (win-probability
bucket and shrink-only price band), minus `p1`. The **conservative EV** is
the worst EV across every win model available, and the pick and the stake
use it.

**Hedges** (Betfair commission `c`; win lay `Lw`; "To Be Placed" lay `Lp`
on a **3-winner** market):

* *Lay the win*: lay `O/(Lw - c)` per $1. Every outcome returns the
  qualifying loss `(1-c)·O/(Lw-c) - 1`, plus `r` on 2nd/3rd.
* *Full lock*: lay win `(O - r)/(Lw - c)` and place `r/(Lp - c)` per $1.
  All three outcomes then pay
  `lock = (1-c)·((O - r)/(Lw - c) + r/(Lp - c)) - 1`. Positive means
  risk-free profit, provided you really convert the bonus at `r`. A 2-winner
  place market cannot do this, so no lock is shown for one.

**Bonus conversion `r`.** A bonus bet returns winnings only. Backing it at
`B` and laying at `L` locks `(1-c)(B-1)/(L-c)` per $1, which is about 0.70-0.80
for a $6-$15 runner with a tight Betfair spread. The scanner prints the best
conversion it can see in the races it scanned, so you can check your
`--bonus-value` against it.

**Stake.** Fractional Kelly (`KELLY_FRACTION`, a quarter by default) on the
three-outcome bet, using the conservative probabilities, capped at
`--max-stake` (the refund cap). In practice that usually means the full
promo stake.

**Objectives** (`--objective`): `ev` (default) picks the highest
conservative EV; `lock` picks the highest locked profit; `growth` picks the
highest expected log growth at the Kelly stake. No pick is made below
`--min-ev` (0.02).

## Evidence — `python -m app.bonusback_backtest`

Betfair ANZ history, last 12 months (Sep 2025 – Aug 2026, after the
calibration's fit window): **15,286 races with three placings, 151,873
runners**. "2nd or 3rd" is observed as *placed and did not win*.

### P(2nd or 3rd) is calibrated — measured

Log-loss: **0.4561** model, **0.4625** plain Harville.

| BSP win price | n | model | plain Harville | actual | actual/model |
|---|---|---|---|---|---|
| $1-2 | 1,785 | 0.271 | 0.374 | 0.276 | 1.02 |
| $2-3 | 5,611 | 0.358 | 0.458 | 0.372 | 1.04 |
| $3-5 | 15,855 | 0.355 | 0.430 | 0.352 | 0.99 |
| $5-9 | 25,523 | 0.306 | 0.332 | 0.304 | 0.99 |
| $9-15 | 25,340 | 0.241 | 0.228 | 0.242 | 1.00 |
| $15-21 | 15,256 | 0.192 | 0.162 | 0.190 | 0.99 |
| $21-51 | 28,792 | 0.129 | 0.099 | 0.135 | 1.05 |
| $51+ | 33,711 | 0.045 | 0.029 | 0.051 | 1.13 |

Plain Harville puts a $1-2 favourite's 2nd-or-3rd chance at 37%, when it is
actually 28%. A promo model built on it would pile onto short favourites
for a refund that comes a third less often than it claims. The discounted
model is within ±5% from $1 to $51.

### Which horse — real outcomes, simulated Sportsbet prices

Historical Sportsbet prices do not exist, so Sportsbet's win price is
simulated from BSP at a given overround `R`. Outcomes are real. One $1 promo
bet per race, `r = 0.70`, realised profit per $1 (± standard error):

| rule | power margin, R 1.12 | power, R 1.16 | power, R 1.20 | flat margin, R 1.16 |
|---|---|---|---|---|
| **model (max EV)** | **+0.185** ±.011 | **+0.165** ±.011 | **+0.144** ±.010 | **+0.135** ±.012 |
| favourite | +0.180 | +0.160 | +0.142 | +0.107 |
| second favourite | +0.159 | +0.131 | +0.105 | +0.119 |
| max P(2nd/3rd) | +0.186 | +0.161 | +0.137 | +0.135 |
| random runner | -0.024 | -0.066 | -0.104 | +0.001 |

How to read it:

1. **The promotion is worth about 14-19c per $1 if you back the right
   horse**, which is $7-9 on a $50 bet. On a random runner it is worth
   roughly nothing. The choice is what makes it valuable.
2. **No fixed rule of thumb is safe.** "Back the favourite" is near-optimal
   when Sportsbet piles its margin onto longshots (the power shape) and
   clearly worse when the margin is flat. "Back the most likely placegetter"
   has the opposite problem. The model reads the actual prices, so it is at
   or near the top in every regime. That is the case for using it.
3. Realised returns sat 1-2c above prediction across the board, which is
   within about 1.5 standard errors. There is no sign the model overstates
   the offer.

Re-run with your own assumptions:
`python -m app.bonusback_backtest --overround 1.18 --bonus-value 0.75 --margin-shape flat`.

## Caveats

* **The second part of the backtest uses simulated Sportsbet prices.**
  The live tool uses real ones. Every scan the place scanner stores keeps
  real Sportsbet prices, so this can be re-measured on real prices later.
* Promo terms vary (stake cap, eligible bet types, whether dead heats for
  3rd qualify, one bet per race or per customer). Check them. `--max-stake`
  sets the cap.
* `r` is the largest assumption. If you do not hedge your bonus bets, your
  real `r` is the EV of the bonus bet, which is usually 0.5-0.65 at a
  bookmaker's margin. Use `r*` to see how much the pick depends on it.
* The lock assumes you get the lay prices shown, and both Betfair markets
  settle on the same field. A late scratching can change the Betfair place
  market's terms.
* Win-price caps from the place scanner apply ($51 on Betfair
  probabilities, $9 on Sportsbet-only probabilities): runners beyond them
  are shown dimmed and never picked.
