"""Bridges stdlib logging into an in-memory ring buffer for the curses Log tab."""

from __future__ import annotations

import collections
import logging
import threading
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

RECENT_LINES_MAXLEN = 2000
LOGGER_NAMESPACE = "jj_dlp"

# Single formatter shared by debug.log and the Log tab, so both render identical text.
_LINE_FORMATTER = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

_lock = threading.Lock()
_recent_lines: Deque[Tuple[str, str, str]] = collections.deque(maxlen=RECENT_LINES_MAXLEN)
_tag_filter: Dict[str, bool] = {}
_handler: Optional["_RingBufferHandler"] = None


class _RingBufferHandler(logging.Handler):
    """Formats each record once and fans it out to the ring buffer and, if enabled, debug.log."""

    def __init__(self) -> None:
        super().__init__()
        self._file = None

    def set_file(self, path: Optional[Path]) -> None:
        """Swap the underlying debug.log file handle, closing any previous one."""
        with _lock:
            if self._file is not None:
                self._file.close()
                self._file = None
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._file = open(path, "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        tag = record.name.rsplit(".", 1)[-1]
        line = self.format(record).replace("\n", " | ")
        with _lock:
            _recent_lines.append((line, tag, record.levelname))
            _tag_filter.setdefault(tag, True)
            if self._file is not None:
                self._file.write(line + "\n")
                self._file.flush()


def configure(data_dir: Path, app_cfg) -> None:
    """Attach the ring-buffer handler to the jj_dlp logger tree and point it at debug.log if enabled."""
    global _handler
    data_dir = Path(data_dir)
    debug = app_cfg.get_debug()
    root = logging.getLogger(LOGGER_NAMESPACE)
    root.setLevel(logging.DEBUG)

    if _handler is None:
        _handler = _RingBufferHandler()
        _handler.setFormatter(_LINE_FORMATTER)
        root.addHandler(_handler)

    _handler.set_file(data_dir / debug.log_path if debug.enabled else None)


def get_recent_lines(include_filtered: bool = False) -> List[Tuple[str, str, str]]:
    """Return recent (line, tag, levelname) entries, honoring the tag filter unless overridden."""
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
