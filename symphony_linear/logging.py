"""Structured logging setup for symphony-lite."""

import logging
import sys

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(*, debug: bool = False) -> None:
    """Configure root logger for structured console output.

    Args:
        debug: If ``True``, set log level to DEBUG; otherwise INFO.
    """
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT))
    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers when called multiple times.
    root.handlers.clear()
    root.addHandler(handler)
    # Keep third-party loggers quieter unless debugging.
    if not debug:
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger for the given module name."""
    return logging.getLogger(name)
