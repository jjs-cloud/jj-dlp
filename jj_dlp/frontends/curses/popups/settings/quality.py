"""Quality override popup (Doc 1 §6.1, §16.1)."""

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

# No site-level field backs this; absent override means fallback is allowed.
_DEFAULT_LQ_FALLBACK_DISABLED = False


def open_quality_popup(
    stdscr,
    data_dir: Path,
    site: str,
    streamer: str,
    site_config: dict,
    color_fn: Optional[ColorFn] = None,
) -> str:
    """Run the quality override popup for one streamer; returns the ACTION_* result."""
    override = priority_config.get_override(data_dir, site, streamer, "quality") or {}
    site_format = site_config.get("downloader", {}).get("format", "best")

    fields = [
        PopupField(
            key="format",
            label="Format",
            field_type="str",
            current_override=override.get("format"),
            effective_value=site_format,
        ),
        PopupField(
            key="lq_fallback_disabled",
            label="Disable LQ Fallback",
            field_type="bool",
            current_override=override.get("lq_fallback_disabled"),
            effective_value=_DEFAULT_LQ_FALLBACK_DISABLED,
        ),
    ]

    def do_confirm_reset() -> bool:
        """Ask for confirmation before clearing this streamer's quality override."""
        return confirm_reset(stdscr, "quality", streamer, color_fn)

    popup = FieldEditPopup(
        "Quality Override",
        fields,
        color_fn=color_fn,
        confirm_reset_fn=do_confirm_reset,
    )
    action = run_popup(stdscr, popup)

    if action == ACTION_SAVE and popup.is_modified():
        priority_config.set_override(data_dir, site, streamer, "quality", popup.values())
    elif action == ACTION_RESET:
        priority_config.clear_override(data_dir, site, streamer, "quality")

    return action
