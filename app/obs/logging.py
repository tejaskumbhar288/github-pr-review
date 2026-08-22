"""Logging setup shared by the CLI, the webhook service and the worker."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO", *, quiet_libs: bool = True) -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(level)

    if quiet_libs:
        for noisy in ("httpx", "httpcore", "urllib3", "langfuse", "uvicorn.access"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
