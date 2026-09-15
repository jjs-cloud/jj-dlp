"""Add-streamer management overlay popup (Doc 1 §17)."""

from __future__ import annotations

import curses
from typing import Callable, Optional, Tuple

from jj_dlp.core.config import sites as sites_config

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


class AddStreamerPopup:
    """Text-entry popup that appends a new streamer name to a site."""

    def __init__(self, label: str, existing: list, color_fn: Optional[ColorFn] = None) -> None:
        self.label = label
        self.existing = existing
        self.color = color_fn or _default_color_fn
        self.buf: list = []
        self.pos = 0
        self.error: Optional[str] = None

    def _validate(self, name: str) -> Optional[str]:
        """Return an error message for an invalid/duplicate name, or None if it's fine."""
        if not name:
            return "Streamer name cannot be empty."
        if name in self.existing:
            return f"'{name}' is already on {self.label}."
        return None

    def draw(self, stdscr) -> None:
        """Render the name-entry box, with an error line if the last attempt failed."""
        height, width = stdscr.getmaxyx()
        prompt = f"Add streamer to {self.label}: "
        text = "".join(self.buf)
        lines = [prompt + text]
        if self.error:
            lines.append(self.error)
        box_width = min(width - 2, max(len(line) for line in lines) + 4)
        box_height = len(lines) + 2
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.erase()
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        try:
            win.addstr(1, 2, (prompt + text)[: box_width - 4], self.color("config.field_value", None))
            if self.error:
                win.addstr(2, 2, self.error[: box_width - 4], self.color("log.line_error", None))
        except curses.error:
            pass
        try:
            win.move(1, min(box_width - 2, 2 + len(prompt) + self.pos))
        except curses.error:
            pass
        win.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int) -> Optional[str]:
        """Apply one keypress. Returns the validated name on confirm, "" on cancel, None to keep going."""
        if key in (curses.KEY_ENTER, 10, 13):
            name = "".join(self.buf).strip()
            self.error = self._validate(name)
            if self.error is None:
                return name
            return None
        if key == 27:
            return ""
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.pos > 0:
                del self.buf[self.pos - 1]
                self.pos -= 1
        elif key == curses.KEY_LEFT:
            self.pos = max(0, self.pos - 1)
        elif key == curses.KEY_RIGHT:
            self.pos = min(len(self.buf), self.pos + 1)
        elif 32 <= key < 256:
            self.buf.insert(self.pos, chr(key))
            self.pos += 1
        return None


def add_streamer(stdscr, data_dir, label: str, color_fn: Optional[ColorFn] = None) -> Optional[str]:
    """Run the add-streamer popup; on confirm, persist the new streamer and return its name."""
    site = sites_config.load_site(data_dir, label)
    existing = list(site.get("streamers", [])) + list(site.get("disabled", []))
    popup = AddStreamerPopup(label, existing, color_fn)
    curses.curs_set(1)
    try:
        while True:
            popup.draw(stdscr)
            key = stdscr.getch()
            result = popup.handle_key(key)
            if result == "":
                return None
            if result:
                site.setdefault("streamers", []).append(result)
                sites_config.save_site(data_dir, label, site)
                return result
    finally:
        curses.curs_set(0)
