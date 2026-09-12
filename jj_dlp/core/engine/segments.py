"""Segment/part continuation for split recordings."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from jj_dlp.core.config import state as config_state
from jj_dlp.core.engine.site_state import SiteState

log = logging.getLogger("jj_dlp.engine.segments")

_EXT_MARKER = ".%(ext)s"

# (streamer, new_output_path) -> None; the caller restarts the recording at the given path.
RestartCallback = Callable[[str, str], None]


def part_suffix(part: int) -> str:
    """Return the literal filename suffix for a given part number (empty for part 1)."""
    return "" if part <= 1 else f" part{part:03d}"


def apply_part_suffix(template_path: str, part: int) -> str:
    """Insert a part-number suffix into an output path, just before its extension placeholder."""
    suffix = part_suffix(part)
    if not suffix:
        return template_path
    if template_path.endswith(_EXT_MARKER):
        return template_path[: -len(_EXT_MARKER)] + suffix + _EXT_MARKER
    return template_path + suffix


class SegmentTracker:
    """Tracks per-streamer split-recording part numbers and persists continuation across restarts."""

    def __init__(self, data_dir: Path, site_label: str, site_state: SiteState) -> None:
        self.data_dir = Path(data_dir)
        self.site_label = site_label
        self.site_state = site_state
        self._lock = threading.Lock()
        self._parts: Dict[str, int] = {}
        self._part_start: Dict[str, float] = {}
        self._base_path: Dict[str, str] = {}

    def start(self, streamer: str, base_output_path: str, now: Optional[float] = None) -> str:
        """Begin a fresh part-1 session for a streamer, clearing any stale continuation."""
        with self._lock:
            self._parts[streamer] = 1
            self._base_path[streamer] = base_output_path
            self._part_start[streamer] = time.time() if now is None else now
        config_state.set_segment_continuation(self.data_dir, self.site_label, streamer, None, None)
        return base_output_path

    def current_part(self, streamer: str) -> int:
        """Return a streamer's current part number (1 if not tracked)."""
        with self._lock:
            return self._parts.get(streamer, 1)

    def elapsed_in_part(self, streamer: str, now: Optional[float] = None) -> float:
        """Return seconds elapsed since a streamer's current part started."""
        now = time.time() if now is None else now
        with self._lock:
            start = self._part_start.get(streamer)
        return 0.0 if start is None else now - start

    def advance(self, streamer: str, previous_path: str, now: Optional[float] = None) -> str:
        """Advance a streamer to its next part, persisting continuation, and return the new output path."""
        with self._lock:
            base = self._base_path.get(streamer, previous_path)
            next_part = self._parts.get(streamer, 1) + 1
            self._parts[streamer] = next_part
            self._part_start[streamer] = time.time() if now is None else now
        next_path = apply_part_suffix(base, next_part)
        config_state.set_segment_continuation(
            self.data_dir, self.site_label, streamer, next_part, previous_path
        )
        return next_path

    def resume_from_state(self, streamer: str, base_output_path: str) -> Optional[str]:
        """On startup, resume a streamer's in-progress split sequence from persisted state, if any."""
        continuation = config_state.get_segment_continuation(self.data_dir, self.site_label, streamer)
        if not continuation:
            return None
        part = continuation.get("next_part")
        if not part:
            return None
        with self._lock:
            self._parts[streamer] = part
            self._base_path[streamer] = base_output_path
            self._part_start[streamer] = time.time()
        return apply_part_suffix(base_output_path, part)

    def clear(self, streamer: str) -> None:
        """Drop tracking for a streamer and clear its persisted continuation, once recording ends."""
        with self._lock:
            self._parts.pop(streamer, None)
            self._part_start.pop(streamer, None)
            self._base_path.pop(streamer, None)
        config_state.set_segment_continuation(self.data_dir, self.site_label, streamer, None, None)


class SegmentMonitor:
    """Per-site monitor that triggers the next part's restart once split_after_minutes elapses."""

    def __init__(
        self,
        site_config: dict,
        site_state: SiteState,
        tracker: SegmentTracker,
        on_split: RestartCallback,
    ) -> None:
        self.site_config = site_config
        self.site_state = site_state
        self.tracker = tracker
        self.on_split = on_split

    def _split_minutes(self) -> int:
        """Return the site's configured split_after_minutes (0 means splitting is disabled)."""
        return self.site_config.get("timing", {}).get("split_after_minutes", 0)

    def check_once(self, now: Optional[float] = None) -> None:
        """Check every currently-recording streamer for whether its part has run long enough to split."""
        split_minutes = self._split_minutes()
        if not split_minutes:
            return
        now = time.time() if now is None else now
        limit_sec = split_minutes * 60
        for streamer in self.site_config.get("streamers", []):
            if not self.site_state.is_recording(streamer):
                continue
            if self.tracker.elapsed_in_part(streamer, now) < limit_sec:
                continue
            path = self.site_state.get_in_progress_path(streamer)
            if path is None:
                continue
            log.info("Split boundary reached for %s, starting next part", streamer)
            self._kill(streamer)
            new_path = self.tracker.advance(streamer, path, now)
            self.on_split(streamer, new_path)

    def _kill(self, streamer: str) -> None:
        """Force-kill a streamer's current process ahead of a split-boundary restart."""
        process = self.site_state.get_process(streamer)
        if process is not None:
            process.kill()

    def run_loop(self, stop_event: threading.Event, poll_interval: float = 30.0) -> None:
        """Run check_once repeatedly, sleeping poll_interval between passes."""
        while not stop_event.is_set():
            self.check_once()
            stop_event.wait(poll_interval)
