"""Background check against GitHub Releases for available updates (Doc 1 §9.1)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from jj_dlp.core.config import app as app_config
from jj_dlp.core.config import state
from jj_dlp.core.notify import logger

# owner/repo slug queried on the GitHub Releases API; update for the real repo.
GITHUB_REPO = "jjs-cloud/jj-dlp"
RELEASES_API_URL = "https://api.github.com/repos/{repo}/releases"
REQUEST_TIMEOUT_SEC = 10
# how often the background loop wakes to check whether a real check is due
LOOP_POLL_SEC = 60


def _fetch_releases(repo: str) -> List[Dict[str, Any]]:
    """Fetch the repo's releases list from the GitHub API."""
    url = RELEASES_API_URL.format(repo=repo)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _latest_release_for_branch(releases: List[Dict[str, Any]], branch: str) -> Optional[Dict[str, Any]]:
    """Return the most recently published non-draft release built from branch."""
    candidates = [
        r for r in releases
        if not r.get("draft") and r.get("target_commitish") == branch
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    return candidates[0]


def is_update_available(data_dir: Path, config: Optional[app_config.AppConfig] = None) -> bool:
    """Check GitHub Releases now; record latest_sha_seen and return whether it differs from installed_sha."""
    data_dir = Path(data_dir)
    cfg = config or app_config.load(data_dir)
    branch = cfg.get_updates().update_branch
    now = time.time()

    try:
        releases = _fetch_releases(GITHUB_REPO)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.dbg(f"update check request failed: {exc}", tag="updater")
        state.set_update_check(data_dir, last_check_ts=now)
        return False

    release = _latest_release_for_branch(releases, branch)
    if release is None:
        logger.dbg(f"no releases found for branch '{branch}'", tag="updater")
        state.set_update_check(data_dir, last_check_ts=now)
        return False

    latest_sha = release.get("tag_name", "")
    installed_sha = state.get_update_check(data_dir).get("installed_sha")
    state.set_update_check(data_dir, last_check_ts=now, latest_sha_seen=latest_sha)
    return bool(latest_sha) and latest_sha != installed_sha


def maybe_check(data_dir: Path, config: Optional[app_config.AppConfig] = None) -> bool:
    """Run a real check only if enabled and the configured interval has elapsed."""
    data_dir = Path(data_dir)
    cfg = config or app_config.load(data_dir)
    updates_cfg = cfg.get_updates()
    if not updates_cfg.check_for_updates:
        return False

    last_check_ts = state.get_update_check(data_dir).get("last_check_ts")
    interval_sec = updates_cfg.update_interval_min * 60
    if last_check_ts is not None and (time.time() - last_check_ts) < interval_sec:
        return False

    return is_update_available(data_dir, cfg)


def run_forever(
    data_dir: Path,
    config: Optional[app_config.AppConfig] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Loop, calling maybe_check on each wake, until stop_event is set."""
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            maybe_check(data_dir, config)
        except Exception as exc:  # noqa: BLE001 - never let the checker thread die
            logger.dbg(f"update checker error: {exc}", tag="updater")
        stop_event.wait(LOOP_POLL_SEC)


def start_background(
    data_dir: Path,
    config: Optional[app_config.AppConfig] = None,
    stop_event: Optional[threading.Event] = None,
) -> threading.Thread:
    """Start the periodic update-check loop on a daemon thread; returns the thread."""
    stop_event = stop_event if stop_event is not None else threading.Event()
    thread = threading.Thread(
        target=run_forever, args=(data_dir, config, stop_event), daemon=True
    )
    thread.start()
    return thread
