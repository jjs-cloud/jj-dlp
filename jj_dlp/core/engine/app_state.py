"""Process-wide runtime state, PID tracking, and single-instance lock."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jj_dlp.core.engine.site_state import SiteState

log = logging.getLogger("jj_dlp.engine.app_state")

LOCK_RELPATH = Path("state") / ".lock"


class SingleInstanceError(RuntimeError):
    """Raised when another instance already holds the data directory's lock."""


def _pid_is_alive(pid: int) -> bool:
    """Return whether a process with the given PID currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class AppState:
    """Process-wide runtime state: loaded sites, recording lock, and PID registry."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.loaded_sites: List[str] = []
        self.recording_decision_lock = threading.Lock()
        self._site_states: Dict[str, SiteState] = {}
        self._checkers: Dict[str, Any] = {}
        self._active_services: Dict[str, Tuple[Any, Any]] = {}
        self._lock_path = self.data_dir / LOCK_RELPATH
        self._holds_lock = False

    def set_loaded_sites(self, labels: List[str]) -> None:
        """Replace the list of site labels loaded for this run."""
        self.loaded_sites = list(labels)

    def register_site_state(self, label: str, site_state: SiteState) -> None:
        """Register a loaded site's runtime SiteState so other modules can look it up by label."""
        self._site_states[label] = site_state

    def unregister_site_state(self, label: str) -> None:
        """Drop a site's runtime SiteState, e.g. on site removal or shutdown."""
        self._site_states.pop(label, None)

    def get_site_state(self, label: str) -> Optional[SiteState]:
        """Return a loaded site's registered SiteState, or None if not registered."""
        return self._site_states.get(label)

    def site_states(self) -> Dict[str, SiteState]:
        """Return a shallow copy of the label -> SiteState registry."""
        return dict(self._site_states)

    def register_checker(self, label: str, checker: Any) -> None:
        """Register a loaded site's Checker so background services can hand off live events to it."""
        self._checkers[label] = checker

    def unregister_checker(self, label: str) -> None:
        """Drop a site's registered Checker, e.g. on site removal or shutdown."""
        self._checkers.pop(label, None)

    def get_checker(self, label: str) -> Optional[Any]:
        """Return a loaded site's registered Checker, or None if not registered."""
        return self._checkers.get(label)

    def start_site_services(self, site: Any, plugin: Any) -> None:
        """Call a site's plugin start_background_service hook, if defined, and track it for shutdown."""
        hook = getattr(plugin, "start_background_service", None)
        if hook is None:
            return
        hook(self, site)
        self._active_services[site.label] = (plugin, site)

    def stop_site_services(self, label: str) -> None:
        """Call a previously started site's plugin stop_background_service hook, if any is running."""
        entry = self._active_services.pop(label, None)
        if entry is None:
            return
        plugin, site = entry
        hook = getattr(plugin, "stop_background_service", None)
        if hook is not None:
            hook(site)

    def stop_all_site_services(self) -> None:
        """Stop every currently running site background service, e.g. during shutdown."""
        for label in list(self._active_services):
            self.stop_site_services(label)

    def emergency_kill_all(self) -> None:
        """Force-terminate every registered site's active processes, for shutdown/crash paths."""
        for label, site_state in self.site_states().items():
            for streamer, process in site_state.active_processes().items():
                try:
                    process.kill()
                except OSError as exc:
                    log.warning("Failed to kill %s/%s pid %s: %s", label, streamer, process.pid, exc)

    def _read_lock_pid(self) -> Optional[int]:
        """Read the PID recorded in the lock file, or None if unreadable."""
        try:
            return int(self._lock_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def acquire_single_instance_lock(self) -> None:
        """Claim the data directory's instance lock, raising if another live instance holds it."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self._lock_path.exists():
            existing_pid = self._read_lock_pid()
            if existing_pid is not None and _pid_is_alive(existing_pid):
                raise SingleInstanceError(
                    f"jj-dlp is already running (pid {existing_pid}) against this data directory."
                )
            log.warning("Stale lock file found (pid %s not running); taking over.", existing_pid)

        fd = os.open(self._lock_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        self._holds_lock = True

    def release_single_instance_lock(self) -> None:
        """Release the instance lock by removing the lock file, if this process holds it."""
        if not self._holds_lock:
            return
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Failed to remove lock file %s: %s", self._lock_path, exc)
        self._holds_lock = False
