"""Help popup listing the active tab's keybinds plus the global ones."""

from __future__ import annotations

import curses
from typing import Callable, List, Optional, Tuple

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def show_help(stdscr, hints: List[Tuple[str, str]], color_fn: Optional[ColorFn] = None) -> None:
    """Render a centered keybind list and block until any key dismisses it."""
    color = color_fn or _default_color_fn
    height, width = stdscr.getmaxyx()

    title = "Help — keybinds"
    lines = [f"{key:>5}  {label}" for key, label in hints]
    box_width = min(width - 2, max(len(title), *(len(line) for line in lines)) + 4) if lines else len(title) + 4
    box_height = min(height - 2, len(lines) + 4)
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)

    win = curses.newwin(box_height, box_width, y1, x1)
    win.attrset(color("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    win.addstr(1, 2, title[: box_width - 4], color("popup.title", None))
    visible = lines[: box_height - 4]
    for i, line in enumerate(visible):
        win.addstr(2 + i, 2, line[: box_width - 4], color("config.field_label", None))
    win.addstr(box_height - 2, 2, "press any key to close"[: box_width - 4], color("footer.hint_text", None))
    win.noutrefresh()
    curses.doupdate()

    stdscr.getch()
