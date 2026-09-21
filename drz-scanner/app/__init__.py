__version__ = "1.2.0"

#: Bumped whenever the valuation pipeline changes shape, so the backtester
#: can tell rows produced by different versions of the model apart.
#: 1.0 = flat 9.0 win-price cap, no band correction, no exchange confirmation.
#: 1.1 = per-model win-price caps, shrink-only band correction, BET requires
#:       a matching Betfair place market to agree.
#: 1.2 = delayed-key rows are no longer quality-downgraded (the delayed cap
#:       and near-jump rule now actually decide them); stakes size off the
#:       worst model / exchange estimate; exchange books refreshed per sweep
#:       with a staleness gate; runners matched to the exchange by cloth
#:       number.
MODEL_VERSION = "1.2"
