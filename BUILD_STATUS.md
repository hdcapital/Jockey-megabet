# BUILD STATUS

Last updated: 2026-08-22 (UTC) — after live GitHub Actions runs
32543539735 / 32543634489 and the first successful live probe from an
Australian machine.

## Live schema verification (2026-08-22, user-run probe from AU — HTTP 200s)

`scripts/probe_endpoints.py` run on an Australian Windows machine reached
every Sportsbet endpoint. Verified against real responses:

* **Megabets listing**: returns a LIST of Racing Extras event stubs
  (`{id, name, competitionName, startTime, statusCode, numMarkets,
  httpLink}`). Jockey Megabets = events with
  `competitionName == "Jockey Extras"`, named "Jockey Extras - <Meeting>"
  (5+ meetings live at probe time). Their markets live in the event's
  Racecard. Discovery has been REWRITTEN to this two-stage shape and is
  covered by fixture tests.
* **/Racing/Challenges**: dead — live 404 `ResourceNotFound`; removed.
* **AllRacing/{date}**: `{dates:[{meetingDate, sections:[{raceType,
  meetings:[{id, name, className, events:[{id, raceNumber, startTime,
  name, statusCode, bettingStatus, result?, ...}]}]}]}]}` — compatible
  with the existing walker; horse-only filter added via `className`.
* **Racecard top level**: venue = `competitionName`, `statusCode` "A"/"R",
  `bettingStatus` "PRICED"/"RESULTED", `result` = placings by saddlecloth
  ("1,16,18") — status mapping and winner extraction updated accordingly.
* **Betfair**: both official endpoints reachable from the AU machine and
  answering documented JSON (`INVALID_USERNAME_OR_PASSWORD`,
  `INVALID_APP_KEY` without credentials) — Model B needs only real creds.

**Deep probe (user-run from AU, 2026-08-22) closed the remaining gaps** —
all parser-facing fields are now live-verified and implemented:

* **Jockey Extras racecards**: each market is NAMED AFTER THE JOCKEY
  ("Blake Shinn"); selections carry the threshold in words ("To Ride Two
  or More Winners") with the price at `prices[priceCode=L].winPrice`.
  Parser Case C implements exactly this shape (10 live meetings observed:
  Sandown, Newcastle, Cairns, Morphettville, Port Macquarie, Gympie,
  Belmont, Kununurra, Newman, Toowoomba).
* **Ordinary racecards**: runners sit in the top-level `markets` list
  ("Win or Place") with `jockey`, `runnerNumber`, `trainer`, `isOut` and a
  `prices` list per price code — `L` is the live price; `MDP`/`TMD` are
  stale morning references and are now explicitly NEVER used (they would
  otherwise silently price scratched runners).
* **Scratches**: a scratched runner's selection flips to `statusCode "S"`
  with its live win price withdrawn (observed live); also `isOut: true`
  is honoured.

**Live-verified end-to-end (user run, 2026-08-22 12:19 AEST)**: a full
`python -m app.scan` produced the real valuation table — 84 jockey offers
across 10 meetings, 72 racecards, correct fair-probability monotonicity and
EV arithmetic, scratches excluded, LOW rows hidden.

## Extended market types (added after the verified jockey run)

* **Trainer Extras** (`--type trainer`, on by default): discovery of the
  live-observed "Trainer Extras - <Meeting>" events; a trainer's per-race
  win probability is the exact sum of their runners' fair probabilities,
  then the same Poisson-binomial. Trainer selection *wording* is parsed
  tolerantly (To Train / To Have / To Saddle ...) but has NOT yet been seen
  live — a mismatch logs loudly with the raw payload archived.
* **Jockey Challenge** (most wins): seeded Monte Carlo over every jockey at
  the meeting (validated in tests against exact enumeration, incl.
  dead-heat division and "Any Other" aggregation). No live Sportsbet
  challenge market has been observed yet; discovery reports honestly.
  Settlement is assumed "most winners, dead-heats divided" and recorded
  with each valuation.
* Output is grouped by type (Jockey Megabets / Trainer Megabets / Jockey
  Challenge tables); DB rows carry `market_type` (additive migration for
  older SQLite files included). 149 tests pass.

**Remaining to verify live**: trainer-market selection wording and any
challenge markets on a real race day; results capture after races resolve;
Betfair with real credentials.

## What is working (verified by actually running it)

* Clean-environment install: `python3 -m venv` + `pip install -r requirements.txt` — verified.
* Full unit test suite: **105 passed** (`python -m pytest -q`), covering:
  * Poisson-binomial engine validated against an independent brute-force
    enumeration, including the specification's 7-ride example vector and
    degenerate cases (0 rides, p=0, p=1, thresholds beyond n).
  * De-vig methods (proportional / power / shin): sum-to-1, ordering,
    overround bookkeeping, invalid-odds rejection, post-scratch markets.
  * Name matching: capitalization, punctuation, apostrophes (all variants),
    runner-number prefixes, bracketed suffixes `(NZ)`/`(a3)`, jockey
    initials, ambiguous names correctly refused.
  * Consensus blending, fallback and weight renormalisation.
  * Betfair probability derivation (midpoint/spread/liquidity gates,
    crossed-book rejection, unavailable states).
  * Sportsbet parsers against clearly-labelled synthetic fixtures
    (Megabet market-name threshold parsing, racecard runner/jockey/price
    extraction, scratch detection, trainer-market rejection).
  * End-to-end pipeline: value → persist to SQLite → render table →
    settle in backtester with a correct win count.
  * Scratching recalculation and late-rider-replacement detection
    (possible-void flag set on the affected Megabet).
* Database initialisation (SQLite file + in-memory; portable types for
  PostgreSQL) — verified.
* `python -m app.scan` runs end-to-end: with the network blocked it reports
  the real proxy error and displays nothing (no fabricated data), exit code 2.
  Filters (`--meeting`, `--jockey`, `--min-edge`, `--date`, `--source`,
  `--show-low`, `--no-db`, `--loop`) parse and execute.
* `python -m app.backtest` runs; correctly reports an empty observation set
  and, in tests, settles synthetic persisted observations with correct
  win counts, calibration buckets and P&L.
* Raw-response archival layer (hash + metadata header) and `raw_responses`
  table wiring.
* Rate limiting (per-host throttle), retries with exponential backoff,
  timeouts, realistic User-Agent — exercised by the live-attempt code path.
* No credentials committed; `.gitignore` covers `.env`, keys, certs, tokens.

## Known external blockers (unresolved, honestly reported)

**1. This build environment's egress proxy denies all betting hosts**:

* `CONNECT www.sportsbet.com.au:443` → **HTTP 403** from the org egress
  proxy ("policy denial"), confirmed via `$HTTPS_PROXY/__agentproxy/status`.
* Same 403 for `api.betfair.com`, `identitysso.betfair.com`,
  `identitysso-cert.betfair.com`, `www.betfair.com.au`, `api.beta.tab.com.au`.

**2. Live tests were then run on GitHub-hosted Actions runners** (US Azure,
`northcentralus`; runs 32543539735 and 32543634489 on 2026-08-22, evidence
in the job logs and uploaded raw-response artifacts). TCP/TLS connected
fine, but both providers' edges denied the requests themselves:

* Sportsbet (`www.sportsbet.com.au`, all four probed endpoints):
  `HTTP 403`, `Server: AkamaiGHost`, HTML body `"Access Denied ... Reference
  #18.aa3a2f17..."` — Sportsbet serves Australian IPs only.
* Betfair identity SSO (`identitysso.betfair.com/api/login`): `HTTP 403`
  with a Betfair-branded HTML block page (not the documented JSON).
* Betfair betting API (`api.betfair.com/exchange/betting/json-rpc/v1`):
  `HTTP 403` Cloudflare "Attention Required" challenge page.

These are the providers' own geo/bot access controls; per project rules
they are reported, not bypassed. The scanner behaved exactly as designed in
both runs: real errors (now including a snippet of the actual block-page
body) and zero fabricated output; unit suite passed 105/105 on the runner.

**Resolution requires an Australian vantage point**: run the scanner (or a
GitHub self-hosted runner, `runs-on: [self-hosted]`) on an AU
machine/VPS/residential connection. Consequently:

## NOT yet verified against real data (blocked by the above)

* The **current live Sportsbet response schemas**. The endpoint routes in
  `app/sources/sportsbet.py:ENDPOINTS` and the field mappings in its parsers
  follow previously researched payload shapes and are written tolerantly
  (multiple candidate keys, whole-document scanning, archived raw payloads,
  loud `SchemaMismatchError` with the archive path). They must be validated
  on a network that can reach sportsbet.com.au by running:
  `python -m pytest -m live -rs` and then `python -m app.scan -v`.
  If parsing fails, the raw payload is saved under `data/raw/sportsbet/` —
  adapt the one module and re-run.
* Live meeting discovery, racecard retrieval, jockey/odds extraction and a
  real populated output table (the code paths are covered by fixture tests;
  the live data itself was unreachable).
* Betfair login/market retrieval (needs both network access and user
  credentials, which are deliberately not in the repo).

## What remains

1. Run the live integration test + a real scan from an unblocked network;
   adapt `app/sources/sportsbet.py` field mappings if the live schema
   differs (single-module change by design).
2. Capture scans across a real race day; confirm scratching/jockey-change
   transitions in stored observations.
3. Accumulate observations + results, then run backtest/calibration on real
   history.
4. Optional: Betfair credentials → verify Model B end-to-end.

## Latest test results

```
python -m pytest -q          -> 105 passed, 1 deselected (live)
python -m pytest -m live -rs -> 1 skipped: "Sportsbet unreachable from this
                                environment: ... ProxyError: 403 Forbidden"
python -m app.scan           -> reports real 403, no fabricated output, exit 2
python -m app.backtest       -> empty-history report, exit 0
```
