"""Exit confirmation popup for active recordings."""

from __future__ import annotations

import curses
from typing import Callable, Iterable, Optional, Tuple

from jj_dlp.core.engine.site_state import SiteState

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def count_active_recordings(site_states: Iterable[SiteState]) -> int:
    """Return how many streamers, across the given sites, are currently recording."""
    count = 0
    for site_state in site_states:
        snapshot = site_state.snapshot()
        count += sum(1 for s in snapshot.streamers.values() if s.recording)
    return count


class ExitConfirmPopup:
    """Yes/no confirmation shown when quitting while recordings are active."""

    def __init__(self, active_count: int, color_fn: Optional[ColorFn] = None) -> None:
        self.active_count = active_count
        self.color = color_fn or _default_color_fn

    def draw(self, stdscr) -> None:
        """Render the confirmation box centered on the screen."""
        height, width = stdscr.getmaxyx()
        plural = "recording" if self.active_count == 1 else "recordings"
        lines = [
            f"{self.active_count} active {plural} will be stopped.",
            "Quit anyway? (y/n)",
        ]
        box_width = max(len(line) for line in lines) + 4
        box_height = len(lines) + 2
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        for i, line in enumerate(lines):
            win.addstr(1 + i, 2, line[: box_width - 4], self.color("popup.title", None))
        win.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int) -> Optional[bool]:
        """Apply one keypress. Returns None to keep waiting, else the yes/no decision."""
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27):
            return False
        return None


def confirm_exit(stdscr, site_states: Iterable[SiteState], color_fn: Optional[ColorFn] = None) -> bool:
    """Return whether to proceed with quitting, prompting only if recordings are active."""
    active_count = count_active_recordings(site_states)
    if active_count == 0:
        return True

    popup = ExitConfirmPopup(active_count, color_fn)
    popup.draw(stdscr)
    while True:
        key = stdscr.getch()
        result = popup.handle_key(key)
        if result is not None:
            return result
