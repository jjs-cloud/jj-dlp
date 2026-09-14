"""Output Directory override popup (Doc 1 §6.1, §16.1)."""

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


def open_output_dir_popup(
    stdscr,
    data_dir: Path,
    site: str,
    streamer: str,
    site_config: dict,
    color_fn: Optional[ColorFn] = None,
) -> str:
    """Run the output-directory override popup for one streamer; returns the ACTION_* result."""
    override = priority_config.get_override(data_dir, site, streamer, "output_dir")
    site_output_dir = site_config.get("output", {}).get("dir", "recordings")

    fields = [
        PopupField(
            key="output_dir",
            label="Output Dir",
            field_type="str",
            current_override=override,
            effective_value=site_output_dir,
        ),
    ]

    def do_confirm_reset() -> bool:
        """Ask for confirmation before clearing this streamer's output-dir override."""
        return confirm_reset(stdscr, "output_dir", streamer, color_fn)

    popup = FieldEditPopup(
        "Output Directory Override",
        fields,
        color_fn=color_fn,
        confirm_reset_fn=do_confirm_reset,
    )
    action = run_popup(stdscr, popup)

    if action == ACTION_SAVE and popup.is_modified():
        priority_config.set_override(data_dir, site, streamer, "output_dir", popup.values()["output_dir"])
    elif action == ACTION_RESET:
        priority_config.clear_override(data_dir, site, streamer, "output_dir")

    return action
