# BUILD STATUS

## 2026-10-10 — Race value scanner (Sportsbet vs Betfair, next 20 minutes)

`python -m app.race_value` (or `value-loop.bat`) loops every 60 seconds over
the races jumping in the next 20 minutes and ranks every runner by EV of
Sportsbet's fixed win price against Betfair as the truth. Details and the
adjustments in README §9a. Per pass: one Betfair catalogue call for the
window, the Sportsbet AllRacing listing, racecards only for races the
exchange has a market for, then one `listMarketBook` call last so the two
snapshots are close together.

Supporting changes: `BetfairClient.list_win_markets(from, to, countries)`
(the day catalogue now calls it); `BetfairMarket` records `inplay`, the
market's matched volume and the book time; `RaceInfo.win_market_status`
carries Sportsbet's win-market code ("S" = suspended); the "still priced by
Sportsbet but REMOVED on Betfair" warning no longer fires for runners both
sides have scratched. Raw-response archiving is off by default for this loop
(`--archive` turns it on). 202 tests pass; the scanner has not been run
against the live sites from here (the build host cannot reach either).

## 2026-10-03 — Betfair connection diagnosis and hardening

Report: "it's not connecting to Betfair despite having the API
requirements." A Betfair failure was one stderr log line plus an em-dash
column, which reads the same as "no credentials", so the first change is
visibility; the rest are defects found reviewing the client against the
Betfair API.

* **`python -m app.betfair_check`** walks config → login → catalogue →
  books → prices and prints one `[OK]`/`[FAIL]` line per step with
  Betfair's own error code and the usual fix. Secrets are masked; the
  password never appears. Exit code says which step failed.
* **Status line in every scan**: `Betfair: connected via <host>; N AU win
  markets; M books (K open)` or `Betfair: FAILED — <reason>`.
* **Login errors carry Betfair's code** (`INVALID_APP_KEY`,
  `INVALID_USERNAME_OR_PASSWORD`, `PENDING_AUTH`, ...) and a hint
  (`LOGIN_HINTS`). Before, the message was `login failed: FAIL`.
* **Australian identity host.** Betfair documents
  `identitysso.betfair.com.au` for AU/NZ accounts; the default was `.com`
  with the AU host as a commented-out example. The client now tries the
  configured host, then the other region, and logs which accepted the
  login. Lock-out and ban codes are never retried on the second host.
* **Block pages and non-JSON answers** (Cloudflare "Attention Required",
  HTML challenges) were a `JSONDecodeError` traceback; they are now a
  reported `SourceUnavailableError` with the body snippet.
* **`.env` path.** Settings read `.env` from the current working directory;
  launched from another directory the credentials were silently absent.
  It is now read from the project folder.
* **Liquidity gate** compared `BETFAIR_MIN_LIQUIDITY` against each
  runner's own matched volume (`r["totalMatched"]`), not the market's as
  documented, so outsiders failed the gate all morning and the exchange
  model (which needs every ride reliable) rarely engaged. The gate now uses
  the market's matched volume; the runner's own is still recorded.
* **End-to-end test over HTTP** (`tests/test_betfair_end_to_end.py`): a
  fake Betfair identity host and JSON-RPC API served through the real
  `ArchivingClient` cover the form login, headers, session renewal, the
  regional fallback, block pages, APING error codes, delayed-key
  detection and a Sportsbet racecard valued with a Betfair fair price.
* **`betfair-check` workflow** runs the diagnosis on GitHub with the
  repository's `BETFAIR_*` secrets; its log is the live verdict.

184 tests pass. The build host's egress policy blocks every Betfair host
and the Betfair developer docs, so no live login could be made here.

**Live verdict from the user's machine (2026-10-03 11:44 AEST):**
`betfair_check` passed every step (login via identitysso.betfair.com, 121
AU WIN markets, 12/12 books open, 107 runners priced, 49 reliable) and the
scan printed `Betfair: connected ...; 121 books (120 open)`. Every runner
showed `matched 0.0` with the market's own volume non-zero, which confirms
the runner-level liquidity gate was what kept the exchange out before.
The same log showed four more things, fixed here with tests:

* The same race was re-matched (and re-logged) once per jockey with a
  Megabet; matches are now cached per race for the whole scan.
* Per-ride `consensus fell back` lines (hundreds per scan) are DEBUG now.
* The "Betfair fair" column was a bare dash whenever any ride failed the
  gate; it now reads e.g. `3/9 rides` so engagement is visible.
* A second meeting named `NEWCASTLE` (16 races, class not given) was merged
  into the thoroughbred Newcastle card. A section's `raceType` now
  classifies meetings without their own class, and a second meeting with
  an already-loaded name is skipped with a warning naming both.
* Runners Betfair has REMOVED but Sportsbet still prices (e.g. `Just In
  Time`, Randwick R1) are now named in one warning per race instead of
  being logged as "unmatched"; the two sources disagree, so nothing is
  overridden.

**Scratchings were being priced (found via the Betfair cross-check, fixed
with the user's archived payload, 2026-10-03 12:24 AEST).** The exchange
reported dozens of runners REMOVED that Sportsbet still "priced" (9 of 20
in Flemington R5); the website confirmed `Just In Time` (Randwick R1) was
scratched. `scripts/inspect_runner.py` on the archived racecard showed the
live shape: in the "Win or Place" market the scratched selection has
`statusCode "S"` while the market stays `"A"`, and its `prices` list still
carries NTP/NTS entries and a stale live price (26.0). The parser's rule
("S" only counts when no price is left) therefore kept the runner active at
$26 with its jockey booked, inflating every overround and leaving stale
rides (the "ambiguous Brodie Loy" booking) in the model. Now a selection
"S" inside an open market is a scratching whatever its prices say (a
market-wide "S" is a suspension, not a scratching), a scratched runner is
never priced, and a price-code-tagged list yields only the "L" entry.
Regression tests in `tests/test_sportsbet_scratchings.py` use the real
shape. 188 tests pass.

Still open, outside the Betfair work: once a race has run its rides lose
their live price, so the Megabet drops to LOW and is hidden rather than
being valued conditional on the results so far.

**GitHub Actions verdict (runs 37086687517 / 37086845909, 2026-10-03):**

* The repository has **no** `BETFAIR_APP_KEY`, `BETFAIR_USERNAME` or
  `BETFAIR_PASSWORD` secrets (the "Secrets present?" step printed NOT SET
  for all three), so the check stopped at the config step.
* Without credentials the reachability probe showed that
  `identitysso.betfair.com`, `identitysso.betfair.com.au` and
  `api.betfair.com` all answer a GitHub-hosted runner with **HTTP 403 and
  a Cloudflare block page**, so credentials would not have helped: Betfair
  refuses GitHub's datacenter IPs, as the 2026-08-22 note already recorded.
  Sportsbet answers the same runner with its "Location Error" page.
* The live verdict therefore has to come from the user's own Australian
  machine: `python -m app.betfair_check` there prints the step that fails
  and Betfair's own reason. A self-hosted runner in Australia with the
  three secrets would make the `betfair-check` workflow meaningful.

## 2026-09-19 — Betfair catalogue fix (ported from drz-scanner)

A live run with real credentials on 2026-09-19 showed `listMarketCatalogue`
returning `TOO_MUCH_DATA`: one request for a day's ~110 AU WIN markets
with both `MARKET_DESCRIPTION` and `RUNNER_DESCRIPTION` weighs ~222
against Betfair's limit of 200, so the exchange never engaged. The
catalogue is now fetched in six-hour windows with the runner projection
only (`MARKET_DESCRIPTION` was never read), deduplicated by market id.
Also ported: one automatic re-login when a session token expires mid-loop.
Regression test: `test_catalogue_is_fetched_in_light_windows`. 150 tests pass.

## 2026-09-19 — Betfair runner matching and delayed keys (second live run)

With the catalogue loading (127 AU win markets) the next live run still
showed the exchange contributing nothing: whole fields logged
`runner unmatched on Betfair`, every matched runner was excluded as
`thin market` or `spread too wide`, and the Betfair column was `—`
throughout. Changes, each with a regression test in
`tests/test_betfair_matching.py`:

* **Runner matching** (`app/matching/runners.py`): Betfair names runners
  `"4. Horse Name"`; the saddlecloth is now the first key (name checked,
  never overridden), then normalized name. One INFO/WARNING line per race
  reports `matched m/n`, the unmatched names and the first few names Betfair
  lists for that market, so a wrong market is visible; per-runner lines are
  DEBUG. The engine matches each race once instead of once per ride.
* **Market lookup** (`app/matching/meetings.py`): a same-venue, same-race-
  number market whose start is outside the 40-minute tolerance is now
  rejected with a log line naming it, instead of being accepted when it was
  the only candidate (the catalogue spans 52 hours, so the next day's card
  at the same venue, or a harness card sharing a venue name, could be taken).
* **Delayed application keys** (`app/sources/betfair.py`, `app/config.py`):
  a delayed key reports zero matched volume on every book, so the
  `BETFAIR_MIN_LIQUIDITY` gate could never pass and Betfair was never used.
  The client now decides per session (all open books at zero matched
  volume) and, when delayed, marks a runner reliable on the spread alone
  (`BETFAIR_DELAYED_MAX_RELATIVE_SPREAD`, default 10%). `BETFAIR_KEY_DELAYED`
  forces it either way. Quality text says `delayed key` when that rule was
  used.
* **Diagnostics**: markets that come back without a book, or with no active
  runners, are logged with their status and counts.

Not established from the log alone: whether the fully-unmatched races were
a market taken from the wrong day, or a market with an empty book. The new
per-race summary names the market's runners so the next run shows which.
Genuinely wide morning spreads (50%–3000% at 10:37) are real and remain
excluded; that improves as the markets fill through the day.

163 tests pass.


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
