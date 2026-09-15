"""Move popup: pick a destination, or add a new one on the fly."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.config import app as app_config
from jj_dlp.frontends.curses.popups.file_manager.move_filename import open_move_filename_popup

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_ADD_LABEL = "+ Add destination..."


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _edit_text(stdscr, prompt: str, color_fn: ColorFn) -> Optional[str]:
    """Blocking single-line text-entry popup; returns the text, or None if cancelled."""
    curses.curs_set(1)
    buf: List[str] = []
    pos = 0
    try:
        while True:
            height, width = stdscr.getmaxyx()
            win_width = max(20, min(width - 4, 60))
            win = curses.newwin(3, win_width, max(0, height // 2 - 1), max(0, (width - win_width) // 2))
            win.erase()
            win.attrset(color_fn("popup.border", None))
            win.border()
            win.attrset(curses.A_NORMAL)
            shown = (prompt + "".join(buf))[: win_width - 4]
            try:
                win.addstr(1, 2, shown)
                win.move(1, min(win_width - 2, 2 + len(prompt) + pos))
            except curses.error:
                pass
            win.noutrefresh()
            curses.doupdate()

            key = stdscr.getch()
            if key in (curses.KEY_ENTER, 10, 13):
                return "".join(buf)
            if key == 27:
                return None
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
            elif key == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif key == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif 32 <= key < 256:
                buf.insert(pos, chr(key))
                pos += 1
    finally:
        curses.curs_set(0)


def _add_destination(stdscr, data_dir: Path, color_fn: ColorFn) -> Optional[dict]:
    """Prompt for a name and path, append it to app.json's output.destinations, return it."""
    name = _edit_text(stdscr, "Destination name: ", color_fn)
    if not name:
        return None
    path = _edit_text(stdscr, "Destination path: ", color_fn)
    if not path:
        return None
    dest = {"name": name.strip(), "path": path.strip()}
    config = app_config.load(data_dir)
    destinations = list(config.get_output().destinations)
    destinations.append(dest)
    config.set_output(destinations=destinations)
    app_config.save(data_dir, config)
    return dest


class _DestinationListPopup:
    """Lists destinations plus an "add new" row; returns the chosen row index."""

    def __init__(self, destinations: List[dict], color_fn: ColorFn) -> None:
        self.destinations = destinations
        self.color = color_fn
        self.index = 0

    @property
    def rows(self) -> List[str]:
        """Return display strings for every destination row plus the add-new row."""
        return [f"{d['name']} ({d['path']})" for d in self.destinations] + [_ADD_LABEL]

    def draw(self, stdscr) -> None:
        """Render the centered destination list."""
        height, width = stdscr.getmaxyx()
        title = "Move to..."
        rows = self.rows
        box_width = max(30, min(width - 2, max(len(r) for r in rows + [title]) + 4))
        box_height = max(5, min(height - 2, len(rows) + 4))
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.erase()
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        try:
            win.addstr(0, 2, f" {title} "[: box_width - 4], self.color("popup.title", None))
            for i, row_text in enumerate(rows):
                row = 2 + i
                if row >= box_height - 1:
                    break
                attr = self.color("popup.button_focused", None) if i == self.index else curses.A_NORMAL
                win.addstr(row, 2, row_text[: box_width - 4], attr)
        except curses.error:
            pass
        win.noutrefresh()
        curses.doupdate()

    def run(self, stdscr) -> Optional[int]:
        """Blocking selection loop; returns the chosen row index, or None if cancelled."""
        rows = self.rows
        while True:
            self.draw(stdscr)
            key = stdscr.getch()
            if key == curses.KEY_UP:
                self.index = (self.index - 1) % len(rows)
            elif key == curses.KEY_DOWN:
                self.index = (self.index + 1) % len(rows)
            elif key in (curses.KEY_ENTER, 10, 13):
                return self.index
            elif key == 27:
                return None


def open_move_popup(
    stdscr,
    file_row,
    color_fn: Optional[ColorFn] = None,
    data_dir: Optional[Path] = None,
) -> Optional[str]:
    """Run the Move popup: pick or add a destination, then hand off to the filename popup."""
    color = color_fn or _default_color_fn
    if data_dir is None:
        return "Move unavailable: no data directory."
    data_dir = Path(data_dir)
    config = app_config.load(data_dir)
    destinations = list(config.get_output().destinations)

    while True:
        choice = _DestinationListPopup(destinations, color).run(stdscr)
        if choice is None:
            return None
        if choice == len(destinations):
            new_dest = _add_destination(stdscr, data_dir, color)
            if new_dest is not None:
                destinations.append(new_dest)
            continue
        return open_move_filename_popup(stdscr, file_row, destinations[choice]["path"], color)
