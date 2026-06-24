"""Logging configuration."""

import logging
import os
from logging.handlers import RotatingFileHandler


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

    return logger
