"""Bridges stdlib logging into an in-memory ring buffer for the curses Log tab."""

from __future__ import annotations

import collections
import datetime
import logging
import threading
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

RECENT_LINES_MAXLEN = 2000
LOGGER_NAMESPACE = "jj_dlp"

_lock = threading.Lock()
_recent_lines: Deque[Tuple[str, str, str]] = collections.deque(maxlen=RECENT_LINES_MAXLEN)
_tag_filter: Dict[str, bool] = {}
_handler: Optional["_RingBufferHandler"] = None
_file_handler: Optional[logging.FileHandler] = None


class _RingBufferHandler(logging.Handler):
    """Appends each record's (timestamp, tag, message) to the shared ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        tag = record.name.rsplit(".", 1)[-1]
        msg = self.format(record).replace("\n", " | ")
        ts = datetime.datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        with _lock:
            _recent_lines.append((ts, tag, msg))
            _tag_filter.setdefault(tag, True)


def configure(data_dir: Path, app_cfg) -> None:
    """Attach the ring-buffer handler to the jj_dlp logger tree, plus a file handler if debug.enabled."""
    global _handler, _file_handler
    data_dir = Path(data_dir)
    debug = app_cfg.get_debug()
    root = logging.getLogger(LOGGER_NAMESPACE)
    root.setLevel(logging.DEBUG)

    if _handler is None:
        _handler = _RingBufferHandler()
        _handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(_handler)

    if _file_handler is not None:
        root.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
    if debug.enabled:
        log_path = data_dir / debug.log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = logging.FileHandler(log_path, encoding="utf-8")
        _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(_file_handler)


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
    """Toggle a tag's visibility in the Log tab filter."""
    with _lock:
        _tag_filter[tag] = enabled
