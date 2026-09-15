"""Fixup popup: remux a file, optionally to mp4, optionally deleting the original."""

from __future__ import annotations

import curses
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

from jj_dlp.core.engine import file_ops

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_SPINNER = "|/-\\"


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


class _FixupOptionsPopup:
    """Checkbox popup collecting delete-original/convert-to-mp4 before running fixup."""

    _OPTIONS = ("Delete original", "Convert to MP4")

    def __init__(self, filename: str, color_fn: ColorFn) -> None:
        self.filename = filename
        self.color = color_fn
        self.checked = [False, False]
        self.index = 0
        self._win = None

    def draw(self, stdscr) -> None:
        """Render the checkbox box centered on the screen."""
        height, width = stdscr.getmaxyx()
        title = f"Fixup \u2014 {self.filename}"
        rows = list(self._OPTIONS) + ["Run"]
        box_width = max(30, min(width - 2, max(len(r) for r in rows + [title]) + 8))
        box_height = len(self._OPTIONS) + 5

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
            for i, label in enumerate(self._OPTIONS):
                mark = "x" if self.checked[i] else " "
                attr = self.color("popup.button_focused", None) if i == self.index else curses.A_NORMAL
                win.addstr(2 + i, 2, f"[{mark}] {label}"[: box_width - 4], attr)
            run_row = 2 + len(self._OPTIONS) + 1
            run_attr = self.color("popup.button_focused", None) if self.index == len(self._OPTIONS) else curses.A_NORMAL
            win.addstr(run_row, 2, "Run"[: box_width - 4], run_attr)
            hint = "\u2191/\u2193 move  Space toggle  Enter run  Esc cancel"
            win.addstr(box_height - 1, 2, hint[: box_width - 4], curses.A_DIM)
        except curses.error:
            pass
        self._win = win
        win.noutrefresh()
        curses.doupdate()

    def run(self, stdscr) -> Optional[Tuple[bool, bool]]:
        """Blocking edit loop; returns (delete_original, convert_to_mp4), or None if canceled."""
        while True:
            self.draw(stdscr)
            key = self._win.getch()
            if key == curses.KEY_UP:
                self.index = max(0, self.index - 1)
            elif key == curses.KEY_DOWN:
                self.index = min(len(self._OPTIONS), self.index + 1)
            elif key == ord(" ") and self.index < len(self._OPTIONS):
                self.checked[self.index] = not self.checked[self.index]
            elif key in (curses.KEY_ENTER, 10, 13):
                return (self.checked[0], self.checked[1])
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
        return f"Fixup failed: {result['error']}"
    return f"Fixed up -> {Path(result['value']).name}"


def open_fixup_popup(stdscr, file_row, color_fn: Optional[ColorFn] = None, data_dir=None) -> Optional[str]:
    """Run the Fixup popup for one file: pick options, remux via file_ops, return a status message."""
    color = color_fn or _default_color_fn
    filename = Path(file_row.path).name
    choice = _FixupOptionsPopup(filename, color).run(stdscr)
    if choice is None:
        return None
    delete_original, convert_to_mp4 = choice
    return _run_with_progress(
        stdscr,
        f"Fixing up {filename}...",
        color,
        lambda: file_ops.fixup(file_row.path, delete_original, convert_to_mp4),
    )
