"""Role list, role edit, and create-new-role UI (Doc 1 §19)."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.theme import roles as roles_core
from jj_dlp.core.theme import store
from jj_dlp.frontends.curses.theme_manager import color_picker

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


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


def _edit_text(win, y: int, x: int, width: int, initial: str, color: ColorFn) -> Optional[str]:
    """Read a line of text in place, starting from initial. None if canceled."""
    curses.curs_set(1)
    curses.echo()
    text = initial
    try:
        while True:
            win.addstr(y, x, " " * max(0, width), curses.A_NORMAL)
            win.addstr(y, x, text[:width], color("popup.button_focused", None))
            win.move(y, x + min(len(text), max(0, width - 1)))
            win.refresh()
            key = win.getch()
            if key in (curses.KEY_ENTER, 10, 13):
                return text
            if key == 27:
                return None
            if key in (curses.KEY_BACKSPACE, 127, 8):
                text = text[:-1]
            elif 0 <= key < 256 and chr(key).isprintable():
                text += chr(key)
    finally:
        curses.noecho()
        curses.curs_set(0)


def _draw_role_list(stdscr, theme_id: str, names: List[str], index: int, color: ColorFn) -> None:
    """Render the centered list of roles in the active theme."""
    height, width = stdscr.getmaxyx()
    title = f"Roles \u2014 {theme_id}"
    hint = "\u2191/\u2193 select  Enter edit  n new role  Esc back"
    rows = names or ["(no roles)"]

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
    for i, name in enumerate(rows):
        row = 1 + i
        if row >= box_height - 2:
            break
        attr = color("popup.button_focused", None) if i == index else curses.A_NORMAL
        try:
            win.addstr(row, 2, name[: box_width - 4], attr)
        except curses.error:
            pass
    try:
        win.addstr(box_height - 2, 2, hint[: box_width - 4], curses.A_DIM)
    except curses.error:
        pass
    win.noutrefresh()
    curses.doupdate()


def _edit_existing_role(stdscr, data_dir: Path, theme: dict, name: str, color: ColorFn) -> None:
    """Open the fg/bg/bold editor for one existing role and save any changes."""
    role = roles_core.get_role(theme, name)
    result = color_picker.pick_color(stdscr, f"Role: {name}", role["fg"], role["bg"], role["bold"], color)
    if result is None:
        return
    fg, bg, bold = result
    roles_core.set_role(theme, name, fg, bg, bold)
    store.save_theme(data_dir, theme)


def _create_new_role(stdscr, data_dir: Path, theme: dict, color: ColorFn) -> None:
    """Prompt for a new role's name and starting colors, then add it to the theme."""
    height, width = stdscr.getmaxyx()
    box_width = min(width - 2, 40)
    win = curses.newwin(3, box_width, max(0, (height - 3) // 2), max(0, (width - box_width) // 2))
    win.keypad(True)
    win.erase()
    win.attrset(color("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    win.addstr(0, 2, " New Role Name "[: box_width - 4], color("popup.title", None))
    win.noutrefresh()
    curses.doupdate()

    name = _edit_text(win, 1, 2, box_width - 4, "", color)
    if not name:
        return
    if name in theme["roles"]:
        _draw_message(stdscr, f"Role already exists: {name}", color)
        return

    result = color_picker.pick_color(stdscr, f"Role: {name}", "white", "black", False, color)
    if result is None:
        return
    fg, bg, bold = result
    roles_core.create_role(theme, name, fg, bg, bold)
    store.save_theme(data_dir, theme)


def open_role_editor(
    stdscr,
    data_dir: Path,
    theme_id: str,
    color_fn: Optional[ColorFn] = None,
) -> None:
    """List the active theme's roles; edit one, or create a new role via 'n'."""
    color = color_fn or _default_color_fn
    index = 0
    while True:
        theme = store.get_theme(data_dir, theme_id)
        if theme is None:
            return
        names = sorted(theme["roles"].keys())
        index = min(index, len(names) - 1) if names else 0
        _draw_role_list(stdscr, theme_id, names, index, color)
        key = stdscr.getch()
        if key == curses.KEY_UP:
            index = max(0, index - 1)
        elif key == curses.KEY_DOWN:
            index = min(len(names) - 1, index + 1)
        elif key in (curses.KEY_ENTER, 10, 13) and names:
            _edit_existing_role(stdscr, data_dir, theme, names[index], color)
        elif key in (ord("n"), ord("N")):
            _create_new_role(stdscr, data_dir, theme, color)
        elif key == 27:
            return
