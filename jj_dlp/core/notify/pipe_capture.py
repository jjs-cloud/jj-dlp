"""Per-streamer stdout/stderr ring buffers capturing raw downloader output."""

from __future__ import annotations

import collections
import threading
from typing import Callable, Deque, Dict, List, Tuple

BUFFER_MAXLEN = 500

LineFeed = Callable[[str, str], None]

_lock = threading.Lock()
_buffers: Dict[Tuple[str, str, str], Deque[str]] = {}


def append_line(site: str, streamer: str, stream: str, line: str) -> None:
    """Add one line of downloader output to a streamer's stdout or stderr buffer."""
    key = (site, streamer, stream)
    with _lock:
        buf = _buffers.setdefault(key, collections.deque(maxlen=BUFFER_MAXLEN))
        buf.append(line)


def get_lines(site: str, streamer: str, stream: str) -> List[str]:
    """Return a snapshot of buffered lines for one streamer's stdout or stderr."""
    key = (site, streamer, stream)
    with _lock:
        return list(_buffers.get(key, ()))


def get_streamers() -> List[Tuple[str, str]]:
    """Return every (site, streamer) pair with any captured output, in first-seen order."""
    with _lock:
        seen: List[Tuple[str, str]] = []
        for site, streamer, _stream in _buffers.keys():
            pair = (site, streamer)
            if pair not in seen:
                seen.append(pair)
        return seen


def clear_streamer(site: str, streamer: str) -> None:
    """Drop both stdout and stderr buffers for one streamer."""
    with _lock:
        for stream in ("stdout", "stderr"):
            _buffers.pop((site, streamer, stream), None)


def feed(site: str, streamer: str) -> LineFeed:
    """Return an on_line callback bound to one site/streamer, for engine/downloader.py to feed."""

    def _on_line(stream: str, line: str) -> None:
        append_line(site, streamer, stream, line)

    return _on_line
