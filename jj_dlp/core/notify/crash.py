"""Crash log: unfiltered traceback dump for uncaught exceptions, kept separate from normal logging."""

from __future__ import annotations

import datetime
import threading
import traceback
from pathlib import Path
from typing import Optional

CRASH_LOG_RELPATH = Path("logs") / "crash.log"

_lock = threading.Lock()
_crash_log_path: Optional[Path] = None


def configure(data_dir: Path) -> None:
    """Set the crash log path for this run."""
    global _crash_log_path
    with _lock:
        _crash_log_path = Path(data_dir) / CRASH_LOG_RELPATH


def log_crash(exc: BaseException) -> None:
    """Append a timestamped traceback for an uncaught exception to the crash log."""
    with _lock:
        path = _crash_log_path
    if path is None:
        return
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"=== crash at {_timestamp()} ===\n{text}\n")


def _timestamp() -> str:
    """Return the current local time as a sortable timestamp string."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
