"""Persistent management log for operational events.

Writes to ``~/.agenticore/agenticore.log`` (or ``$AGENTICORE_HOME/agenticore.log``).
Uses a RotatingFileHandler (5 MB max, 3 backups) so disk usage stays bounded.

Usage::

    from agenticore.mgmt_log import get_mgmt_logger
    mgmt = get_mgmt_logger()
    mgmt.info("hot-reload agentihooks OK ref=%s", ref)
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_mgmt: Optional[logging.Logger] = None


def get_mgmt_logger() -> logging.Logger:
    """Return the singleton management logger (file-only, no stderr)."""
    global _mgmt
    if _mgmt is not None:
        return _mgmt

    log_dir = Path(os.environ.get("AGENTICORE_HOME", str(Path.home() / ".agenticore")))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "agenticore.log"

    _mgmt = logging.getLogger("agenticore.mgmt")
    _mgmt.setLevel(logging.INFO)
    _mgmt.propagate = False  # file-only — don't duplicate to stderr

    handler = RotatingFileHandler(
        str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _mgmt.addHandler(handler)
    return _mgmt
