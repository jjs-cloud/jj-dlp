"""Split popup: split a file at one timestamp."""

from __future__ import annotations

import curses
from typing import Callable, Optional, Tuple

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _placeholder(stdscr, message: str, color: ColorFn) -> None:
    """Blocking dismiss-on-any-key box shown until this popup is fully built."""
    height, width = stdscr.getmaxyx()
    lines = [message, "(any key)"]
    box_width = min(width - 2, max(len(line) for line in lines) + 4)
    box_height = len(lines) + 2
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)
    win = curses.newwin(box_height, box_width, y1, x1)
    win.attrset(color("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    try:
        for i, line in enumerate(lines):
            win.addstr(1 + i, 2, line[: box_width - 4], color("popup.title", None))
    except curses.error:
        pass
    win.noutrefresh()
    curses.doupdate()
    stdscr.getch()


def open_split_popup(stdscr, file_row, color_fn: Optional[ColorFn] = None) -> Optional[str]:
    """Placeholder Split popup; Step 13.10 replaces this with the real confirm/run flow."""
    _placeholder(stdscr, "Split not yet implemented.", color_fn or _default_color_fn)
    return None
