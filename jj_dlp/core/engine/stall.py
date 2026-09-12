"""Stall detection and restart."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from jj_dlp.core.engine.site_state import SiteState

log = logging.getLogger("jj_dlp.engine.stall")

# (streamer, output_path) -> None; the caller restarts the recording via recording.py.
RestartCallback = Callable[[str, str], None]


@dataclass
class _StreamerTrack:
    """Per-streamer file-size tracking for stall detection."""

    path: Optional[str] = None
    last_size: int = -1
    last_growth: float = 0.0


def _file_size(path: str) -> int:
    """Return a file's current size in bytes, or 0 if it doesn't exist yet."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class StallMonitor:
    """Polls one site's active recordings for file-size growth and restarts stalled ones."""

    def __init__(
        self,
        site_config: dict,
        site_state: SiteState,
        on_stall: RestartCallback,
    ) -> None:
        self.site_config = site_config
        self.site_state = site_state
        self.on_stall = on_stall
        self._tracks: Dict[str, _StreamerTrack] = {}

    def _track_for(self, streamer: str) -> _StreamerTrack:
        """Return (creating if needed) a streamer's tracking record."""
        return self._tracks.setdefault(streamer, _StreamerTrack())

    def reset(self, streamer: str) -> None:
        """Clear tracking for a streamer, called whenever a fresh recording attempt starts."""
        self._tracks.pop(streamer, None)

    def check_once(self, now: Optional[float] = None) -> None:
        """Check every currently-recording streamer once for stalled file growth."""
        now = time.time() if now is None else now
        stall_timeout = self.site_config["timing"]["stall_timeout"]
        for streamer in self.site_config.get("streamers", []):
            path = self.site_state.get_in_progress_path(streamer)
            if not self.site_state.is_recording(streamer) or path is None:
                self.reset(streamer)
                continue

            track = self._track_for(streamer)
            size = _file_size(path)
            if track.path != path or size > track.last_size:
                track.path = path
                track.last_size = size
                track.last_growth = now
                self.site_state.set_stall_since(streamer, None)
                continue

            if now - track.last_growth >= stall_timeout:
                self.site_state.set_stall_since(streamer, track.last_growth)
                log.warning("Stall detected for %s, restarting", streamer)
                self._kill(streamer)
                self.reset(streamer)
                self.on_stall(streamer, path)

    def _kill(self, streamer: str) -> None:
        """Force-kill a stalled streamer's current process."""
        process = self.site_state.get_process(streamer)
        if process is not None:
            process.kill()

    def run_loop(self, stop_event: threading.Event) -> None:
        """Run check_once repeatedly, sleeping stall_check_interval between passes."""
        interval = self.site_config["timing"]["stall_check_interval"]
        while not stop_event.is_set():
            self.check_once()
            stop_event.wait(interval)
