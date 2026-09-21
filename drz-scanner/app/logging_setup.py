"""Structured logging shared by all entry points: stderr plus a daily file.

The file log is what you read the morning after a loop ran unattended. It
rotates at midnight and keeps two weeks; the terminal stays readable.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(level: int = logging.INFO, log_dir: Path | None = None) -> Path | None:
    formatter = logging.Formatter(fmt=FORMAT, datefmt=DATEFMT)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    path: Path | None = None
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / "drz.log"
            file_handler = logging.handlers.TimedRotatingFileHandler(
                path, when="midnight", backupCount=14, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            # The file gets everything; the terminal gets what was asked for.
            file_handler.setLevel(logging.DEBUG)
            root.addHandler(file_handler)
            root.setLevel(min(level, logging.DEBUG))
            stream.setLevel(level)
        except OSError as exc:  # a log file must never stop the scanner
            root.warning("file logging disabled: %s", exc)
            path = None
    # httpx logs full URLs at INFO and httpcore traces every request at
    # DEBUG. Neither belongs in a file that lives on disk for two weeks —
    # the exchange session token travels in a request header.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path
