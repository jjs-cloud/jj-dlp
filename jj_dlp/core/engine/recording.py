"""Start/stop recording lifecycle, cooldown, and output path resolution."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from jj_dlp.core.config.app import AppConfig
from jj_dlp.core.engine import downloader
from jj_dlp.core.engine.site_state import SiteState

log = logging.getLogger("jj_dlp.engine.recording")

_EXT_MARKER = ".%(ext)s"


def resolve_output_dir(subfolders_mode: str, base_dir: str, site_label: str, streamer: str) -> str:
    """Compute a streamer's output directory per app.json's output.subfolders grouping mode."""
    parts = [base_dir]
    if subfolders_mode == "site-only":
        parts.append(site_label)
    elif subfolders_mode == "streamer-only":
        parts.append(streamer)
    elif subfolders_mode == "site-and-streamer":
        parts += [site_label, streamer]
    return str(Path(*parts))


def _with_suffix(template: str, suffix: str) -> str:
    """Insert a literal suffix into an output template, just before its extension placeholder."""
    if template.endswith(_EXT_MARKER):
        return template[: -len(_EXT_MARKER)] + suffix + _EXT_MARKER
    return template + suffix


class RecordingCoordinator:
    """Coordinates recording start/restart/stop for every streamer across this run."""

    def __init__(self, app_config: AppConfig, schema_path: Optional[Path] = None) -> None:
        self.app_config = app_config
        self.schema_path = schema_path
        self._lock = threading.Lock()
        self._last_end_time: Dict[Tuple[str, str], float] = {}
        self._session_count: Dict[Tuple[str, str], int] = {}

    def _cooldown_wait(self, site_label: str, streamer: str, cooldown_sec: float) -> None:
        """Sleep out any remaining cooldown_after_recording since this streamer last stopped."""
        with self._lock:
            last_end = self._last_end_time.get((site_label, streamer))
        if last_end is None or cooldown_sec <= 0:
            return
        remaining = (last_end + cooldown_sec) - time.time()
        if remaining > 0:
            time.sleep(remaining)

    def resolve_output_path(self, site_label: str, site_config: dict, streamer: str) -> str:
        """Resolve a new session's output template path, applying subfolder grouping and auto_suffix.

        auto_suffix is tracked as a per-streamer session counter for this run: the first
        live session of the run uses the plain template, later ones get a literal _2, _3, ...
        inserted before the extension so a same-titled later session can't overwrite the first.
        """
        output = site_config.get("output", {})
        out_dir = resolve_output_dir(
            self.app_config.output.subfolders, output.get("dir", "recordings"), site_label, streamer
        )
        template = output.get("template", "%(title)s.%(ext)s")
        if output.get("auto_suffix", True):
            with self._lock:
                key = (site_label, streamer)
                n = self._session_count.get(key, 0) + 1
                self._session_count[key] = n
            if n > 1:
                template = _with_suffix(template, f"_{n}")
        return str(Path(out_dir) / template)

    def start_session(
        self,
        site_label: str,
        site_config: dict,
        streamer: str,
        site_state: SiteState,
        lq: bool = False,
        on_line: Optional[downloader.LineCallback] = None,
    ) -> downloader.DownloaderProcess:
        """Start a brand-new recording session for a streamer checker.py just reported live."""
        cooldown = site_config.get("timing", {}).get("cooldown_after_recording", 0)
        self._cooldown_wait(site_label, streamer, cooldown)
        output_path = self.resolve_output_path(site_label, site_config, streamer)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        return self._launch(site_config, streamer, output_path, site_state, lq, on_line)

    def restart_session(
        self,
        site_config: dict,
        streamer: str,
        site_state: SiteState,
        output_path: str,
        lq: bool = False,
        on_line: Optional[downloader.LineCallback] = None,
    ) -> downloader.DownloaderProcess:
        """Restart a recording at an already-resolved output path (stall/quality-switch restarts)."""
        return self._launch(site_config, streamer, output_path, site_state, lq, on_line)

    def _launch(
        self,
        site_config: dict,
        streamer: str,
        output_path: str,
        site_state: SiteState,
        lq: bool,
        on_line: Optional[downloader.LineCallback],
    ) -> downloader.DownloaderProcess:
        """Launch the downloader process and register it with site_state."""
        process = downloader.start_recording(
            site_config, streamer, output_path, lq=lq, schema_path=self.schema_path, on_line=on_line
        )
        site_state.set_process(streamer, process)
        site_state.set_in_progress_path(streamer, output_path)
        return process

    def on_process_exit(self, site_label: str, streamer: str, site_state: SiteState) -> None:
        """Mark a streamer not-recording and record its end time, once its process has exited."""
        with self._lock:
            self._last_end_time[(site_label, streamer)] = time.time()
        site_state.clear_process(streamer)
        site_state.set_in_progress_path(streamer, None)

    def watch(
        self,
        site_label: str,
        streamer: str,
        site_state: SiteState,
        process: downloader.DownloaderProcess,
        poll_interval: float = 1.0,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """Block until process exits (or stop_event fires), then finalize via on_process_exit."""
        while process.poll() is None:
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(poll_interval)
        self.on_process_exit(site_label, streamer, site_state)

    def make_live_handler(
        self,
        site_label: str,
        site_config: dict,
        site_state: SiteState,
        on_line: Optional[downloader.LineCallback] = None,
    ):
        """Build a checker.py on_live callback that starts a session and watches it to exit."""

        def _on_live(streamer: str) -> None:
            process = self.start_session(site_label, site_config, streamer, site_state, on_line=on_line)
            threading.Thread(
                target=self.watch, args=(site_label, streamer, site_state, process), daemon=True
            ).start()

        return _on_live
