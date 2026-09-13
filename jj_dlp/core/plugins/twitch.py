"""SitePlugin implementation for the twitch plugin."""

from typing import Any

from jj_dlp.core.plugins import register_plugin
from jj_dlp.core.plugins.base import FieldDef, SitePlugin


@register_plugin
class TwitchPlugin(SitePlugin):
    """Site plugin for twitch.tv, per Doc 1 §4.2."""

    id = "twitch"
    display_name = "Twitch"

    def default_config(self) -> dict:
        """Return the §3.4 base shape with twitch's overrides applied."""
        return {
            "schema_version": 1,
            "label": "",
            "plugin": "twitch",
            "order": 0,
            "url_template": "https://twitch.tv/{username}",
            "streamers": [],
            "disabled": [],
            "blocked": [],
            "timing": {
                "check_interval": 60,
                "cooldown_after_recording": 60,
                "stall_check_interval": 30,
                "stall_timeout": 120,
                "split_after_minutes": 0,
            },
            "output": {
                "dir": "recordings",
                "template": "%(title)s [%(id)s].%(ext)s",
                "auto_suffix": True,
                "panel_resize": True,
            },
            "checker": {"cookies_from_browser": True},
            "downloader": {
                "verbose": True,
                "cookies_from_browser": True,
                "fixup": "never",
                "no_part": True,
                "format": "best",
                "retries": 10,
                "downloader_args": "",
                "extra_args": "",
            },
            "lq_downloader": {
                "format": "720p60",
                "cookies_from_browser": True,
                "fixup": "never",
                "no_part": True,
            },
            "notifications": {
                "popup_enabled": True,
                "popup_timeout_sec": 15,
                "popup_cooldown_sec": 30,
                "ntfy_enabled": True,
            },
            "display": {
                "last_live_highlight_days": 1,
                "progress_bar_max_hours": 10,
                "progress_bar_width": 58,
            },
            "browser": "firefox",
            "upgrade_quality": True,
            "plugin_settings": {
                "ad_alerts_enabled": True,
                "ad_alert_patterns": [
                    "EXT-X-DISCONTINUITY",
                    "amazon",
                    "twitch-ad",
                    "/ad/",
                    "admanifest",
                    "/ads/",
                    "EXT-X-TWITCH-AD",
                    "twitch-stitched-ad",
                ],
                "eventsub": {
                    "client_id": "",
                    "client_secret": "",
                    "callback_url": "",
                    "webhook_port": 8888,
                },
            },
        }

    def plugin_settings_schema(self):
        """Return twitch's ad-alert and EventSub field definitions."""
        return [
            FieldDef(
                path="plugin_settings.ad_alerts_enabled",
                type="bool",
                default=True,
                preserve=True,
                help="Scan downloader output for ad-marker patterns.",
            ),
            FieldDef(
                path="plugin_settings.ad_alert_patterns",
                type="list[str]",
                default=[],
                preserve=True,
                help="Case-insensitive substrings checked against downloader output to flag ad segments.",
            ),
            FieldDef(
                path="plugin_settings.eventsub.client_id",
                type="str",
                default="",
                preserve=True,
                help="Twitch application client ID for EventSub/Helix API access.",
            ),
            FieldDef(
                path="plugin_settings.eventsub.client_secret",
                type="str",
                default="",
                preserve=True,
                help="Twitch application client secret for EventSub/Helix API access.",
            ),
            FieldDef(
                path="plugin_settings.eventsub.callback_url",
                type="str",
                default="",
                preserve=True,
                help="Publicly reachable URL Twitch sends EventSub webhook notifications to.",
            ),
            FieldDef(
                path="plugin_settings.eventsub.webhook_port",
                type="int",
                default=8888,
                preserve=True,
                help="Local port the EventSub webhook server listens on.",
            ),
        ]

    def on_downloader_line(self, site: Any, streamer: str, line: str) -> None:
        """Flag ad_alert_active for streamer on a matching ad-pattern substring."""
        settings = site.config.get("plugin_settings", {})
        if not settings.get("ad_alerts_enabled", True):
            return
        patterns = settings.get("ad_alert_patterns", [])
        lowered = line.lower()
        if any(pattern.lower() in lowered for pattern in patterns):
            site.site_state.set_ad_alert_active(streamer, True)

    # start_background_service / stop_background_service: implemented in
    # Phase 16 (Twitch EventSub subsystem).
