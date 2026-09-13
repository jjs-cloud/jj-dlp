"""Disk free-space and write-rate sampling into rolling history."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from jj_dlp.core.config import state as config_state
from jj_dlp.core.config.app import AppConfig
from jj_dlp.core.engine.site_state import SiteState

log = logging.getLogger("jj_dlp.engine.disk")


@dataclass
class SiteDiskSource:
    """One loaded site's output directory and runtime state, for disk sampling."""

    label: str
    output_dir: str
    site_state: SiteState
    streamers: List[str]


def _drive_root(path: str) -> str:
    """Return the nearest existing ancestor of a path, for stat/disk_usage purposes."""
    try:
        p = Path(path).resolve()
    except OSError:
        p = Path(path).absolute()
    while not p.exists() and p.parent != p:
        p = p.parent
    return str(p)


def resolve_drives(app_config: AppConfig, sources: Iterable[SiteDiskSource]) -> List[str]:
    """Return drive paths to monitor: disk.drives if set, else each source's drive, deduped."""
    configured = app_config.get_disk().drives
    if configured:
        return list(dict.fromkeys(configured))
    seen: Dict[Any, str] = {}
    for source in sources:
        root = _drive_root(source.output_dir)
        try:
            key: Any = os.stat(root).st_dev
        except OSError:
            key = root
        seen.setdefault(key, root)
    return list(seen.values())


def _free_space(drive: str) -> int:
    """Return free bytes on a drive path, or 0 if it can't be statted."""
    try:
        return shutil.disk_usage(drive).free
    except OSError:
        return 0


def _file_size(path: str) -> int:
    """Return a file's current size in bytes, or 0 if it doesn't exist."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class DiskSampler:
    """Samples free space and total write rate across every loaded site's output drives."""

    def __init__(self, data_dir: Path, app_config: AppConfig, sources: List[SiteDiskSource]) -> None:
        self.data_dir = Path(data_dir)
        self.app_config = app_config
        self.sources = sources
        self._prev_sizes: Dict[Tuple[str, str], int] = {}
        self._prev_time: Optional[float] = None
        self._current_rate: float = 0.0
        self._current_free: int = 0
        self._lock = threading.Lock()

    def _active_paths(self) -> Dict[Tuple[str, str], str]:
        """Return {(site, streamer): in_progress_path} for every currently-recording streamer."""
        paths: Dict[Tuple[str, str], str] = {}
        for source in self.sources:
            for streamer in source.streamers:
                path = source.site_state.get_in_progress_path(streamer)
                if path is not None:
                    paths[(source.label, streamer)] = path
        return paths

    def sample_once(self, now: Optional[float] = None) -> float:
        """Take one sample: free space + write rate, push the rate into history, and return it."""
        now = time.time() if now is None else now
        drives = resolve_drives(self.app_config, self.sources)
        free = sum(_free_space(d) for d in drives)

        sizes = {key: _file_size(path) for key, path in self._active_paths().items()}

        rate = 0.0
        if self._prev_time is not None:
            elapsed = now - self._prev_time
            if elapsed > 0:
                written = sum(
                    max(0, size - self._prev_sizes.get(key, size)) for key, size in sizes.items()
                )
                rate = written / elapsed

        self._prev_sizes = sizes
        self._prev_time = now

        with self._lock:
            self._current_rate = rate
            self._current_free = free

        config_state.append_disk_history(self.data_dir, rate, self.app_config.get_disk().graph_scale)
        return rate

    def current_rate(self) -> float:
        """Return the most recently sampled instantaneous write rate, in bytes/sec."""
        with self._lock:
            return self._current_rate

    def current_free(self) -> int:
        """Return the most recently sampled total free space across monitored drives, in bytes."""
        with self._lock:
            return self._current_free

    def history(self) -> List[float]:
        """Return the persisted rolling history of write-rate samples."""
        return config_state.get_disk_history(self.data_dir)

    def run_loop(self, stop_event: threading.Event) -> None:
        """Run sample_once repeatedly, sleeping sample_interval_sec between passes."""
        while not stop_event.is_set():
            try:
                self.sample_once()
            except Exception:
                log.exception("Disk sampling failed")
            stop_event.wait(self.app_config.get_disk().sample_interval_sec)
