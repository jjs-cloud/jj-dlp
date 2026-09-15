"""Schedule override popup (Doc 1 §6.1, §16.1)."""

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

DAYS_OF_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# No site-level fields back this override; empty days means no restriction.
_DEFAULT_DAYS: list = []
_DEFAULT_START = ""
_DEFAULT_END = ""


def open_schedule_popup(
    stdscr,
    data_dir: Path,
    site: str,
    streamer: str,
    site_config: dict,
    color_fn: Optional[ColorFn] = None,
) -> str:
    """Run the schedule override popup for one streamer; returns the ACTION_* result."""
    override = priority_config.get_override(data_dir, site, streamer, "schedule") or {}

    fields = [
        PopupField(
            key="days",
            label="Days",
            field_type="list_str",
            current_override=override.get("days"),
            effective_value=_DEFAULT_DAYS,
            options=DAYS_OF_WEEK,
        ),
        PopupField(
            key="start",
            label="Start (HH:MM)",
            field_type="str",
            current_override=override.get("start"),
            effective_value=_DEFAULT_START,
        ),
        PopupField(
            key="end",
            label="End (HH:MM)",
            field_type="str",
            current_override=override.get("end"),
            effective_value=_DEFAULT_END,
        ),
    ]

    def do_confirm_reset() -> bool:
        """Ask for confirmation before clearing this streamer's schedule override."""
        return confirm_reset(stdscr, "schedule", streamer, color_fn)

    popup = FieldEditPopup(
        "Schedule Override",
        fields,
        color_fn=color_fn,
        confirm_reset_fn=do_confirm_reset,
    )
    action = run_popup(stdscr, popup)

    if action == ACTION_SAVE and popup.is_modified():
        priority_config.set_override(data_dir, site, streamer, "schedule", popup.values())
    elif action == ACTION_RESET:
        priority_config.clear_override(data_dir, site, streamer, "schedule")

    return action
