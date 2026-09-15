"""File Options menu popup (Doc 1 §18)."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, Optional, Tuple

from jj_dlp.core.engine.file_scan import FileRow
from jj_dlp.frontends.curses.popups.file_manager.fixup import open_fixup_popup
from jj_dlp.frontends.curses.popups.file_manager.move import open_move_popup
from jj_dlp.frontends.curses.popups.file_manager.split import open_split_popup
from jj_dlp.frontends.curses.popups.file_manager.trim import open_trim_popup

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

# (menu label, popup opener) for the four file operations.
MENU_ITEMS = (
    ("Fixup", open_fixup_popup),
    ("Move", open_move_popup),
    ("Trim", open_trim_popup),
    ("Split", open_split_popup),
)


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _draw_menu(stdscr, filename: str, index: int, color_fn: ColorFn) -> None:
    """Render the centered File Options menu for the given filename."""
    height, width = stdscr.getmaxyx()
    title = f"File Options \u2014 {filename}"
    rows = [label for label, _opener in MENU_ITEMS]

    box_width = max(24, min(width - 2, max(len(r) for r in rows + [title]) + 4))
    box_height = max(5, min(height - 2, len(rows) + 4))
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)

    win = curses.newwin(box_height, box_width, y1, x1)
    win.erase()
    win.attrset(color_fn("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    try:
        win.addstr(0, 2, f" {title} "[: box_width - 4], color_fn("popup.title", None))
        for i, row_text in enumerate(rows):
            row = 2 + i
            if row >= box_height - 1:
                break
            attr = color_fn("popup.button_focused", None) if i == index else curses.A_NORMAL
            win.addstr(row, 2, row_text[: box_width - 4], attr)
    except curses.error:
        pass
    win.noutrefresh()
    curses.doupdate()


def open_file_menu(stdscr, file_row: FileRow, color_fn: Optional[ColorFn] = None) -> Optional[str]:
    """Run the File Options menu for one file, looping until dismissed with Esc.

    Returns a status message from whichever operation ran, or None if nothing changed.
    """
    color = color_fn or _default_color_fn
    filename = Path(file_row.path).name
    index = 0
    while True:
        _draw_menu(stdscr, filename, index, color)
        key = stdscr.getch()
        if key == curses.KEY_UP:
            index = (index - 1) % len(MENU_ITEMS)
        elif key == curses.KEY_DOWN:
            index = (index + 1) % len(MENU_ITEMS)
        elif key in (curses.KEY_ENTER, 10, 13):
            _label, opener = MENU_ITEMS[index]
            result = opener(stdscr, file_row, color)
            if result is not None:
                return result
        elif key == 27:
            return None
