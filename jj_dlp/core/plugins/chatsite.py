"""SitePlugin implementation for the chatsite plugin."""

from jj_dlp.core.plugins import register_plugin
from jj_dlp.core.plugins.base import SitePlugin


@register_plugin
class ChatsitePlugin(SitePlugin):
    """Site plugin for chatsite.com, per Doc 1 §4.1."""

    id = "chatsite"
    display_name = "Chatsite"

    def default_config(self) -> dict:
        """Return the §3.4 base shape with chatsite's overrides applied."""
        return {
            "schema_version": 1,
            "label": "",
            "plugin": "chatsite",
            "order": 0,
            "url_template": "https://chatsite.com/{username}",
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
                "template": "%(title)s.%(ext)s",
                "auto_suffix": True,
                "panel_resize": True,
            },
            "checker": {"cookies_from_browser": False},
            "downloader": {
                "verbose": False,
                "cookies_from_browser": False,
                "fixup": "never",
                "no_part": True,
                "format": "best",
                "retries": 10,
                "downloader_args": (
                    'ffmpeg:"-fps_mode passthrough -copyts -avoid_negative_ts make_zero"'
                ),
                "extra_args": "",
            },
            "lq_downloader": {
                "format": "4",
                "cookies_from_browser": False,
                "fixup": "never",
                "no_part": True,
            },
            "notifications": {
                "popup_enabled": True,
                "popup_timeout_sec": 15,
                "popup_cooldown_sec": 240,
                "ntfy_enabled": True,
            },
            "display": {
                "last_live_highlight_days": 1,
                "progress_bar_max_hours": 10,
                "progress_bar_width": 58,
            },
            "browser": "",
            "upgrade_quality": True,
            "plugin_settings": {},
        }
