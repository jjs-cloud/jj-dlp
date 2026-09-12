"""SitePlugin implementation for the tiktok-live plugin."""

from jj_dlp.core.plugins import register_plugin
from jj_dlp.core.plugins.base import SitePlugin


@register_plugin
class TiktokLivePlugin(SitePlugin):
    """Site plugin for tiktok.com/@user/live, per Doc 1 §4.3."""

    id = "tiktok-live"
    display_name = "TikTok Live"

    def default_config(self) -> dict:
        """Return the §3.4 base shape with tiktok-live's overrides applied."""
        return {
            "schema_version": 1,
            "label": "",
            "plugin": "tiktok-live",
            "order": 0,
            "url_template": "https://tiktok.com/@{username}/live",
            "streamers": [],
            "disabled": [],
            "blocked": [],
            "timing": {
                "check_interval": 60,
                "cooldown_after_recording": 60,
                "stall_check_interval": 30,
                "stall_timeout": 360,
                "split_after_minutes": 0,
            },
            "output": {
                "dir": "recordings",
                "template": "%(uploader)s %(epoch>%Y-%m-%d %H_%M_%S)s.%(ext)s",
                "auto_suffix": True,
                "panel_resize": True,
            },
            "checker": {"cookies_from_browser": True},
            "downloader": {
                "verbose": False,
                "cookies_from_browser": True,
                "fixup": "never",
                "no_part": True,
                "format": "best",
                "retries": 0,
                "downloader_args": "",
                "extra_args": "",
            },
            "lq_downloader": {
                "format": "480p",
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
            "upgrade_quality": False,
            "plugin_settings": {},
        }
