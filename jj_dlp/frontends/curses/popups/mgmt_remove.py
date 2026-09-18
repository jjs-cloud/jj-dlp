"""Remove-streamer management overlay popup (Doc 1 §17)."""

from __future__ import annotations

import curses
import logging
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.config import priority as priority_config
from jj_dlp.core.config import sites as sites_config
from jj_dlp.core.config import state as state_config

log = logging.getLogger("jj_dlp.popups.mgmt_remove")

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


class RemoveStreamerPicker:
    """List picker for choosing which streamer to remove from a site."""

    def __init__(self, label: str, streamers: List[str], color_fn: Optional[ColorFn] = None) -> None:
        self.label = label
        self.streamers = streamers
        self.color = color_fn or _default_color_fn
        self.index = 0

    def draw(self, stdscr) -> None:
        """Render the centered list of streamers, highlighting the current selection."""
        height, width = stdscr.getmaxyx()
        title = f"Remove streamer from {self.label}"
        rows = list(self.streamers) or ["(no streamers)"]

        box_width = max(24, min(width - 2, max(len(r) for r in rows + [title]) + 4))
        box_height = max(5, min(height - 2, len(rows) + 4))
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.erase()
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        win.addstr(0, 2, f" {title} "[: box_width - 4], self.color("popup.title", None))
        for i, row_text in enumerate(rows):
            row = 2 + i
            if row >= box_height - 1:
                break
            attr = self.color("popup.button_focused", None) if i == self.index else curses.A_NORMAL
            win.addstr(row, 2, row_text[: box_width - 4], attr)
        win.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int) -> Optional[str]:
        """Apply one keypress. Returns "" on cancel, a streamer name on confirm, else None."""
        if not self.streamers:
            if key == 27:
                return ""
            return None
        if key == curses.KEY_UP:
            self.index = (self.index - 1) % len(self.streamers)
        elif key == curses.KEY_DOWN:
            self.index = (self.index + 1) % len(self.streamers)
        elif key in (curses.KEY_ENTER, 10, 13):
            return self.streamers[self.index]
        elif key == 27:
            return ""
        return None


class RemoveStreamerConfirm:
    """Yes/no confirmation shown before a streamer is actually removed."""

    def __init__(self, label: str, streamer: str, color_fn: Optional[ColorFn] = None) -> None:
        self.label = label
        self.streamer = streamer
        self.color = color_fn or _default_color_fn

    def draw(self, stdscr) -> None:
        """Render the confirmation box centered on the screen."""
        height, width = stdscr.getmaxyx()
        lines = [f"Remove '{self.streamer}' from {self.label}?", "(y/n)"]
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


def _remove_streamer_data(data_dir: Path, label: str, streamer: str) -> None:
    """Drop streamer from the site's list and clean up priority/live-status references."""
    site = sites_config.load_site(data_dir, label)
    streamers = site.get("streamers", [])
    if streamer in streamers:
        streamers.remove(streamer)
        sites_config.save_site(data_dir, label, site)
    priority_config.remove_entry(data_dir, label, streamer)
    state_config.remove_streamer_live_status(data_dir, label, streamer)
    log.info("Removed streamer '%s' from site '%s'", streamer, label)


def remove_streamer(stdscr, data_dir: Path, label: str, color_fn: Optional[ColorFn] = None) -> Optional[str]:
    """Run the pick-then-confirm remove-streamer flow; returns the removed name, or None."""
    site = sites_config.load_site(data_dir, label)
    streamers = list(site.get("streamers", []))

    picker = RemoveStreamerPicker(label, streamers, color_fn)
    while True:
        picker.draw(stdscr)
        key = stdscr.getch()
        chosen = picker.handle_key(key)
        if chosen == "":
            return None
        if chosen:
            break

    confirm = RemoveStreamerConfirm(label, chosen, color_fn)
    confirm.draw(stdscr)
    while True:
        key = stdscr.getch()
        decision = confirm.handle_key(key)
        if decision is None:
            continue
        if not decision:
            return None
        _remove_streamer_data(data_dir, label, chosen)
        return chosen
