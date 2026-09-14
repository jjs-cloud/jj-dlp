"""Shared yes/no confirm-reset popup (Doc 1 §16.1)."""

from __future__ import annotations

import curses
from typing import Callable, Optional, Tuple

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


class ConfirmResetPopup:
    """Yes/no confirmation shown before an override reset takes effect."""

    def __init__(self, override_name: str, streamer: str, color_fn: Optional[ColorFn] = None) -> None:
        self.override_name = override_name
        self.streamer = streamer
        self.color = color_fn or _default_color_fn

    def draw(self, stdscr) -> None:
        """Render the confirmation box centered on the screen."""
        height, width = stdscr.getmaxyx()
        lines = [
            f"Reset {self.override_name} for {self.streamer} to site defaults?",
            "(y/n)",
        ]
        box_width = min(width - 2, max(len(line) for line in lines) + 4)
        box_height = len(lines) + 2
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        for i, line in enumerate(lines):
            try:
                win.addstr(1 + i, 2, line[: box_width - 4], self.color("popup.title", None))
            except curses.error:
                pass
        win.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int) -> Optional[bool]:
        """Apply one keypress. Returns None to keep waiting, else the yes/no decision."""
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27):
            return False
        return None


def confirm_reset(stdscr, override_name: str, streamer: str, color_fn: Optional[ColorFn] = None) -> bool:
    """Blocking prompt; returns True if the user confirms the reset."""
    popup = ConfirmResetPopup(override_name, streamer, color_fn)
    popup.draw(stdscr)
    while True:
        key = stdscr.getch()
        result = popup.handle_key(key)
        if result is not None:
            return result
