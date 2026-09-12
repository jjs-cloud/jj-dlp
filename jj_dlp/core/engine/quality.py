"""Per-recording ffmpeg-error tracking, LQ fallback, and quality-upgrade retries."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from jj_dlp.core.config.app import AppConfig
from jj_dlp.core.engine.site_state import SiteState

log = logging.getLogger("jj_dlp.engine.quality")

DEFAULT_UPGRADE_RETRY_INTERVAL_SEC = 3600  # hourly, per Doc 1 §5.8

_FFMPEG_ERROR_PATTERN = re.compile(r"\berror\b", re.IGNORECASE)

# (streamer, in_progress_path) -> None; the caller restarts the recording via recording.py.
RestartCallback = Callable[[str, str], None]


@dataclass
class _StreamerQualityTrack:
    """Per-streamer quality bookkeeping, reset on every fresh recording attempt."""

    lq_active: bool = False
    last_upgrade_attempt: Optional[float] = None


def is_ffmpeg_error_line(line: str) -> bool:
    """Return whether a downloader output line looks like an ffmpeg error."""
    return bool(_FFMPEG_ERROR_PATTERN.search(line))


class QualityMonitor:
    """Tracks ffmpeg errors for one site's streamers and drives LQ fallback/upgrade restarts."""

    def __init__(
        self,
        app_config: AppConfig,
        site_config: dict,
        site_state: SiteState,
        on_restart: RestartCallback,
        upgrade_interval: float = DEFAULT_UPGRADE_RETRY_INTERVAL_SEC,
    ) -> None:
        self.app_config = app_config
        self.site_config = site_config
        self.site_state = site_state
        self.on_restart = on_restart
        self.upgrade_interval = upgrade_interval
        self._lock = threading.Lock()
        self._tracks: Dict[str, _StreamerQualityTrack] = {}

    def _track_for(self, streamer: str) -> _StreamerQualityTrack:
        """Return (creating if needed) a streamer's quality tracking record."""
        with self._lock:
            return self._tracks.setdefault(streamer, _StreamerQualityTrack())

    def is_lq_active(self, streamer: str) -> bool:
        """Return whether a streamer's next/current restart should use lq_downloader."""
        return self._track_for(streamer).lq_active

    def reset(self, streamer: str) -> None:
        """Zero a streamer's per-recording ffmpeg-error count for a fresh attempt."""
        self.site_state.set_ffmpeg_error_count(streamer, 0)

    def _lq_enabled(self) -> bool:
        """Return whether the LQ-fallback mechanism is enabled app-wide."""
        return self.app_config.recording.lq_downloader_enabled

    def on_downloader_line(self, streamer: str, line: str) -> None:
        """Bump a streamer's ffmpeg-error count on a matching line; switch to LQ past threshold."""
        if not is_ffmpeg_error_line(line):
            return
        if not self._lq_enabled():
            log.debug("ffmpeg error for %s (LQ fallback disabled app-wide): %s", streamer, line)
            return
        count = self.site_state.increment_ffmpeg_error_count(streamer)
        threshold = self.app_config.recording.ff_err_threshold
        if count < threshold:
            return
        track = self._track_for(streamer)
        if track.lq_active:
            return
        track.lq_active = True
        track.last_upgrade_attempt = time.time()
        path = self.site_state.get_in_progress_path(streamer)
        if path is None:
            return
        log.warning("ffmpeg error threshold crossed for %s, switching to LQ", streamer)
        self._kill(streamer)
        self.on_restart(streamer, path)

    def _kill(self, streamer: str) -> None:
        """Force-kill a streamer's current process ahead of a quality-driven restart."""
        process = self.site_state.get_process(streamer)
        if process is not None:
            process.kill()

    def check_upgrade_once(self, now: Optional[float] = None) -> None:
        """Check every LQ-active streamer for whether it's due a quality-upgrade attempt."""
        if not self._lq_enabled() or not self.site_config.get("upgrade_quality", True):
            return
        now = time.time() if now is None else now
        for streamer in list(self._tracks):
            track = self._track_for(streamer)
            if not track.lq_active or not self.site_state.is_recording(streamer):
                continue
            if track.last_upgrade_attempt is None:
                track.last_upgrade_attempt = now
                continue
            if now - track.last_upgrade_attempt < self.upgrade_interval:
                continue
            path = self.site_state.get_in_progress_path(streamer)
            if path is None:
                continue
            log.info("Attempting quality upgrade restart for %s", streamer)
            track.last_upgrade_attempt = now
            track.lq_active = False
            self.site_state.set_ffmpeg_error_count(streamer, 0)
            self._kill(streamer)
            self.on_restart(streamer, path)

    def run_loop(self, stop_event: threading.Event, poll_interval: float = 60.0) -> None:
        """Run check_upgrade_once repeatedly at a coarse poll interval."""
        while not stop_event.is_set():
            self.check_upgrade_once()
            stop_event.wait(poll_interval)
