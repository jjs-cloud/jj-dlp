"""Split override popup (Doc 1 §6.1, §16.1)."""

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


def open_split_popup(
    stdscr,
    data_dir: Path,
    site: str,
    streamer: str,
    site_config: dict,
    color_fn: Optional[ColorFn] = None,
) -> str:
    """Run the split override popup for one streamer; returns the ACTION_* result."""
    override = priority_config.get_override(data_dir, site, streamer, "split") or {}
    site_split_minutes = site_config.get("timing", {}).get("split_after_minutes", 0)

    fields = [
        PopupField(
            key="enabled",
            label="Split Enabled",
            field_type="tri_bool",
            current_override=override.get("enabled"),
            effective_value=site_split_minutes != 0,
        ),
        PopupField(
            key="split_after_minutes",
            label="Split After (min)",
            field_type="int",
            current_override=override.get("split_after_minutes"),
            effective_value=site_split_minutes,
        ),
    ]

    def do_confirm_reset() -> bool:
        """Ask for confirmation before clearing this streamer's split override."""
        return confirm_reset(stdscr, "split", streamer, color_fn)

    popup = FieldEditPopup(
        "Split Override",
        fields,
        color_fn=color_fn,
        confirm_reset_fn=do_confirm_reset,
    )
    action = run_popup(stdscr, popup)

    if action == ACTION_SAVE and popup.is_modified():
        priority_config.set_override(data_dir, site, streamer, "split", popup.values())
    elif action == ACTION_RESET:
        priority_config.clear_override(data_dir, site, streamer, "split")

    return action
