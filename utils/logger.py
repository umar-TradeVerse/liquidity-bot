"""Logging configuration."""

import logging
import os
from logging.handlers import RotatingFileHandler

# 2026-11-05: ONE combined log (all components, time-ordered, same format as
# the old Railway logs) -- this is what /logs and the nightly Telegram report
# send. Rotates at 10 MB, keeps 10 files (~weeks of history on the server).
LOG_DIR = os.getenv("LOG_DIR", "logs")
COMBINED_LOG = os.path.join(LOG_DIR, "bot.log")
_combined = None


def _combined_handler(formatter):
    global _combined
    if _combined is None:
        os.makedirs(LOG_DIR, exist_ok=True)
        _combined = RotatingFileHandler(COMBINED_LOG, maxBytes=10 * 1024 * 1024, backupCount=10)
        _combined.setFormatter(formatter)
    return _combined


def setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    level = logging.DEBUG if os.getenv("DEBUG", "false").lower() == "true" else logging.INFO
    logger.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s | %(name)-12s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler with rotation (10MB, keep 5 files)
    os.makedirs("logs", exist_ok=True)
    fh = RotatingFileHandler(
        f"logs/{name}.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(_combined_handler(formatter))

    return logger
