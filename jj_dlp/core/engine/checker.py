"""Liveness polling via yt-dlp metadata-only mode."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from jj_dlp.core.engine.site_state import SiteState

log = logging.getLogger("jj_dlp.engine.checker")

DEFAULT_CHECK_TIMEOUT_SEC = 30.0
_OFFLINE_MARKERS = ("offline", "unavailable", "no video formats", "not currently live")


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one liveness check for a streamer."""

    live: bool
    error: Optional[str] = None


def build_check_command(site_config: dict, streamer: str) -> List[str]:
    """Build the yt-dlp metadata-only check command line for one streamer."""
    url = site_config["url_template"].format(username=streamer)
    cmd = ["yt-dlp", "--dump-json", "--skip-download", "--no-warnings", url]
    if site_config.get("checker", {}).get("cookies_from_browser"):
        browser = site_config.get("browser", "")
        if browser:
            cmd[1:1] = ["--cookies-from-browser", browser]
    return cmd


def _looks_offline(text: str) -> bool:
    """Return whether check output text matches a normal offline/unavailable response."""
    lowered = text.lower()
    return any(marker in lowered for marker in _OFFLINE_MARKERS)


def check_streamer(
    site_config: dict, streamer: str, timeout: float = DEFAULT_CHECK_TIMEOUT_SEC
) -> CheckResult:
    """Run one yt-dlp metadata-only check for a streamer and classify the result."""
    cmd = build_check_command(site_config, streamer)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(live=False, error="check timed out")
    except OSError as exc:
        return CheckResult(live=False, error=str(exc))

    if proc.returncode == 0:
        first_line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
        if not first_line:
            return CheckResult(live=False, error="empty output on success exit")
        try:
            json.loads(first_line)
        except ValueError:
            return CheckResult(live=False, error="could not parse yt-dlp output as JSON")
        return CheckResult(live=True)

    combined = f"{proc.stdout}\n{proc.stderr}"
    if _looks_offline(combined):
        return CheckResult(live=False)
    return CheckResult(live=False, error=combined.strip()[:500])


class Checker:
    """Polls every non-recording, non-disabled streamer on one site on its own schedule."""

    def __init__(
        self,
        site_config: dict,
        site_state: SiteState,
        on_live: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.site_config = site_config
        self.site_state = site_state
        self.on_live = on_live or site_state.mark_live
        self._last_checked: Dict[str, float] = {}

    def _due(self, streamer: str, now: float) -> bool:
        """Return whether a streamer's check_interval has elapsed since its last check."""
        interval = self.site_config["timing"]["check_interval"]
        return now - self._last_checked.get(streamer, 0.0) >= interval

    def poll_once(self, now: Optional[float] = None) -> None:
        """Check every due, eligible streamer once and update site_state on live results."""
        now = time.time() if now is None else now
        disabled = set(self.site_config.get("disabled", []))
        for streamer in self.site_config.get("streamers", []):
            if streamer in disabled or self.site_state.is_recording(streamer):
                continue
            if not self._due(streamer, now):
                continue
            self._last_checked[streamer] = now
            result = check_streamer(self.site_config, streamer)
            if result.live:
                self.on_live(streamer)
            elif result.error:
                # Nonzero exit classified as "offline" carries no error and is not logged.
                log.warning("Liveness check error for %s: %s", streamer, result.error)

    def run_loop(self, stop_event: threading.Event, tick_sec: float = 1.0) -> None:
        """Run poll_once repeatedly, sleeping tick_sec between passes, until stop_event is set."""
        while not stop_event.is_set():
            self.poll_once()
            stop_event.wait(tick_sec)
