"""Move-filename popup: adjust destination filename, then move with collision-safe renaming."""

from __future__ import annotations

import curses
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.engine import file_ops

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_SPINNER = "|/-\\"


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _default_filename(file_row) -> str:
    """Build a sensible default destination filename from streamer name + timestamp."""
    ext = Path(file_row.path).suffix
    stem = getattr(file_row, "streamer", None) or Path(file_row.path).stem
    stamp = time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(file_row.modified))
    return f"{stem} {stamp}{ext}"


def _edit_filename(stdscr, title: str, initial: str, color: ColorFn) -> Optional[str]:
    """Blocking single-line text-entry popup pre-filled with initial; returns edited text or None."""
    curses.curs_set(1)
    buf: List[str] = list(initial)
    pos = len(buf)
    try:
        while True:
            height, width = stdscr.getmaxyx()
            box_width = max(30, min(width - 2, 70))
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
        return f"Move failed: {result['error']}"
    return f"Moved -> {Path(result['value']).name}"


def open_move_filename_popup(stdscr, file_row, destination_dir: str, color_fn: Optional[ColorFn] = None) -> Optional[str]:
    """Let the user adjust the destination filename, then move the file there."""
    color = color_fn or _default_color_fn
    default_name = _default_filename(file_row)
    name = _edit_filename(stdscr, "Move as...", default_name, color)
    if name is None:
        return None
    if not name:
        name = default_name
    return _run_with_progress(
        stdscr,
        f"Moving {Path(file_row.path).name}...",
        color,
        lambda: file_ops.move(file_row.path, destination_dir, new_name=name),
    )
