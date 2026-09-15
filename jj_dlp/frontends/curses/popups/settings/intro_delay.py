"""Intro Delay override popup (Doc 1 §6.1, §16.1)."""

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

# No site-level config field backs this override; 0 (record immediately) is the default.
DEFAULT_INTRO_DELAY_SECONDS = 0


def open_intro_delay_popup(
    stdscr,
    data_dir: Path,
    site: str,
    streamer: str,
    site_config: dict,
    color_fn: Optional[ColorFn] = None,
) -> str:
    """Run the intro-delay override popup for one streamer; returns the ACTION_* result."""
    override = priority_config.get_override(data_dir, site, streamer, "intro_delay")

    fields = [
        PopupField(
            key="intro_delay",
            label="Intro Delay (sec)",
            field_type="int",
            current_override=override,
            effective_value=DEFAULT_INTRO_DELAY_SECONDS,
        ),
    ]

    def do_confirm_reset() -> bool:
        """Ask for confirmation before clearing this streamer's intro-delay override."""
        return confirm_reset(stdscr, "intro_delay", streamer, color_fn)

    popup = FieldEditPopup(
        "Intro Delay Override",
        fields,
        color_fn=color_fn,
        confirm_reset_fn=do_confirm_reset,
    )
    action = run_popup(stdscr, popup)

    if action == ACTION_SAVE and popup.is_modified():
        priority_config.set_override(data_dir, site, streamer, "intro_delay", popup.values()["intro_delay"])
    elif action == ACTION_RESET:
        priority_config.clear_override(data_dir, site, streamer, "intro_delay")

    return action
