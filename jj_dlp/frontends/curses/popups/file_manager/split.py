"""Split popup: split a file into two parts at one timestamp."""

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


def _confirm(stdscr, message: str, color: ColorFn) -> bool:
    """Blocking yes/no confirmation box; y/Enter confirms, anything else cancels."""
    height, width = stdscr.getmaxyx()
    lines = [message, "y = yes, any other key = cancel"]
    box_width = min(width - 2, max(len(line) for line in lines) + 4)
    box_height = len(lines) + 2
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)
    win = curses.newwin(box_height, box_width, y1, x1)
    win.erase()
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
    key = stdscr.getch()
    return key in (ord("y"), ord("Y"), curses.KEY_ENTER, 10, 13)


def _run_with_progress(stdscr, message: str, color: ColorFn, work: Callable[[], tuple]) -> str:
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
        return f"Split failed: {result['error']}"
    part1, part2 = result["value"]
    return f"Split -> {Path(part1).name}, {Path(part2).name}"


def open_split_popup(stdscr, file_row, color_fn: Optional[ColorFn] = None, data_dir=None) -> Optional[str]:
    """Run the Split popup for one file: pick a split point, confirm, split via file_ops, return a status message."""
    color = color_fn or _default_color_fn
    filename = Path(file_row.path).name
    at = ""
    while True:
        at = _edit_text_field(stdscr, f"Split point \u2014 {filename}", at, color)
        if at is None:
            return None
        if _is_valid_time(at):
            break

    if not _confirm(stdscr, f"Split {filename} at {at}?", color):
        return None

    return _run_with_progress(
        stdscr,
        f"Splitting {filename}...",
        color,
        lambda: file_ops.split(file_row.path, at),
    )
