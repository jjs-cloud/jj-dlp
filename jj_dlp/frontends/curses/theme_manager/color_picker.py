"""Shared fg/bg/bold color picker widget (Doc 1 §19)."""

from __future__ import annotations

import curses
from typing import Callable, Dict, Optional, Tuple

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

# The eight portable curses color names usable for a role/element fg or bg.
COLOR_NAMES = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]

_CURSES_COLOR_NAMES: Dict[str, int] = {
    "black": curses.COLOR_BLACK,
    "red": curses.COLOR_RED,
    "green": curses.COLOR_GREEN,
    "yellow": curses.COLOR_YELLOW,
    "blue": curses.COLOR_BLUE,
    "magenta": curses.COLOR_MAGENTA,
    "cyan": curses.COLOR_CYAN,
    "white": curses.COLOR_WHITE,
}

# Isolated pair-id range for preview swatches, kept apart from a themed app's own ids.
_PREVIEW_PAIR_BASE = 200
_preview_pair_ids: Dict[Tuple[str, str], int] = {}


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _preview_attr(fg: str, bg: str, bold: bool) -> int:
    """Curses attribute for an exact (fg, bg) pair, independent of any theme resolution."""
    key = (fg, bg)
    pair_id = _preview_pair_ids.get(key)
    if pair_id is None:
        pair_id = _PREVIEW_PAIR_BASE + len(_preview_pair_ids)
        if pair_id >= curses.COLOR_PAIRS:
            return curses.A_BOLD if bold else curses.A_NORMAL
        try:
            curses.init_pair(
                pair_id,
                _CURSES_COLOR_NAMES.get(fg, curses.COLOR_WHITE),
                _CURSES_COLOR_NAMES.get(bg, curses.COLOR_BLACK),
            )
        except curses.error:
            return curses.A_BOLD if bold else curses.A_NORMAL
        _preview_pair_ids[key] = pair_id
    attr = curses.color_pair(pair_id)
    if bold:
        attr |= curses.A_BOLD
    return attr


def pick_color(
    stdscr,
    title: str,
    fg: str,
    bg: str,
    bold: bool,
    color_fn: Optional[ColorFn] = None,
) -> Optional[ColorTuple]:
    """Boxed fg/bg/bold picker with a live preview swatch. Returns (fg, bg, bold), or None if canceled."""
    color = color_fn or _default_color_fn
    fg_i = COLOR_NAMES.index(fg) if fg in COLOR_NAMES else COLOR_NAMES.index("white")
    bg_i = COLOR_NAMES.index(bg) if bg in COLOR_NAMES else COLOR_NAMES.index("black")
    values = [fg_i, bg_i, bold]
    labels = ["Foreground", "Background", "Bold"]
    index = 0

    height, width = stdscr.getmaxyx()
    box_width = min(width - 2, 46)
    box_height = 8
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)
    win = curses.newwin(box_height, box_width, y1, x1)
    win.keypad(True)

    while True:
        win.erase()
        win.attrset(color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        try:
            win.addstr(0, 2, f" {title} "[: box_width - 4], color("popup.title", None))
        except curses.error:
            pass
        for i, label in enumerate(labels):
            row_y = 1 + i
            text = ("on" if values[2] else "off") if i == 2 else COLOR_NAMES[values[i]]
            attr = color("popup.button_focused", None) if i == index else curses.A_NORMAL
            try:
                win.addstr(row_y, 2, f"{label}:", attr)
                win.addstr(row_y, 14, f"< {text} >"[: box_width - 16], attr)
            except curses.error:
                pass
        try:
            win.addstr(5, 2, "Preview:", curses.A_NORMAL)
            win.addstr(
                5,
                14,
                " sample text "[: box_width - 16],
                _preview_attr(COLOR_NAMES[values[0]], COLOR_NAMES[values[1]], bool(values[2])),
            )
        except curses.error:
            pass
        hint = "\u2191/\u2193 field  \u2190/\u2192 change  Space toggle  s save  Esc cancel"
        try:
            win.addstr(box_height - 2, 2, hint[: box_width - 4], curses.A_DIM)
        except curses.error:
            pass
        win.noutrefresh()
        curses.doupdate()

        key = win.getch()
        if key == curses.KEY_UP:
            index = max(0, index - 1)
        elif key == curses.KEY_DOWN:
            index = min(2, index + 1)
        elif index == 2 and key in (curses.KEY_ENTER, 10, 13, ord(" "), curses.KEY_LEFT, curses.KEY_RIGHT):
            values[2] = not values[2]
        elif index in (0, 1) and key == curses.KEY_LEFT:
            values[index] = (values[index] - 1) % len(COLOR_NAMES)
        elif index in (0, 1) and key == curses.KEY_RIGHT:
            values[index] = (values[index] + 1) % len(COLOR_NAMES)
        elif key in (ord("s"), ord("S")):
            return COLOR_NAMES[values[0]], COLOR_NAMES[values[1]], bool(values[2])
        elif key == 27:
            return None
