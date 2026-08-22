# BUILD STATUS

Last updated: 2026-08-22 (UTC)

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

## Known external blocker (unresolved, honestly reported)

**This build environment's egress proxy denies all betting hosts**, so no
live market data could be retrieved from here:

* `CONNECT www.sportsbet.com.au:443` → **HTTP 403** from the org egress
  proxy ("policy denial"), confirmed via `$HTTPS_PROXY/__agentproxy/status`.
* Same 403 for `api.betfair.com`, `identitysso.betfair.com`,
  `identitysso-cert.betfair.com`, `www.betfair.com.au`, `api.beta.tab.com.au`.

The proxy documentation explicitly instructs that policy denials must be
reported, not worked around. Consequently:

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
