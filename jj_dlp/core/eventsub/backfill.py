"""Helix streams backfill for missed webhook events (Doc 1 §21)."""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Set

from jj_dlp.core.engine.checker import Checker
from jj_dlp.core.engine.site_state import SiteState
from jj_dlp.core.eventsub import token

log = logging.getLogger("jj_dlp.eventsub.backfill")

STREAMS_URL = "https://api.twitch.tv/helix/streams"
REQUEST_TIMEOUT_SEC = 10
MAX_LOGINS_PER_REQUEST = 100
DEFAULT_INTERVAL_SEC = 300.0

# Called with a streamer login found live that a webhook may have missed.
OnLiveCallback = Callable[[str], None]


def _headers(client_id: str, access_token: str) -> Dict[str, str]:
    """Build standard Helix API auth headers."""
    return {"Client-Id": client_id, "Authorization": f"Bearer {access_token}"}


def _chunks(items: List[str], size: int) -> List[List[str]]:
    """Split a list into size-limited chunks."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def get_live_logins(client_id: str, access_token: str, logins: List[str]) -> Set[str]:
    """Return the subset of logins Helix currently reports as live."""
    live: Set[str] = set()
    for batch in _chunks(logins, MAX_LOGINS_PER_REQUEST):
        if not batch:
            continue
        params = urllib.parse.urlencode({"user_login": batch}, doseq=True)
        req = urllib.request.Request(f"{STREAMS_URL}?{params}", headers=_headers(client_id, access_token))
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("eventsub backfill: streams request failed: %s", exc)
            continue
        for stream in payload.get("data") or []:
            login = stream.get("user_login")
            if login:
                live.add(login.lower())
    return live


def _eventsub_creds(plugin_settings: dict) -> Dict[str, str]:
    """Pull client_id/client_secret out of a twitch site's plugin_settings."""
    cfg = plugin_settings.get("eventsub", {})
    return {"client_id": cfg.get("client_id", ""), "client_secret": cfg.get("client_secret", "")}


def run_once(
    site: str,
    plugin_settings: dict,
    streamers: List[str],
    site_state: SiteState,
    on_live: OnLiveCallback,
) -> None:
    """Catch any streamer already live that a webhook subscription may have missed."""
    creds = _eventsub_creds(plugin_settings)
    client_id, client_secret = creds["client_id"], creds["client_secret"]
    if not client_id or not client_secret:
        return

    try:
        access_token = token.get_token(client_id, client_secret)
    except token.TokenError as exc:
        log.warning("eventsub backfill aborted for %s: %s", site, exc)
        return

    candidates = [s for s in streamers if not site_state.is_recording(s)]
    if not candidates:
        return

    live_logins = get_live_logins(client_id, access_token, candidates)
    for streamer in candidates:
        if streamer.lower() in live_logins:
            on_live(streamer)


class BackfillRunner:
    """Runs the backfill check on startup and on a fixed interval until stopped."""

    def __init__(
        self,
        site: str,
        plugin_settings: dict,
        site_config: dict,
        site_state: SiteState,
        on_live: OnLiveCallback,
        interval_sec: float = DEFAULT_INTERVAL_SEC,
    ) -> None:
        self.site = site
        self.plugin_settings = plugin_settings
        self.site_config = site_config
        self.site_state = site_state
        self.on_live = on_live
        self.interval_sec = interval_sec

    def _streamers(self) -> List[str]:
        """Return the site's currently enabled (non-disabled) streamer list."""
        disabled = set(self.site_config.get("disabled", []))
        return [s for s in self.site_config.get("streamers", []) if s not in disabled]

    def run_loop(self, stop_event: threading.Event) -> None:
        """Run the backfill check immediately, then repeatedly every interval_sec, until stopped."""
        while not stop_event.is_set():
            run_once(self.site, self.plugin_settings, self._streamers(), self.site_state, self.on_live)
            stop_event.wait(self.interval_sec)


def dispatch_to_checker(checker: Checker) -> OnLiveCallback:
    """Return an on_live callback feeding straight into a Checker's live-detection path."""
    return lambda streamer: checker.on_live(streamer)


def build_backfill_runner(
    site: str,
    plugin_settings: dict,
    site_config: dict,
    checker: Checker,
    site_state: SiteState,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
) -> Optional[BackfillRunner]:
    """Construct a BackfillRunner for a twitch site from its plugin_settings, or None if unconfigured."""
    creds = _eventsub_creds(plugin_settings)
    if not creds["client_id"] or not creds["client_secret"]:
        log.warning("eventsub backfill not started for %s: missing client_id/client_secret", site)
        return None
    return BackfillRunner(
        site=site,
        plugin_settings=plugin_settings,
        site_config=site_config,
        site_state=site_state,
        on_live=dispatch_to_checker(checker),
        interval_sec=interval_sec,
    )
