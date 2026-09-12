"""Write-failure detection and alerting."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional

from jj_dlp.core.engine.site_state import SiteState

log = logging.getLogger("jj_dlp.engine.write_failure")

DEFAULT_QUICK_EXIT_SEC = 5.0
DEFAULT_CONSECUTIVE_THRESHOLD = 3


def _has_nonempty_file(path: Optional[str]) -> bool:
    """Return whether path exists and is a non-empty file."""
    if not path:
        return False
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


@dataclass
class _StreamerTrack:
    """Per-streamer consecutive quick-empty-exit count."""

    consecutive_failures: int = 0


class WriteFailureMonitor:
    """Detects repeated quick, empty-output process exits and flags them on site_state."""

    def __init__(
        self,
        site_state: SiteState,
        quick_exit_sec: float = DEFAULT_QUICK_EXIT_SEC,
        consecutive_threshold: int = DEFAULT_CONSECUTIVE_THRESHOLD,
    ) -> None:
        self.site_state = site_state
        self.quick_exit_sec = quick_exit_sec
        self.consecutive_threshold = consecutive_threshold
        self._tracks: Dict[str, _StreamerTrack] = {}

    def _track_for(self, streamer: str) -> _StreamerTrack:
        """Return (creating if needed) a streamer's tracking record."""
        return self._tracks.setdefault(streamer, _StreamerTrack())

    def on_process_exit(
        self,
        streamer: str,
        output_path: Optional[str],
        started_at: float,
        ended_at: Optional[float] = None,
    ) -> None:
        """Update a streamer's write-failure tracking after one recording attempt exits."""
        ended_at = time.time() if ended_at is None else ended_at
        quick = (ended_at - started_at) < self.quick_exit_sec
        produced = _has_nonempty_file(output_path)
        track = self._track_for(streamer)

        if quick and not produced:
            track.consecutive_failures += 1
            if track.consecutive_failures >= self.consecutive_threshold:
                log.warning("Write failure detected for %s (%s)", streamer, output_path)
                self.site_state.set_write_failure(streamer, True)
        else:
            track.consecutive_failures = 0

    def acknowledge(self, streamer: str) -> None:
        """Clear a streamer's write-failure flag once the user has acknowledged it in the UI."""
        self._tracks.pop(streamer, None)
        self.site_state.set_write_failure(streamer, False)
