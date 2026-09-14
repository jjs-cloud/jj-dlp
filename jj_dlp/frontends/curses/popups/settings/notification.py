"""Notification override popup (Doc 1 §6.1, §16.1)."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, Optional, Tuple

from jj_dlp.core.config import priority as priority_config
from jj_dlp.frontends.curses.popups.confirm_reset import confirm_reset
from jj_dlp.frontends.curses.popups.framework import (
    ACTION_SAVE,
    ACTION_RESET,
    FieldEditPopup,
    PopupField,
    run_popup,
)

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def open_notification_popup(
    stdscr,
    data_dir: Path,
    site: str,
    streamer: str,
    site_config: dict,
    color_fn: Optional[ColorFn] = None,
) -> str:
    """Run the notification override popup for one streamer; returns the ACTION_* result."""
    override = priority_config.get_override(data_dir, site, streamer, "notifications") or {}
    site_notifications = site_config.get("notifications", {})

    fields = [
        PopupField(
            key="popup_enabled",
            label="Desktop Popup",
            field_type="tri_bool",
            current_override=override.get("popup_enabled"),
            effective_value=site_notifications.get("popup_enabled", True),
        ),
        PopupField(
            key="ntfy_enabled",
            label="ntfy Push",
            field_type="tri_bool",
            current_override=override.get("ntfy_enabled"),
            effective_value=site_notifications.get("ntfy_enabled", False),
        ),
    ]

    def do_confirm_reset() -> bool:
        """Ask for confirmation before clearing this streamer's notification override."""
        return confirm_reset(stdscr, "notifications", streamer, color_fn)

    popup = FieldEditPopup(
        "Notification Override",
        fields,
        color_fn=color_fn,
        confirm_reset_fn=do_confirm_reset,
    )
    action = run_popup(stdscr, popup)

    if action == ACTION_SAVE and popup.is_modified():
        priority_config.set_override(data_dir, site, streamer, "notifications", popup.values())
    elif action == ACTION_RESET:
        priority_config.clear_override(data_dir, site, streamer, "notifications")

    return action
