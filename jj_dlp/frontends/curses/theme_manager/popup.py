"""Theme manager top-level shell (Doc 1 §19)."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.theme import store

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

# Step 14.3 wires the real element editor in here; until then this entry
# just explains what the key will do. Role editing is passed in by the
# caller as edit_role_fn (see theme_manager/role_edit.py, Step 14.2).
_ELEMENT_EDIT_PLACEHOLDER = "Element editing arrives in Step 14.3."

RoleEditFn = Callable[..., None]
ElementEditFn = Callable[..., None]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _draw_message(stdscr, message: str, color: ColorFn) -> None:
    """Show a centered, dismiss-on-any-key message box."""
    height, width = stdscr.getmaxyx()
    box_width = min(width - 2, max(20, len(message) + 4))
    box_height = 3
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)

    win = curses.newwin(box_height, box_width, y1, x1)
    win.attrset(color("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    try:
        win.addstr(1, 2, message[: box_width - 4], color("popup.title", None))
    except curses.error:
        pass
    win.noutrefresh()
    curses.doupdate()
    win.getch()


def _draw_theme_list(stdscr, themes: List[dict], active_id: str, index: int, color: ColorFn) -> None:
    """Render the centered list of themes, marking the active one and the cursor."""
    height, width = stdscr.getmaxyx()
    title = "Theme Manager"
    rows = []
    for theme in themes:
        marker = "*" if theme["id"] == active_id else " "
        rows.append(f"{marker} {theme.get('name', theme['id'])}")
    hint = "\u2191/\u2193 select  Enter activate  r roles  x elements  Esc close"

    box_width = min(width - 2, max(len(r) for r in rows + [title, hint]) + 4)
    box_height = min(height - 2, len(rows) + 5)
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)

    win = curses.newwin(box_height, box_width, y1, x1)
    win.erase()
    win.attrset(color("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    win.addstr(0, 2, f" {title} "[: box_width - 4], color("popup.title", None))
    for i, row_text in enumerate(rows):
        row = 1 + i
        if row >= box_height - 2:
            break
        attr = color("popup.button_focused", None) if i == index else curses.A_NORMAL
        try:
            win.addstr(row, 2, row_text[: box_width - 4], attr)
        except curses.error:
            pass
    try:
        win.addstr(box_height - 2, 2, hint[: box_width - 4], curses.A_DIM)
    except curses.error:
        pass
    win.noutrefresh()
    curses.doupdate()


def open_theme_manager(
    stdscr,
    data_dir: Path,
    color_fn: Optional[ColorFn] = None,
    edit_role_fn: Optional[RoleEditFn] = None,
    edit_element_fn: Optional[ElementEditFn] = None,
) -> None:
    """Run the theme manager shell: pick the active theme, or enter role/element editing."""
    color = color_fn or _default_color_fn
    index = 0
    while True:
        themes = store.list_themes(data_dir)
        active_id = store.get_active_theme(data_dir)["id"]
        index = min(index, len(themes) - 1) if themes else 0
        _draw_theme_list(stdscr, themes, active_id, index, color)
        key = stdscr.getch()
        if key == curses.KEY_UP:
            index = max(0, index - 1)
        elif key == curses.KEY_DOWN:
            index = min(len(themes) - 1, index + 1)
        elif key in (curses.KEY_ENTER, 10, 13) and themes:
            store.set_active_theme(data_dir, themes[index]["id"])
        elif key in (ord("r"), ord("R")):
            if edit_role_fn is not None:
                edit_role_fn(stdscr, data_dir, active_id, color)
            else:
                _draw_message(stdscr, "Role editor not available.", color)
        elif key in (ord("x"), ord("X")):
            if edit_element_fn is not None:
                edit_element_fn(stdscr, data_dir, active_id, color)
            else:
                _draw_message(stdscr, _ELEMENT_EDIT_PLACEHOLDER, color)
        elif key == 27:
            return
