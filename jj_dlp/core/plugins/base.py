"""SitePlugin interface definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class FieldDef:
    """One schema/fields.json-style field definition contributed by a plugin."""

    path: str
    type: str
    default: Any = None
    help: str = ""
    preserve: bool = False
    yt_dlp_flag: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialize to the plain dict shape used in schema/fields.json."""
        d = {
            "path": self.path,
            "type": self.type,
            "default": self.default,
            "preserve": self.preserve,
            "help": self.help,
        }
        if self.yt_dlp_flag is not None:
            d["yt_dlp_flag"] = self.yt_dlp_flag
        return d


class SitePlugin(ABC):
    """Interface every site plugin (chatsite, twitch, tiktok-live, ...) implements."""

    id: str
    display_name: str

    @abstractmethod
    def default_config(self) -> dict:
        """Return the plugin's default site JSON (Doc 1 §3.4 shape)."""
        raise NotImplementedError

    def plugin_settings_schema(self) -> List[FieldDef]:
        """Return extra schema/fields.json entries this plugin contributes (may be empty)."""
        return []

    # Optional hooks below — a plugin only defines the ones it needs.
    # Callers must check with getattr(plugin, "hook_name", None) before calling,
    # since most plugins won't override them at all.
    #
    # def on_downloader_line(self, site, streamer, line: str) -> None:
    #     """Called with each line of downloader output."""
    #
    # def start_background_service(self, app_state, site) -> None:
    #     """Start a long-running per-site service (e.g. EventSub webhook)."""
    #
    # def stop_background_service(self, site) -> None:
    #     """Stop a previously started per-site background service."""
