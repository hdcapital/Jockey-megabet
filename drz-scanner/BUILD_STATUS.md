# BUILD STATUS

Built 2026-09-18. Python 3.11.15, Linux build environment.

**Test suite: 342 passed, 3 skipped** (`python -m pytest`). The three skips
are the live Sportsbet tests, which cannot run here — see below.

---

## Verified live, against real data

### The calibration, end to end — fully reproduced

The Betfair Data Scientists historical files **were reachable** from this
build (via the GitHub raw mirror; the `betfair-datascientists.github.io`
host itself is blocked by the egress proxy, and `app/history.py` falls back
to the mirror automatically). So the entire parameter fit is not a claim —
it was run, and it reproduces your independent result:

```
$ python -m app.calibrate --start 2024-01-01 --end 2026-08-31 --lam 0.71 --tau 0.70

  races used            44,856 (2024-01-01 .. 2026-08-31)
  lam / tau             0.7100 / 0.7000   (pinned; free fit 0.7032/0.7076)
  BSP win exponent      1.0125   (1.0 = no FLB correction needed)
  out-of-sample races   16,737
  log-loss  model       0.4999
  log-loss  + bucket    0.4997   (adopted)
  log-loss  Harville    0.5062
  log-loss  place BSP   0.4992   (the exchange's own opinion)
```

| Acceptance target | Yours | Reproduced here | |
|---|---|---|---|
| Races after the documented filters | 44,856 | **44,856** | exact |
| `lam` | 0.71 ± 0.04 | **0.7032** | ok |
| `tau` | 0.70 ± 0.04 | **0.7076** | ok |
| Surface flat to 0.76 / 0.62 | flat | **flat** (Δ log-loss < 5e-4) | ok |
| Out-of-sample log-loss, model | ~0.4999 | **0.49988** | ok |
| Out-of-sample log-loss, Harville | ~0.5062 | **0.50618** | ok |
| Harville top bucket, predicted vs actual | ~0.94 vs ~0.86 | **0.943 vs 0.858** | ok |
| Place-BSP implied log-loss | ~0.4991 | **0.49919** | ok |
| BSP win exponent | ~1.01 | **1.0125** | ok |
| Residual at win prob > 0.5 | 0.89 vs 0.86 | **0.892 vs 0.859** | ok |
| Global logistic recalibration | slope 1.00, intercept 0.00 | **1.001 / 0.0007** | ok |

All of the above are asserted in `tests/test_calibration_acceptance.py`
(marked `data`), which passes: **12 passed in 30s**.

**Bucket correction:** the win-probability-bucket correction *was* tested and
*was* adopted — it improved out-of-sample log-loss from 0.499882 to 0.499708.
The improvement is small but real and repeatable, and the q>0.5 bucket's
fitted delta is -0.027, matching the ~3-point over-prediction you measured.
A global logistic recalibration was also tested and correctly found nothing.

### The exchange-disagreement evidence — reproduced

On the same 44,856 races, flat-staking every runner at place BSP:

| Filter | Yours | Reproduced here | n |
|---|---|---|---|
| `model_p × place_BSP > 1.0` | 0.98 | **0.9802** | 211,379 |
| `model_p × place_BSP > 1.2` | 0.90 | **0.8977** | 33,278 |

So the tool shows a large model-vs-exchange gap as a warning, not an edge.

### A data-format issue worth knowing about

`LOCAL_MEETING_DATE` is **not** consistently formatted across the published
files: most months use ISO (`2025-12-01`), a handful use day-first
(`13/04/2025`). `app/history.parse_meeting_dates` handles both explicitly
(day-first is established from the data: values with a first field above 12
exist, values with a second field above 12 do not) and turns anything else
into NaT, which the filters then drop. A naive `pd.to_datetime` raises, and
a naive `errors="coerce"` would have silently dropped those meetings.

---

## Win-price band calibration (verified on real data)

The band table the win-price caps rest on was computed here, on the same
44,856 races, and reproduces the independent result:

| Band | (1,2] | (2,3] | (3,5] | (5,9] | (9,15] | (15,21] | (21,31] | (31,51] | (51,101] | (101+] |
|---|---|---|---|---|---|---|---|---|---|---|
| **BSP, mine** | 0.97 | 0.98 | 0.99 | 1.00 | 1.03 | 1.02 | 1.00 | 1.00 | 0.99 | **0.84** |
| BSP, independent | 0.97 | 0.98 | 0.99 | 1.00 | 1.03 | 1.02 | 1.00 | 1.00 | 0.99 | 0.84 |
| **live-style, mine** | 0.96 | 0.99 | 0.99 | 1.00 | 1.03 | 1.02 | 1.02 | 0.99 | 0.97 | **0.85** |
| live-style, independent | 0.97 | 0.98 | 0.99 | 1.00 | 1.03 | 1.02 | 1.00 | 1.00 | 0.96 | 0.83 |

The BSP row matches to two decimals in every band. The live-style row needed
one interpretation settled: **the 10% spread limit is a runner-level gate.**
Gating whole races on it instead (requiring every runner in the book to be
tight) keeps only 28% of races, and that selection moves the longshot band to
0.90. Filtering per runner while still normalising over the whole book — the
reading that matches the wording, and what the scanner itself does — gives
0.85 against the independent 0.83. `app/calibrate.py` implements the
runner-level reading and says so in `live_style_bands`.

On the **training window** (what `app.calibrate` actually fits on) the
live-style table puts `(51,101]` at 0.96, so the derived recommended cap is
**51.0** — exactly the shipped `MAX_WIN_PRICE_BETFAIR`. It is reported, not
applied: a recommendation below the configured value produces a warning and
nothing else.

## NOT verified live — and exactly why

### Sportsbet is unreachable from this build environment

```
$ curl https://www.sportsbet.com.au/apigw/sportsbook-racing/.../AllRacing/2026-09-18
curl: (56) CONNECT tunnel failed, response 403
  www.sportsbet.com.au:443 — connect_rejected (the egress proxy denied the
  CONNECT (organization policy) or could not reach the destination)
```

The same 403 applies to `api.betfair.com` and `identitysso.betfair.com`.
This is an organisation egress policy on the build host, not a Sportsbet
geo-block, and it is not something to work around — the scanner is built to
be run from an ordinary Australian machine.

The scanner's own failure path was exercised and behaves correctly:

```
$ python -m app.drz --no-db
Scan failed: sportsbet unavailable (https://www.sportsbet.com.au/...):
ProxyError: 403 Forbidden
Nothing is displayed rather than anything invented. The raw payload path
above is the evidence.
$ echo $?
2
```

### Consequently unverified

**1. The place-price field name — the most important open item.**

The parser reads the place price from the `priceCode=L` entry, trying
`placePrice`, `placePriceDecimal`, `returnPlace`, `placeOdds`, `place`,
and a nested `{"decimal": ...}` form. `placePrice` comes from the
live-shape racecard fixture inherited from the Jockey-Megabet scanner. That
fixture's *structure* (top-level `markets`, "Win or Place", `prices` keyed
by `priceCode`, scratched runners as `statusCode "S"` with the L price
withdrawn) **was** verified live on 2026-08-22 — but that scanner only ever
needed the **win** price, so the BUILD_STATUS of that branch records only
`prices[priceCode=L].winPrice` as verified. **The place field name is
therefore a well-informed candidate, not a verified fact.**

The code does not paper over this. If no active runner in an open race
carries a place price under code `L`, `parse_racecard` raises
`SchemaMismatchError` naming every field it tried and pointing at the
archived payload. It never falls back to a win price, a tote price, or a
derived number.

*To close this:* run `python -m pytest -m live` from an Australian machine.
`test_an_open_racecard_carries_a_live_place_price` answers it directly.

**2. What `MDP` and `TMD` actually are.**

What is established: both codes were observed live on 2026-08-22 carrying
win prices, and those prices **persist after a runner is scratched**, while
the `L` price is withdrawn. So neither is a live fixed price — that much is
solid, and it is why the original scanner was changed to never read them.

What is **not** established: which product each code is. Sportsbet's racing
rules describe a family of tote-derivative products — Top Tote, Top Tote
Plus, and Mid Div / Middle Div (the middle of the three TAB dividends) —
which the site presents as *indicative* odds rather than locked prices. The
letters are suggestive (`TMD` ~ "Top/Mid Div", `MDP` ~ "Mid Div Plus") and
the observed behaviour is consistent with an indicative tote quote. **That
is an inference from the letters and from one behaviour, not evidence, and
this build does not claim it.** No archived racecard available here shows
whether these codes ever carry a *place* price at all.

So the implementation is deliberately conservative and records what it sees
rather than what it suspects:

* any non-`L` code carrying a price is captured as
  `price_type="tote_indicative"`, with the code string stored verbatim;
* an indicative price is never used as a live price, never valued as a
  takeable bet, and **never given a stake suggestion**;
* `PRICE_CODE_NOTES` in `app/sources/sportsbet.py` carries
  `"established": False` for both codes, and the test suite asserts it stays
  that way.

*To close this:* `tests/test_live.py::test_report_every_price_code_seen`
prints, per code, how many selections carry a win price and how many carry a
place price. Run it from an Australian machine and paste the output here.

**3. That any of this makes money.** The band table says the probabilities
are trustworthy from about $3 to $51. It does not say a trustworthy
probability will meet a place price generous enough to profit from, and
widening the cap from $9 to $51 admits *more* candidate rows rather than
better ones. The UNPROVEN banner is still up and still correct.

**4. Betfair with real credentials.** The adapter, the spread/liquidity
gates, the delayed-key detection and the place-market term matching are all
unit-tested, but no live exchange session has been opened from this build.

**5. A live scan table.** None exists. The table below is rendered from the
committed **test fixtures**, so the layout and arithmetic are real but the
horses and prices are invented:

```
Fixtureville R4  ·  jump in 15m00s  ·  9 runners, 3 places                                                    
┏━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━┓
┃  # ┃ Runner           ┃   Win ┃  Place ┃ P(plac… ┃   Fair ┃ drz·sb-pow… ┃      EV ┃ Tier    ┃   Stake ┃ No…┃
┡━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━┩
│  1 │ Ten Bagger       │  2.60 │   1.35 │   71.0% │   1.41 │       0.958 │   -4.2% │ —       │       — │    │
│  3 │ Third Rail       │  6.00 │   2.10 │   45.4% │   2.20 │       0.953 │   -4.7% │ —       │       — │    │
│  2 │ Second Wind      │  4.20 │   1.70 │   55.8% │   1.79 │       0.949 │   -5.1% │ —       │       — │    │
│  4 │ Fourth Estate    │  8.50 │   2.60 │   36.2% │   2.76 │       0.942 │   -5.8% │ —       │       — │    │
│  5 │ Fifth Avenue     │ 11.00 │   3.10 │   30.2% │   3.32 │       0.935 │   -6.5% │ —       │       — │    │
│  6 │ Sixth Sense      │ 15.00 │   3.80 │   24.0% │   4.16 │       0.913 │   -8.7% │ —       │       — │    │
│  7 │ Seventh Son      │ 21.00 │   4.60 │   17.7% │   5.64 │       0.815 │  -18.5% │ —       │       — │    │
│  8 │ Eighth Note      │ 34.00 │   6.20 │   12.1% │   8.27 │       0.750 │  -25.0% │ —       │       — │    │
│  9 │ Ninth Life       │ 51.00 │   8.40 │    8.4% │  11.91 │       0.706 │  -29.4% │ —       │       — │    │
└────┴──────────────────┴───────┴────────┴─────────┴────────┴─────────────┴─────────┴─────────┴─────────┴────┘

Fixtureville R6  ·  jump in 30m00s  ·  8 runners, 3 places  ·  TERMS FRAGILE                                  
┏━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━┓
┃  # ┃ Runner           ┃   Win ┃  Place ┃ P(plac… ┃   Fair ┃ drz·sb-pow… ┃      EV ┃ Tier    ┃   Stake ┃ No…┃
┡━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━┩
│  1 │ Ten Bagger       │  2.60 │   1.72 │   72.1% │   1.39 │       1.241 │  +24.1% │ BET     │  $10.00 │    │
│  3 │ Third Rail       │  6.00 │   2.55 │   46.8% │   2.14 │       1.194 │  +19.4% │ BET     │  $10.00 │    │
│  4 │ Fourth Estate    │  8.50 │   2.60 │   37.5% │   2.66 │       0.976 │   -2.4% │ —       │       — │    │
│  2 │ Second Wind      │  4.20 │   1.70 │   57.3% │   1.75 │       0.974 │   -2.6% │ —       │       — │    │
│  5 │ Fifth Avenue     │ 11.00 │   3.10 │   31.3% │   3.20 │       0.970 │   -3.0% │ —       │       — │    │
```

Note the first race: every Dr Z score is below 1.00 and the longshots score
*worst*. That is the expected shape on an ordinarily-margined book, and it
is what power de-vig buys — under proportional de-vig those tail runners
would show large fictitious edges.

**6. Results capture after a real race.** The whole capture-and-settle path
is exercised against fixtures — a scan stores signals, the race resolves, a
later scan captures the outcome, and the stored signals settle with the dead
heat divided correctly (`tests/test_scan_results_capture.py`). What has not
been seen is a *real* resulted Sportsbet racecard, which matters for one
specific reason:

> **The race-level `result` string cannot express a dead heat.** `"1,16,18"`
> is a list of saddlecloths in finishing order. A 3-place race whose string
> lists four numbers is equally consistent with "two runners dead-heated for
> third" and with "the source gave us more of the finishing order than we
> asked for". Guessing the first reading settles the runner that finished
> *last* as a placed runner on a fractional dividend.
>
> So dead heats are resolved from **per-runner finishing positions** (each
> selection's own `result` field), where two runners carrying position 3
> unambiguously dead-heated for third. When those positions are unavailable
> the settler pays the first N placings in full, marks the row
> `settlement_basis = "placings_string"`, and the backtest report says how
> many rows rest on that weaker evidence. The shape of a resulted racecard's
> per-runner `result` field is **not live-verified**.

**7. The `sportsbet_beta` win model — the fit itself IS verified, the
inputs are not.** The join and the estimator were exercised against the real
August 2026 history: 1,366 real races were joined on date + track + race
number + TAB number with **zero unmatched**, and a planted exponent of 1.15
was recovered as 1.1473 (`test_beta_fit_joins_real_races_and_recovers_a_known_exponent`).
What is missing is only the real input data —the model cannot switch on until this scanner has stored
~1,500 races of its own Sportsbet prices, and nobody sells those
historically. Until then every row is labelled **UNCALIBRATED** and cannot reach BET
without `--allow-uncalibrated`.

---

## What was verified without a network

* **Place engine against brute-force enumeration** of every finishing order,
  fields up to 7, five `(lam, tau)` pairs, both 2 and 3 places; sums equal
  the number of dividends; `lam = tau = 1` reproduces closed-form Harville;
  vectorised equals the loop oracle; padded batches equal one-at-a-time.
* **Power de-vig**: sums to 1, preserves order, and moves probability from
  longshots to favourites relative to proportional — monotonically across
  the book.
* **Beta recovery**: a known B = 1.18 is recovered from 4,000 simulated
  races to within 0.06.
* **Parser**: win and place from code `L` only; MDP/TMD captured but never
  used; scratchings from `statusCode "S"`, `isOut`, *and* from a missing
  live price alone; full placings; horse-only schedule filtering; missing
  place price raises rather than guesses.
* **Tier logic**: the SUSPECT cap overriding everything, the uncalibrated
  gate, the Ziemba filter, price staleness, the exactly-8 fragile flag, the
  delayed-Betfair near-jump rule, and the per-race stake cap.
* **End to end**: value → persist → render → settle, including a dead heat
  resolved from per-runner finishing positions, on a temporary SQLite
  database; plus a full scan-capture-settle cycle driven through `scan_once`
  with a fake client.

## Bugs caught during the build

All were fixed in the implementation, not worked around. The last six came
out of an adversarial review pass over the finished code:

1. **The per-race stake cap could be exceeded by rounding.** Three legs
   rounded to $6.67 summed to $20.01 against a $20.00 cap. Stakes are now
   floored to the cent.
2. **The dead-heat divisor was applied to the whole field.** The first
   implementation divided every placed runner's dividend when a dead heat
   occurred. Correct Australian practice divides only among the runners
   sharing the tied position: for placings `2,5,3,9` over three dividends,
   runners 2 and 5 are paid in full and only 3 and 9 are halved.
3. **Both maximum-likelihood fits could abort on a bad bracket.**
   `scipy.optimize.minimize_scalar(..., bracket=...)` raises `ValueError`
   when the three bracket points do not straddle the minimum, which would
   have taken down an entire calibration run over a detail of the starting
   guess. Both now use bounded optimisation. Caught by running the beta fit
   against real data for the first time; the win exponent is unchanged at
   1.0125.
4. **Results were never captured, so nothing could ever settle.** The scan
   selected only *open* races, and a race stops being open the moment it
   resolves — so `Race.result_placings` was never written by any code path.
   The backtester would have reported "no settled observations" forever and
   the UNPROVEN banner could never have come down. A scan now runs a
   settlement pass over races it valued earlier that have since resolved.
   Regression test: `tests/test_scan_results_capture.py`.
5. **A full finishing order was settled as a dead heat.** Given the string
   `"2,5,3,9,1,7,4,8,6"` for a 3-place race, every runner from third place
   down — including the one that finished last — settled as placed on a 1/7
   dividend. See item 5 above for the contract that replaced it.
6. **A beaten runner carried a dead-heat divisor**, so in any race with a
   dead heat every loser was reported as "settled through a dead heat".
7. **The scanner asked Sportsbet for the UTC date.** AEST is UTC+10, so for
   the first ten or eleven hours of every Australian day it would have
   fetched the *previous* day's schedule. At the moment this was fixed, UTC
   read 2026-09-18 and Australia read 2026-09-19 — the bug was live.
8. **The delayed-Betfair no-BET window was dead code.** Its condition was
   `len(models) == 1`, which can never hold because the Sportsbet model is
   always present. It now fires when Betfair is delayed and nothing else
   clears the BET threshold — i.e. exactly when a delayed price alone would
   have carried the row.
9. **The promised name check in the beta fit never ran**; `n_name_mismatches`
   was hard-wired to 0 while the docstring claimed names were compared. It
   now genuinely compares them — and immediately caught a mis-pairing in the
   test that built it, because real TAB numbers are not contiguous 1..n
   (scratchings leave gaps).
10. **Closing-line value compared a signal against itself.** The query had no
   "strictly later" constraint, so any runner captured once returned exactly
   0.00% CLV, silently dragging the reported mean toward zero.
11. **A fallback price field could beat the preferred one.** `_price_from_entry`
   scanned all keys for a bare number before trying the nested
   `{"decimal": ...}` form, so `{"winPrice": {"decimal": 2.30}, "win": 9.99}`
   returned 9.99.
12. **Scratch wording was matched by exact equality**, so `"Late Scratching"`
   passed through as an active runner while `"Scratched"` did not. Matching
   is now by substring, and string booleans (`"isOut": "true"`) count.

## To finish verification

From an ordinary Australian Windows machine:

```
py -m pytest -m live        # answers open items 1 and 2
py -m app.drz --show-all    # a real table
py -m app.backtest          # after some races have resolved
```
