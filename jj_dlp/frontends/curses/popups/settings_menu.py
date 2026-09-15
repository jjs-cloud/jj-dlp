"""Per-entry settings menu offering all seven override popups (Doc 1 §16)."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, Optional, Tuple

from jj_dlp.core.config import priority as priority_config
from jj_dlp.core.config import sites as sites_config
from jj_dlp.frontends.curses.popups.settings.auto_suffix import open_auto_suffix_popup
from jj_dlp.frontends.curses.popups.settings.intro_delay import open_intro_delay_popup
from jj_dlp.frontends.curses.popups.settings.notification import open_notification_popup
from jj_dlp.frontends.curses.popups.settings.output_dir import open_output_dir_popup
from jj_dlp.frontends.curses.popups.settings.quality import open_quality_popup
from jj_dlp.frontends.curses.popups.settings.schedule import open_schedule_popup
from jj_dlp.frontends.curses.popups.settings.split import open_split_popup

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

# (override field name, menu label, popup opener) for all seven override types.
MENU_ITEMS = (
    ("quality", "Quality", open_quality_popup),
    ("notifications", "Notifications", open_notification_popup),
    ("auto_suffix", "Auto-Suffix", open_auto_suffix_popup),
    ("output_dir", "Output Directory", open_output_dir_popup),
    ("split", "Split", open_split_popup),
    ("intro_delay", "Intro Delay", open_intro_delay_popup),
    ("schedule", "Schedule", open_schedule_popup),
)


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _draw_menu(stdscr, streamer: str, entry: dict, index: int, color_fn: ColorFn) -> None:
    """Render the centered list of override types, marking any already set."""
    height, width = stdscr.getmaxyx()
    overrides = entry.get("overrides", {})
    title = f"Settings \u2014 {streamer}"
    rows = []
    for field, label, _opener in MENU_ITEMS:
        marker = "*" if overrides.get(field) is not None else " "
        rows.append(f"{marker} {label}")

    box_width = max(24, min(width - 2, max(len(r) for r in rows + [title]) + 4))
    box_height = max(5, min(height - 2, len(rows) + 4))
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)

    win = curses.newwin(box_height, box_width, y1, x1)
    win.erase()
    win.attrset(color_fn("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    win.addstr(0, 2, f" {title} "[: box_width - 4], color_fn("popup.title", None))
    for i, row_text in enumerate(rows):
        row = 2 + i
        if row >= box_height - 1:
            break
        attr = color_fn("popup.button_focused", None) if i == index else curses.A_NORMAL
        win.addstr(row, 2, row_text[: box_width - 4], attr)
    win.noutrefresh()
    curses.doupdate()


def open_settings_menu(stdscr, data_dir: Path, entry: dict, color_fn: Optional[ColorFn] = None) -> None:
    """Run the settings menu for one priority entry, looping until dismissed with Esc."""
    color = color_fn or _default_color_fn
    site = entry["site"]
    streamer = entry["streamer"]
    index = 0
    while True:
        current_entry = priority_config.get_order(data_dir)
        current_entry = next(
            (e for e in current_entry if e["site"] == site and e["streamer"] == streamer), entry
        )
        _draw_menu(stdscr, streamer, current_entry, index, color)
        key = stdscr.getch()
        if key == curses.KEY_UP:
            index = (index - 1) % len(MENU_ITEMS)
        elif key == curses.KEY_DOWN:
            index = (index + 1) % len(MENU_ITEMS)
        elif key in (curses.KEY_ENTER, 10, 13):
            site_config = sites_config.load_site(data_dir, site)
            _field, _label, opener = MENU_ITEMS[index]
            opener(stdscr, data_dir, site, streamer, site_config, color)
        elif key == 27:
            return
