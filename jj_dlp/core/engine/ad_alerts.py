"""Dispatches downloader output lines to the active site's plugin ad-scanning hook."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jj_dlp.core.engine.site_state import SiteState

log = logging.getLogger("jj_dlp.engine.ad_alerts")


@dataclass(frozen=True)
class SiteHandle:
    """Everything a plugin's on_downloader_line hook needs about the site being scanned.

    This is the "site" argument in the SitePlugin.on_downloader_line(site, streamer, line)
    interface (Doc 1 §4) — engine internals otherwise keep site_config/site_state separate,
    but the plugin hook boundary bundles them since the interface takes a single "site" arg.
    """

    label: str
    config: dict
    site_state: SiteState


class AdAlertDispatcher:
    """Per-site dispatcher forwarding downloader lines to the site's plugin, if it scans them."""

    def __init__(self, site: SiteHandle, plugin: Any) -> None:
        self.site = site
        self.plugin = plugin

    def on_downloader_line(self, streamer: str, line: str) -> None:
        """Call the site's plugin on_downloader_line hook, if it defines one."""
        hook = getattr(self.plugin, "on_downloader_line", None)
        if hook is None:
            return
        try:
            hook(self.site, streamer, line)
        except Exception:
            log.exception(
                "plugin on_downloader_line hook raised for %s/%s", self.site.label, streamer
            )
