"""Tag-filtered debug logging, recent-lines buffer, and crash log."""

from __future__ import annotations

import collections
import datetime
import threading
import traceback
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from jj_dlp.core.config import app as app_config

RECENT_LINES_MAXLEN = 2000
CRASH_LOG_RELPATH = Path("logs") / "crash.log"

_lock = threading.Lock()
_recent_lines: Deque[Tuple[str, str, str]] = collections.deque(maxlen=RECENT_LINES_MAXLEN)
_tag_filter: Dict[str, bool] = {}
_debug_enabled = False
_debug_log_path: Optional[Path] = None
_crash_log_path: Optional[Path] = None


def configure(data_dir: Path, config: Optional[app_config.AppConfig] = None) -> None:
    """Set debug-log enabled/path and crash-log path from app config."""
    global _debug_enabled, _debug_log_path, _crash_log_path
    data_dir = Path(data_dir)
    cfg = config or app_config.load(data_dir)
    debug = cfg.get_debug()
    with _lock:
        _debug_enabled = debug.enabled
        _debug_log_path = data_dir / debug.log_path
        _crash_log_path = data_dir / CRASH_LOG_RELPATH


def dbg(msg: str, tag: str = "general") -> None:
    """Record a debug line under a tag; also appends to the debug log file if enabled."""
    ts = _timestamp()
    with _lock:
        _recent_lines.append((ts, tag, msg))
        _tag_filter.setdefault(tag, True)
        enabled = _debug_enabled
        path = _debug_log_path
    if enabled and path is not None:
        _append(path, f"[{ts}] [{tag}] {msg}")


def get_recent_lines(include_filtered: bool = False) -> List[Tuple[str, str, str]]:
    """Return recent (timestamp, tag, msg) lines, honoring the tag filter unless overridden."""
    with _lock:
        lines = list(_recent_lines)
        filters = dict(_tag_filter)
    if include_filtered:
        return lines
    return [line for line in lines if filters.get(line[1], True)]


def get_known_tags() -> List[str]:
    """Return every tag seen so far, in first-seen order."""
    with _lock:
        return list(_tag_filter.keys())


def is_tag_enabled(tag: str) -> bool:
    """Return whether a tag is currently shown (not filtered out)."""
    with _lock:
        return _tag_filter.get(tag, True)


def set_tag_enabled(tag: str, enabled: bool) -> None:
    """Toggle a tag's visibility in the Log tab filter. Filtering never affects the log file."""
    with _lock:
        _tag_filter[tag] = enabled


def log_crash(exc: BaseException) -> None:
    """Append a timestamped traceback for an uncaught exception to the crash log."""
    with _lock:
        path = _crash_log_path
    if path is None:
        return
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _append(path, f"=== crash at {_timestamp()} ===\n{text}")


def _timestamp() -> str:
    """Return the current local time as a sortable timestamp string."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _append(path: Path, text: str) -> None:
    """Append a line/block of text to a log file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + "\n")
