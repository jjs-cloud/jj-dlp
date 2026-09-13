"""Startup site picker."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.config import sites as sites_config

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


class SitePicker:
    """Multi-select list of site labels, defaulting to all selected."""

    def __init__(self, labels: List[str], color_fn: Optional[ColorFn] = None) -> None:
        self.labels = labels
        self.selected = [True] * len(labels)
        self.cursor = 0
        self.color = color_fn or _default_color_fn

    def draw(self, stdscr) -> None:
        """Render the picker: title, one row per site, and footer hints."""
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.attrset(self.color("popup.border", None))
        stdscr.border()
        stdscr.attrset(curses.A_NORMAL)

        title = " Select sites to load "
        if width > len(title) + 2:
            stdscr.addstr(0, 2, title, self.color("popup.title", None))

        if not self.labels:
            stdscr.addstr(2, 2, "No sites configured yet.")
        else:
            for i, label in enumerate(self.labels):
                row = 2 + i
                if row >= height - 2:
                    break
                mark = "[x]" if self.selected[i] else "[ ]"
                attr = self.color("popup.button_focused", None) if i == self.cursor else curses.A_NORMAL
                stdscr.addstr(row, 2, f"{mark} {label}"[: max(0, width - 4)], attr)

        hint = "space: toggle  a: all  n: none  enter: confirm  q: quit"
        if height > 1:
            stdscr.addstr(height - 2, 2, hint[: max(0, width - 4)], self.color("footer.hint_text", None))
        stdscr.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int) -> Optional[bool]:
        """Apply one keypress. Returns None to keep running, or a bool for the final decision."""
        if not self.labels:
            return True
        if key in (curses.KEY_UP, ord("k")):
            self.cursor = (self.cursor - 1) % len(self.labels)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor = (self.cursor + 1) % len(self.labels)
        elif key == ord(" "):
            self.selected[self.cursor] = not self.selected[self.cursor]
        elif key == ord("a"):
            self.selected = [True] * len(self.labels)
        elif key == ord("n"):
            self.selected = [False] * len(self.labels)
        elif key in (curses.KEY_ENTER, 10, 13):
            return True
        elif key in (ord("q"), ord("Q"), 27):
            return False
        return None

    def chosen_labels(self) -> List[str]:
        """Return the currently-selected labels, in list order."""
        return [label for label, is_on in zip(self.labels, self.selected) if is_on]


def pick_sites(stdscr, data_dir: Path, color_fn: Optional[ColorFn] = None) -> List[str]:
    """Run the picker loop on an existing screen and return the chosen site labels."""
    labels = sites_config.list_sites(data_dir)
    picker = SitePicker(labels, color_fn)
    if not labels:
        picker.draw(stdscr)
        stdscr.getch()
        return []

    picker.draw(stdscr)
    while True:
        key = stdscr.getch()
        result = picker.handle_key(key)
        picker.draw(stdscr)
        if result is True:
            return picker.chosen_labels()
        if result is False:
            return []


def _entry(stdscr, data_dir: Path) -> List[str]:
    """curses.wrapper target for standalone sanity-checking of the picker."""
    curses.curs_set(0)
    return pick_sites(stdscr, data_dir)


def main(data_dir: Path) -> List[str]:
    """Initialize curses and run the picker alone, returning the chosen labels."""
    return curses.wrapper(_entry, data_dir)
