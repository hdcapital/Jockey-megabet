# Quickstart (for beginners)

You need: a computer (Mac or Windows) on an **Australian** internet
connection. Sportsbet only answers Australian IPs — that's why this can't
run on GitHub's servers or most overseas clouds (verified in BUILD_STATUS.md).

## Easiest: let Claude Code set it up

1. On your computer, install Claude Code from https://claude.ai/code
2. Open it and paste:

> Clone https://github.com/hdcapital/Jockey-megabet, check out the branch
> `claude/sportsbet-jockey-megabet-scanner-xbnjzv`, install it, run the
> tests, then run a live scan and show me the results. If the Sportsbet
> schema doesn't parse, fix the parser using the archived raw responses.

Claude will install everything, run the first real scan, and adapt the
parser against live data if Sportsbet's current schema differs.

## Manual: about 10 minutes

1. **Install Python** (3.11 or newer) from https://python.org/downloads
   — on Windows, tick **"Add python.exe to PATH"** during the install.

2. **Download the code** (no git needed): on the GitHub repo page, switch
   to the branch `claude/sportsbet-jockey-megabet-scanner-xbnjzv`, click
   **Code → Download ZIP**, and unzip it.

3. **Open a terminal** — "Terminal" on Mac, "PowerShell" on Windows — and
   go into the unzipped folder, e.g.:

   ```
   cd Downloads/Jockey-megabet-claude-sportsbet-jockey-megabet-scanner-xbnjzv
   ```

4. **Install the dependencies**:

   ```
   python3 -m pip install -r requirements.txt      # Mac
   py -m pip install -r requirements.txt           # Windows
   ```

5. **Run one scan**:

   ```
   python3 -m app.scan        # Mac
   py -m app.scan             # Windows
   ```

6. **Keep it running all day** (refreshes every 3 minutes):

   ```
   python3 -m app.scan --loop     # Mac  (Windows: py -m app.scan --loop)
   ```

   Leave the window open. Every observation is saved to `data/megabet.db`
   for backtesting later (`python3 -m app.backtest`).

## What you should expect to see

* A table of today's Jockey Megabets with Sportsbet's price, the model's
  fair odds and the expected value — **or**
* `No active Jockey Megabets found at <time>.` if Sportsbet has none up
  right now — **or**
* A clear error naming exactly which endpoint failed and why (the raw
  response is saved under `data/raw/` so it can be diagnosed).

The scanner never shows made-up data. A positive EV is model-implied, not
guaranteed profit — read README sections 14 and 15 before betting anything.

Betfair is optional and off until you add credentials — see README §6.
