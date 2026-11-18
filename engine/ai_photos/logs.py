"""Rotating log files under <home>/logs. Set up by process entry points only, never on import."""
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG = logging.getLogger(__name__)
MAX_BYTES = 1024 * 1024
BACKUPS = 3


def setup_logging(name: str, home: Path) -> Path:
    """Log to <home>/logs/<name>.log, repeating warnings on stderr for the app to show."""
    folder = Path(home) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.log"
    to_file = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
    to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    to_stderr = logging.StreamHandler(sys.stderr)
    to_stderr.setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, handlers=[to_file, to_stderr], force=True)

    previous = sys.excepthook

    def log_crash(kind, value, traceback):
        LOG.critical("Uncaught exception", exc_info=(kind, value, traceback))
        previous(kind, value, traceback)

    def log_thread_crash(args):
        if args.exc_type is not SystemExit:
            LOG.critical("Uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = log_crash
    threading.excepthook = log_thread_crash
    return path
