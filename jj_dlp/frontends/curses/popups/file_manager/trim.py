"""Trim popup: cut a file between two timestamps."""

from __future__ import annotations

import curses
import re
import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.engine import file_ops

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_SPINNER = "|/-\\"
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}(\.\d+)?$|^\d+(\.\d+)?$")


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _is_valid_time(value: str) -> bool:
    """Accept HH:MM:SS(.ms) or a plain seconds value, per Doc 1 §5.14."""
    return bool(value) and bool(_TIME_RE.match(value))


def _edit_text_field(stdscr, title: str, initial: str, color: ColorFn) -> Optional[str]:
    """Blocking single-line text-entry popup pre-filled with initial; returns edited text or None."""
    curses.curs_set(1)
    buf: List[str] = list(initial)
    pos = len(buf)
    try:
        while True:
            height, width = stdscr.getmaxyx()
            box_width = max(30, min(width - 2, 50))
            win = curses.newwin(4, box_width, max(0, height // 2 - 2), max(0, (width - box_width) // 2))
            win.erase()
            win.attrset(color("popup.border", None))
            win.border()
            win.attrset(curses.A_NORMAL)
            try:
                win.addstr(0, 2, f" {title} "[: box_width - 4], color("popup.title", None))
                shown = "".join(buf)[: box_width - 4]
                win.addstr(1, 2, shown)
                win.addstr(2, 2, "Enter confirm  Esc cancel"[: box_width - 4], curses.A_DIM)
                win.move(1, min(box_width - 3, 2 + pos))
            except curses.error:
                pass
            win.noutrefresh()
            curses.doupdate()

            key = stdscr.getch()
            if key in (curses.KEY_ENTER, 10, 13):
                return "".join(buf).strip()
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
            elif key == curses.KEY_DC:
                if pos < len(buf):
                    del buf[pos]
            elif 32 <= key < 256:
                buf.insert(pos, chr(key))
                pos += 1
    finally:
        curses.curs_set(0)


class _TrimPopup:
    """Collects start/end times plus delete-original/convert-to-mp4 before running trim."""

    _ROWS = ("Start", "End", "Delete original", "Convert to MP4", "Run")

    def __init__(self, filename: str, color_fn: ColorFn) -> None:
        self.filename = filename
        self.color = color_fn
        self.start = ""
        self.end = ""
        self.checked = [False, False]
        self.index = 0
        self.error = ""
        self._win = None

    def draw(self, stdscr) -> None:
        """Render the centered start/end/checkbox/run box."""
        height, width = stdscr.getmaxyx()
        title = f"Trim \u2014 {self.filename}"
        box_width = max(36, min(width - 2, max(len(title) + 4, 48)))
        box_height = len(self._ROWS) + 4 + (1 if self.error else 0)

        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.keypad(True)
        win.erase()
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        try:
            win.addstr(0, 2, f" {title} "[: box_width - 4], self.color("popup.title", None))
            for i, label in enumerate(self._ROWS):
                row = 2 + i
                attr = self.color("popup.button_focused", None) if i == self.index else curses.A_NORMAL
                if label == "Start":
                    text = f"Start: {self.start or '(unset)'}"
                elif label == "End":
                    text = f"End:   {self.end or '(unset)'}"
                elif label in ("Delete original", "Convert to MP4"):
                    mark = "x" if self.checked[self._ROWS.index(label) - 2] else " "
                    text = f"[{mark}] {label}"
                else:
                    text = "Run"
                win.addstr(row, 2, text[: box_width - 4], attr)
            if self.error:
                win.addstr(box_height - 2, 2, self.error[: box_width - 4], curses.A_BOLD)
            hint = "\u2191/\u2193 move  Enter edit/run  Space toggle  Esc cancel"
            win.addstr(box_height - 1, 2, hint[: box_width - 4], curses.A_DIM)
        except curses.error:
            pass
        self._win = win
        win.noutrefresh()
        curses.doupdate()

    def run(self, stdscr) -> Optional[Tuple[str, str, bool, bool]]:
        """Blocking edit loop; returns (start, end, delete_original, convert_to_mp4), or None if canceled."""
        while True:
            self.draw(stdscr)
            key = self._win.getch()
            if key == curses.KEY_UP:
                self.index = max(0, self.index - 1)
                self.error = ""
            elif key == curses.KEY_DOWN:
                self.index = min(len(self._ROWS) - 1, self.index + 1)
                self.error = ""
            elif key in (curses.KEY_ENTER, 10, 13):
                if self.index == 0:
                    edited = _edit_text_field(stdscr, "Start time", self.start, self.color)
                    if edited is not None:
                        self.start = edited
                elif self.index == 1:
                    edited = _edit_text_field(stdscr, "End time", self.end, self.color)
                    if edited is not None:
                        self.end = edited
                elif self.index in (2, 3):
                    self.checked[self.index - 2] = not self.checked[self.index - 2]
                else:
                    if not _is_valid_time(self.start) or not _is_valid_time(self.end):
                        self.error = "Times must be HH:MM:SS or seconds."
                        continue
                    return (self.start, self.end, self.checked[0], self.checked[1])
            elif key == ord(" ") and self.index in (2, 3):
                self.checked[self.index - 2] = not self.checked[self.index - 2]
            elif key == 27:
                return None


def _run_with_progress(stdscr, message: str, color: ColorFn, work: Callable[[], str]) -> str:
    """Run `work` on a background thread, animating a spinner until it finishes."""
    result: dict = {}

    def _target() -> None:
        try:
            result["value"] = work()
        except Exception as exc:  # report the failure instead of crashing the thread
            result["error"] = str(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()

    height, width = stdscr.getmaxyx()
    frame = 0
    stdscr.nodelay(True)
    try:
        while thread.is_alive():
            text = f"{_SPINNER[frame % len(_SPINNER)]} {message}"
            y = height // 2
            x = max(0, (width - len(text)) // 2)
            try:
                stdscr.addstr(y, x, text, color("popup.title", None))
            except curses.error:
                pass
            stdscr.refresh()
            curses.napms(120)
            stdscr.getch()
            frame += 1
    finally:
        stdscr.nodelay(False)
    thread.join()

    if "error" in result:
        return f"Trim failed: {result['error']}"
    return f"Trimmed -> {Path(result['value']).name}"


def open_trim_popup(stdscr, file_row, color_fn: Optional[ColorFn] = None, data_dir=None) -> Optional[str]:
    """Run the Trim popup for one file: pick start/end/options, cut via file_ops, return a status message."""
    color = color_fn or _default_color_fn
    filename = Path(file_row.path).name
    choice = _TrimPopup(filename, color).run(stdscr)
    if choice is None:
        return None
    start, end, delete_original, convert_to_mp4 = choice
    return _run_with_progress(
        stdscr,
        f"Trimming {filename}...",
        color,
        lambda: file_ops.trim(file_row.path, start, end, delete_original, convert_to_mp4),
    )
