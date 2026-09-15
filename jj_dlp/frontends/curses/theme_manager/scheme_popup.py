"""Startup random-scheme popup (Doc 1 §19)."""

from __future__ import annotations

import curses
import random
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.theme import store

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def pick_random_theme_id(themes: List[dict], exclude_id: Optional[str] = None) -> Optional[str]:
    """Return a random theme id from the list, avoiding exclude_id when more than one exists."""
    if not themes:
        return None
    ids = [theme["id"] for theme in themes]
    candidates = [tid for tid in ids if tid != exclude_id] or ids
    return random.choice(candidates)


def _draw(stdscr, theme_name: str, color: ColorFn) -> None:
    """Render the yes/no prompt box centered on the screen."""
    height, width = stdscr.getmaxyx()
    lines = [f"Try the \"{theme_name}\" theme for this session?", "y: yes   n/Esc: no"]
    box_width = min(width - 2, max(len(line) for line in lines) + 4)
    box_height = len(lines) + 2
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)

    win = curses.newwin(box_height, box_width, y1, x1)
    win.attrset(color("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    try:
        win.addstr(1, 2, lines[0][: box_width - 4], color("popup.title", None))
        win.addstr(2, 2, lines[1][: box_width - 4], curses.A_DIM)
    except curses.error:
        pass
    win.noutrefresh()
    curses.doupdate()


def maybe_offer_random_scheme(
    stdscr, data_dir: Path, color_fn: Optional[ColorFn] = None
) -> Optional[str]:
    """Offer a randomly chosen theme for this session only; return its id if accepted, else None."""
    data_dir = Path(data_dir)
    color = color_fn or _default_color_fn
    themes = store.list_themes(data_dir)
    active_id = store.get_active_theme(data_dir)["id"]
    if len(themes) < 2:
        return None

    theme_id = pick_random_theme_id(themes, exclude_id=active_id)
    theme = next((t for t in themes if t["id"] == theme_id), None)
    if theme is None:
        return None

    _draw(stdscr, theme.get("name", theme_id), color)
    while True:
        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            return theme_id
        if key in (ord("n"), ord("N"), 27):
            return None
