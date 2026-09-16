"""SitePlugin implementation for the twitch plugin."""

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from jj_dlp.core.eventsub import backfill, subscriptions, webhook
from jj_dlp.core.plugins import register_plugin
from jj_dlp.core.plugins.base import FieldDef, SitePlugin


@dataclass
class _RunningService:
    """One twitch site's live background-service handles, for later shutdown."""

    webhook_server: Optional[Any]
    backfill_stop: threading.Event
    backfill_thread: threading.Thread


@register_plugin
class TwitchPlugin(SitePlugin):
    """Site plugin for twitch.tv, per Doc 1 §4.2."""

    id = "twitch"
    display_name = "Twitch"

    def __init__(self) -> None:
        self._services: Dict[str, _RunningService] = {}

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

    def start_background_service(self, app_state: Any, site: Any) -> None:
        """Reconcile subscriptions, then start this site's webhook server and backfill polling."""
        plugin_settings = site.config.get("plugin_settings", {})
        disabled = set(site.config.get("disabled", []))
        streamers = [s for s in site.config.get("streamers", []) if s not in disabled]

        subscriptions.reconcile(app_state.data_dir, site.label, plugin_settings, streamers)

        checker = app_state.get_checker(site.label)
        if checker is not None:
            on_online = webhook.dispatch_to_checker(checker)
            backfill_on_live = backfill.dispatch_to_checker(checker)
        else:
            # No Checker registered yet for this site (engine wiring not started) —
            # fall back to the same default a fresh Checker would use.
            on_online = site.site_state.mark_live
            backfill_on_live = site.site_state.mark_live
        on_offline = webhook.dispatch_offline_to_site_state(site.site_state)

        webhook_server = None
        eventsub_cfg = plugin_settings.get("eventsub", {})
        client_secret = eventsub_cfg.get("client_secret", "")
        if client_secret:
            secret = subscriptions.derive_secret(client_secret, site.label)
            webhook_server = webhook.EventSubWebhookServer(
                data_dir=app_state.data_dir,
                site=site.label,
                port=eventsub_cfg.get("webhook_port", 8888),
                secret=secret,
                on_online=on_online,
                on_offline=on_offline,
            )
            webhook_server.start()

        backfill_runner = backfill.BackfillRunner(
            site=site.label,
            plugin_settings=plugin_settings,
            site_config=site.config,
            site_state=site.site_state,
            on_live=backfill_on_live,
        )
        backfill_stop = threading.Event()
        backfill_thread = threading.Thread(target=backfill_runner.run_loop, args=(backfill_stop,), daemon=True)
        backfill_thread.start()

        self._services[site.label] = _RunningService(
            webhook_server=webhook_server, backfill_stop=backfill_stop, backfill_thread=backfill_thread
        )

    def stop_background_service(self, site: Any) -> None:
        """Stop this site's running webhook server and backfill polling thread, if any."""
        service = self._services.pop(site.label, None)
        if service is None:
            return
        service.backfill_stop.set()
        service.backfill_thread.join(timeout=5)
        if service.webhook_server is not None:
            service.webhook_server.stop()
