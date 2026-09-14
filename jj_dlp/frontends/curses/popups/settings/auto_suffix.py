"""Auto-Suffix override popup (Doc 1 §6.1, §16.1)."""

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


def open_auto_suffix_popup(
    stdscr,
    data_dir: Path,
    site: str,
    streamer: str,
    site_config: dict,
    color_fn: Optional[ColorFn] = None,
) -> str:
    """Run the auto-suffix override popup for one streamer; returns the ACTION_* result."""
    override = priority_config.get_override(data_dir, site, streamer, "auto_suffix")
    site_auto_suffix = site_config.get("output", {}).get("auto_suffix", True)

    fields = [
        PopupField(
            key="auto_suffix",
            label="Auto-Suffix",
            field_type="tri_bool",
            current_override=override,
            effective_value=site_auto_suffix,
        ),
    ]

    def do_confirm_reset() -> bool:
        """Ask for confirmation before clearing this streamer's auto-suffix override."""
        return confirm_reset(stdscr, "auto_suffix", streamer, color_fn)

    popup = FieldEditPopup(
        "Auto-Suffix Override",
        fields,
        color_fn=color_fn,
        confirm_reset_fn=do_confirm_reset,
    )
    action = run_popup(stdscr, popup)

    if action == ACTION_SAVE and popup.is_modified():
        priority_config.set_override(data_dir, site, streamer, "auto_suffix", popup.values()["auto_suffix"])
    elif action == ACTION_RESET:
        priority_config.clear_override(data_dir, site, streamer, "auto_suffix")

    return action
