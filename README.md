# Sportsbet Jockey Megabet Fair-Value Scanner

Automatically discovers Sportsbet **Jockey Megabets** (e.g. *"James McDonald
to ride 2+ winners"*), prices them from the real underlying win markets of
every race the jockey rides that day, and flags apparent mispricings — while
storing every observation so the strategy can be honestly backtested later.

**Real data only.** Every displayed price, horse, jockey, meeting and
timestamp comes from a retrieved source. When a source is unreachable the
scanner says so and shows nothing, and when there are no Jockey Megabets on
offer it prints `No active Jockey Megabets found at <timestamp>.` — it never
fabricates rows.

## 1. What it does

1. Discovers today's Jockey Megabet markets from Sportsbet's public web API.
2. Fetches the racecard of every race at each relevant meeting.
3. Matches the Megabet jockey to their rides (robust name matching; ambiguous
   matches are recorded, never guessed).
4. Converts each ride's win odds into a **fair win probability** (bookmaker
   margin removed).
5. Combines the ride probabilities into the exact distribution of the
   jockey's total wins (**Poisson-binomial**, no Poisson approximation).
6. Compares fair odds for each threshold (2+, 3+, 4+ wins, …) with
   Sportsbet's Megabet price and computes expected value.
7. Persists every price and valuation, plus raw API responses, for
   backtesting and audit.
8. Detects scratchings, jockey changes and abandonments on every refresh and
   recalculates.

## 2. How fair values are calculated

For each race the jockey rides, with active runners' decimal odds `o_i`:

```
raw_p_i   = 1 / o_i                     # raw implied probability
overround = sum(raw_p_i)                # bookmaker margin indicator
fair_p_i  = devig(raw_p)                # margin removed (see below)
```

De-vig methods (pluggable, chosen by `DEVIG_METHOD`, recorded per stored
valuation; none is claimed superior without evidence):

* `proportional` (default): `fair_p_i = raw_p_i / sum(raw_p)`
* `power`: solves `sum(raw_p_i ** k) = 1`
* `shin`: Shin's insider-trading model, solving for the insider fraction `z`

With the jockey's per-ride fair probabilities `p_1..p_n`, the number of wins
`X` follows a Poisson-binomial distribution computed exactly by dynamic
programming:

```
P(X = j)  for j = 0..n,  then  P(X >= k) = sum_{j>=k} P(X = j)
fair_probability = P(X >= k)          # k = Megabet threshold
fair_odds        = 1 / fair_probability
expected_return  = fair_probability * sportsbet_odds - 1
edge_pct         = sportsbet_odds / fair_odds - 1   (same number)
```

Assumption: race outcomes are conditionally independent given the market
probabilities. This is *not* adjusted arbitrarily; enough history is stored
to test for jockey-level dependence later (see §15).

### Probability models

* **Model A — `sportsbet_novig`**: Sportsbet's own win market, de-vigged.
* **Model B — `betfair`**: Betfair Exchange best back/lay. Midpoint of best
  back and lay when the relative spread ≤ `BETFAIR_MAX_RELATIVE_SPREAD` and
  matched volume ≥ `BETFAIR_MIN_LIQUIDITY`; otherwise the estimate is marked
  unreliable and excluded from consensus. Never invented when missing.
* **Model C — `consensus`**: weighted blend (`CONSENSUS_WEIGHT_*`, defaults
  0.7/0.3, renormalised). Falls back to Sportsbet no-vig when Betfair is
  unavailable/unreliable, and says so (`fallback` flag, log line).

All three are stored per scan so their performance can be compared in
backtests instead of assumed.

## 3. Data sources

* **Sportsbet** — the undocumented public JSON endpoints behind
  sportsbet.com.au (`/apigw/sportsbook-racing/...`). These are not an
  official API: all routes live in `ENDPOINTS` in
  `app/sources/sportsbet.py`, parsing is defensive, and raw payloads are
  archived so schema changes can be diagnosed and fixed in one module.
* **Betfair** — the official Exchange Betting API (JSON-RPC) with your own
  app key; used purely as an external probability benchmark. Optional.

## 4. Limitations (read this)

* **Sportsbet geo-restricts to Australian IPs** (verified 2026-08-22: its
  Akamai edge returns 403 "Access Denied" to US-based GitHub runners). The
  scanner must run from an Australian network/VPS; the bundled GitHub
  Actions workflows only work with a self-hosted AU runner. This is
  Sportsbet's access control and the scanner does not attempt to bypass it.
* **Betfair's edge also blocks major-cloud datacenter IPs** (verified
  2026-08-22: 403 from identity SSO and a Cloudflare challenge on the
  betting API from GitHub's US runners). Run the collector from a
  residential/AU connection or a host whose IP Betfair accepts.
* Sportsbet's endpoints are unofficial and can change or be geo-restricted
  at any time; the adapter fails loudly (with archived payloads), it does
  not guess. **The current parser field-mapping was written against
  researched payload shapes and must be validated against live responses —
  see BUILD_STATUS.md for exactly what has and hasn't been verified.**
* Model EV assumes the underlying win markets are efficiently priced after
  de-vigging, and assumes independence between races.
* Sportsbet settlement rules on jockey changes/abandonments may void or
  reprice a Megabet; the scanner records a `possible_void_on_jockey_change`
  flag separately from mathematical fair value.
* GitHub Actions scheduling is too coarse for high-frequency capture.

## 5. Install

> New to all this? See **[QUICKSTART.md](QUICKSTART.md)** for a
> plain-English, copy-paste setup guide (including a no-git path).

```bash
git clone <this repo> && cd Jockey-megabet
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit; optional unless using Betfair
python -m pytest -q         # 100+ unit tests, no network needed
```

Python 3.11+ required (3.12 recommended).

## 6. Betfair account / API setup (optional)

1. Betfair account (Australian residents: betfair.com.au).
2. Create an application key: <https://developer.betfair.com> → Account →
   *My API keys* (a delayed key works for prices with delay; a live key
   requires activation).
3. Put `BETFAIR_APP_KEY`, `BETFAIR_USERNAME`, `BETFAIR_PASSWORD` in `.env`.
4. Optional non-interactive login: generate a client certificate, upload it
   to your Betfair account, set `BETFAIR_CERT_FILE`/`BETFAIR_KEY_FILE`.

Without credentials the scanner runs Sportsbet-only and clearly marks the
Betfair columns unavailable.

## 7. Environment variables

See [.env.example](.env.example) — every variable is documented there.
Nothing secret is ever committed or logged.

## 8. Run one scan

```bash
python -m app.scan
# filters:
python -m app.scan --meeting Randwick --jockey "J McDonald" --min-edge 0.05
python -m app.scan --date 2026-08-22 --source sportsbet --show-low
python -m app.scan --type trainer   # jockey | trainer | challenge | all
python -m app.scan --no-db          # don't persist observations
```

One scan covers three market families, reported in separate tables:

* **Jockey Megabets** ("X to ride 2+ winners") — Poisson-binomial over the
  jockey's rides.
* **Trainer Megabets** ("Trainer Extras") — same distribution, but a
  trainer's per-race win probability is the sum of their runners' fair
  probabilities (exact: race winners are mutually exclusive).
* **Jockey Challenge** (most winners at a meeting, when Sportsbet offers
  it) — seeded Monte Carlo over the whole meeting's de-vigged win markets,
  so competitors are correctly negatively correlated. Settlement is
  *assumed* to be "most winners, dead-heats divided" and that assumption is
  stored with every valuation; verify Sportsbet's actual challenge rules
  (points systems differ) before relying on these numbers.

Output columns: meeting, jockey, active rides, threshold, Sportsbet odds,
fair probability, per-model fair odds, EV and data-quality grade. LOW-quality
rows are hidden by default (`--show-low` to include). Positive EV is labelled
*model-implied positive EV* — see §14.

## 9. Run continuously

```bash
python -m app.scan --loop            # every SCAN_INTERVAL_SECONDS (180s default)
# or
./scripts/run_loop.sh
# or as a service:
sudo cp scripts/megabet-scanner.service /etc/systemd/system/   # edit paths first
```

Keep the interval ≥ 2 minutes; the HTTP layer additionally throttles
per-host requests (`HTTP_MIN_REQUEST_INTERVAL_SECONDS`) and retries with
exponential backoff. A GitHub Actions workflow
(`.github/workflows/scan.yml`) exists for coarse scheduled capture.

## 10. Run tests

```bash
python -m pytest -q            # unit tests only (fixtures, no network)
python -m pytest -m live -rs   # live integration test against Sportsbet
```

The live test passes when Sportsbet is reachable (even with zero Megabets on
offer — a real, reported state) and skips with the real error when the
network blocks it. Mock data exists only in `tests/fixtures/` and is
labelled synthetic; it can never appear in production output.

## 11. Run backtests

```bash
python -m app.backtest                       # consensus model, EV >= 0
python -m app.backtest --model sportsbet_novig --min-ev 0.05 \
    --by threshold jockey rides ev
```

Reports: observed/settleable counts, theoretical bets at your EV threshold,
average model EV vs actual win rate, turnover, P&L, ROI, calibration by
fair-probability bucket, and performance splits. Only observations actually
captured live are used; a Megabet settles only when **every** race of its
meeting has a stored result. Missing history is never backfilled.

## 12. Database structure

SQLite at `data/megabet.db` by default; set `DATABASE_URL` for PostgreSQL —
only portable SQLAlchemy types are used. Tables:

| table | contents |
|---|---|
| `meetings`, `races`, `runners` | reference data incl. source ids, status |
| `runner_prices` | timestamped odds + raw/fair probability + de-vig method + overround, Betfair back/lay/volume |
| `megabet_markets` | jockey, threshold, source market name, void flag |
| `megabet_prices` | timestamped Sportsbet Megabet odds |
| `model_valuations` | timestamped fair probability/odds, EV, model + version, ride count, quality grade, and the exact per-ride probabilities used (JSON) so every number is reproducible from the row |
| `results` | winning runner/jockey per race |
| `raw_responses` | source, endpoint, timestamp, HTTP status, sha256, archive path |
| `unmatched_records` | jockey/runner/meeting names that could not be matched |

Raw response bodies are archived under `data/raw/<source>/<date>/` with a
metadata header line (source, URL, timestamp, status, hash).

## 13. Adding another bookmaker

1. New adapter in `app/sources/<name>.py` returning the shared dataclasses
   (`RaceInfo`, `RunnerInfo`, …) — see `app/sources/base.py`.
2. Give its probability a `SourceProbability` entry and a weight in
   `app/models/consensus.py` inputs (weights come from configuration).
3. Store its prices via `Repository.add_runner_price(bookmaker="<name>")`.

The pricing engine (`app/engine.py`) is source-agnostic and needs no change.

## 14. Interpreting expected value

`expected_return = fair_probability × odds − 1` is the model-implied average
profit per unit staked *if the model's probabilities are correct*. A positive
number is **model-implied positive EV, not guaranteed profit**, because:

* the fair probabilities inherit any bias in the underlying markets and in
  the de-vig method;
* the independence assumption may be wrong for same-jockey rides;
* scratchings, jockey changes and rule-based voids change the bet after you
  strike it;
* variance dominates small samples — a genuine 5% edge still loses often.

Trust the backtester and calibration tables (§11), not a single green number.

## 15. Correlation investigation

The stored per-ride probabilities, valuations and results are sufficient to
test whether same-meeting jockey outcomes deviate from the independence
assumption (track bias, jockey form, stable dominance, …). The production
model is deliberately not adjusted until that historical evidence exists.

## Security

`.gitignore` excludes `.env`, keys, certificates, cookies and tokens.
Credentials live only in environment variables and are never logged;
raw archives contain only public market data.
