"""Per-site runtime state and read-only snapshot."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from jj_dlp.core.config import state as config_state


@dataclass
class StreamerRuntimeFlags:
    """In-memory-only flags for one streamer, never persisted to disk."""

    stall_since: Optional[float] = None
    ffmpeg_error_count: int = 0
    ad_alert_active: bool = False
    write_failure: bool = False


@dataclass(frozen=True)
class StreamerSnapshot:
    """Read-only view of one streamer's current state, safe for a frontend to read."""

    streamer: str
    recording: bool
    in_progress_path: Optional[str]
    last_live: Optional[float]
    live_since: Optional[float]
    stall_since: Optional[float]
    ffmpeg_error_count: int
    ad_alert_active: bool
    write_failure: bool


@dataclass(frozen=True)
class SiteSnapshot:
    """Read-only view of a site's runtime state across all its streamers."""

    site: str
    streamers: Dict[str, StreamerSnapshot]


class SiteState:
    """Per-site runtime state: process registry, live status, and in-memory flags."""

    def __init__(self, data_dir: Path, site: str) -> None:
        self.data_dir = Path(data_dir)
        self.site = site
        self._lock = threading.Lock()
        self._processes: Dict[str, Any] = {}
        self._in_progress_paths: Dict[str, str] = {}
        self._flags: Dict[str, StreamerRuntimeFlags] = {}

    def _flags_for(self, streamer: str) -> StreamerRuntimeFlags:
        """Return (creating if needed) a streamer's in-memory flags object."""
        return self._flags.setdefault(streamer, StreamerRuntimeFlags())

    # --- process registry ---

    def set_process(self, streamer: str, process: Any) -> None:
        """Register the running process for a streamer's active recording."""
        with self._lock:
            self._processes[streamer] = process

    def clear_process(self, streamer: str) -> None:
        """Remove a streamer's process registration once it has exited."""
        with self._lock:
            self._processes.pop(streamer, None)

    def get_process(self, streamer: str) -> Optional[Any]:
        """Return a streamer's currently registered process, or None."""
        with self._lock:
            return self._processes.get(streamer)

    def is_recording(self, streamer: str) -> bool:
        """Return whether a streamer currently has a registered process."""
        with self._lock:
            return streamer in self._processes

    # --- in-progress output path ---

    def set_in_progress_path(self, streamer: str, path: Optional[str]) -> None:
        """Set (or, if path is None, clear) a streamer's in-progress output path."""
        with self._lock:
            if path is None:
                self._in_progress_paths.pop(streamer, None)
            else:
                self._in_progress_paths[streamer] = path

    def get_in_progress_path(self, streamer: str) -> Optional[str]:
        """Return a streamer's in-progress output path, or None if not recording."""
        with self._lock:
            return self._in_progress_paths.get(streamer)

    # --- live/offline status (persisted) ---

    def mark_live(self, streamer: str, when: Optional[float] = None) -> None:
        """Record that a streamer is now live, persisting last_live and live_since."""
        ts = time.time() if when is None else when
        config_state.set_last_live(self.data_dir, self.site, streamer, ts)
        config_state.set_live_since(self.data_dir, self.site, streamer, ts)

    def mark_offline(self, streamer: str) -> None:
        """Record that a streamer is no longer live, clearing live_since."""
        config_state.set_live_since(self.data_dir, self.site, streamer, None)

    def get_last_live(self, streamer: str) -> Optional[float]:
        """Return the epoch timestamp a streamer was last seen live."""
        return config_state.get_live_status(self.data_dir, self.site, streamer).get("last_live")

    def get_live_since(self, streamer: str) -> Optional[float]:
        """Return the epoch timestamp the current live session began, or None if offline."""
        return config_state.get_live_status(self.data_dir, self.site, streamer).get("live_since")

    # --- in-memory-only flags ---

    def set_stall_since(self, streamer: str, when: Optional[float]) -> None:
        """Set (or clear) the timestamp a streamer's current recording was detected stalled."""
        with self._lock:
            self._flags_for(streamer).stall_since = when

    def set_ffmpeg_error_count(self, streamer: str, count: int) -> None:
        """Set a streamer's per-recording ffmpeg-error count."""
        with self._lock:
            self._flags_for(streamer).ffmpeg_error_count = count

    def increment_ffmpeg_error_count(self, streamer: str) -> int:
        """Increment and return a streamer's per-recording ffmpeg-error count."""
        with self._lock:
            flags = self._flags_for(streamer)
            flags.ffmpeg_error_count += 1
            return flags.ffmpeg_error_count

    def set_ad_alert_active(self, streamer: str, active: bool) -> None:
        """Set whether a streamer's current output is flagged as an ad segment."""
        with self._lock:
            self._flags_for(streamer).ad_alert_active = active

    def set_write_failure(self, streamer: str, failed: bool) -> None:
        """Set (or clear, on acknowledgment) a streamer's persistent write-failure flag."""
        with self._lock:
            self._flags_for(streamer).write_failure = failed

    def reset_recording_flags(self, streamer: str) -> None:
        """Clear the per-recording flags (stall, ffmpeg errors, ad alert) for a new attempt."""
        with self._lock:
            flags = self._flags_for(streamer)
            flags.stall_since = None
            flags.ffmpeg_error_count = 0
            flags.ad_alert_active = False

    def known_streamers(self) -> list:
        """Return every streamer this SiteState currently has any runtime data for."""
        with self._lock:
            names = set(self._processes) | set(self._in_progress_paths) | set(self._flags)
        return sorted(names)

    # --- snapshot ---

    def snapshot(self) -> SiteSnapshot:
        """Return a plain, read-only snapshot of every known streamer's current state."""
        streamers: Dict[str, StreamerSnapshot] = {}
        for name in self.known_streamers():
            with self._lock:
                flags = self._flags_for(name)
                recording = name in self._processes
                in_progress_path = self._in_progress_paths.get(name)
                stall_since = flags.stall_since
                ffmpeg_error_count = flags.ffmpeg_error_count
                ad_alert_active = flags.ad_alert_active
                write_failure = flags.write_failure
            streamers[name] = StreamerSnapshot(
                streamer=name,
                recording=recording,
                in_progress_path=in_progress_path,
                last_live=self.get_last_live(name),
                live_since=self.get_live_since(name),
                stall_since=stall_since,
                ffmpeg_error_count=ffmpeg_error_count,
                ad_alert_active=ad_alert_active,
                write_failure=write_failure,
            )
        return SiteSnapshot(site=self.site, streamers=streamers)
