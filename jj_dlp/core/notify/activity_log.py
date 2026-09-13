"""In-memory ring buffer of human-readable activity lines, per site and combined."""

from __future__ import annotations

import collections
import datetime
import threading
from typing import Deque, Dict, List, Optional, Tuple

from jj_dlp.core.notify import logger

LINES_MAXLEN = 500

_lock = threading.Lock()
_per_site: Dict[str, Deque[Tuple[str, str]]] = {}
_combined: Deque[Tuple[str, str, str]] = collections.deque(maxlen=LINES_MAXLEN)


def log_activity(site: str, msg: str) -> None:
    """Record one activity line for a site, and mirror it into the debug log."""
    ts = _timestamp()
    with _lock:
        buf = _per_site.setdefault(site, collections.deque(maxlen=LINES_MAXLEN))
        buf.append((ts, msg))
        _combined.append((ts, site, msg))
    logger.dbg(msg, tag=site)


def get_site_lines(site: str) -> List[Tuple[str, str]]:
    """Return a snapshot of (timestamp, msg) lines for one site."""
    with _lock:
        return list(_per_site.get(site, ()))


def get_combined_lines() -> List[Tuple[str, str, str]]:
    """Return a snapshot of (timestamp, site, msg) lines across all sites."""
    with _lock:
        return list(_combined)


def get_known_sites() -> List[str]:
    """Return every site with any recorded activity, in first-seen order."""
    with _lock:
        return list(_per_site.keys())


def clear(site: Optional[str] = None) -> None:
    """Clear one site's buffer, or every buffer (including combined) if no site given."""
    with _lock:
        if site is None:
            _per_site.clear()
            _combined.clear()
        else:
            _per_site.pop(site, None)


def _timestamp() -> str:
    """Return the current local time as a sortable timestamp string."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
