"""
logging_utils.py
----------------
Centralised logger setup. Used across all modules.

Rotating file handler keeps the Pi disk tidy. Console handler for dev visibility.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import config


_LOGGER_INITIALISED = False


def get_logger(name: str = "ics") -> logging.Logger:
    """Return a configured logger. Idempotent."""
    global _LOGGER_INITIALISED
    logger = logging.getLogger(name)

    if _LOGGER_INITIALISED:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        config.LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)

    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(sh)

    logger.propagate = False
    _LOGGER_INITIALISED = True
    return logger
