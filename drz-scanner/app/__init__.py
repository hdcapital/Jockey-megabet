__version__ = "1.1.0"

#: Bumped whenever the valuation pipeline changes shape, so the backtester
#: can tell rows produced by different versions of the model apart.
#: 1.0 = flat 9.0 win-price cap, no band correction, no exchange confirmation.
#: 1.1 = per-model win-price caps, shrink-only band correction, BET requires
#:       a matching Betfair place market to agree.
MODEL_VERSION = "1.1"
