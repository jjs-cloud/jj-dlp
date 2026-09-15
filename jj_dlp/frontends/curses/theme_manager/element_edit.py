"""Element override list and edit screen (Doc 1 §19)."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.theme import elements as elements_core
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


def _choose_role(stdscr, theme: dict, current: Optional[str], color: ColorFn) -> Optional[str]:
    """List the theme's roles and let the user pick one. None if canceled."""
    names = sorted(theme["roles"].keys())
    if not names:
        _draw_message(stdscr, "This theme has no roles yet.", color)
        return None
    index = names.index(current) if current in names else 0
    height, width = stdscr.getmaxyx()
    title = "Point at role"
    hint = "\u2191/\u2193 select  Enter choose  Esc cancel"

    while True:
        box_width = min(width - 2, max(len(n) for n in names + [title, hint]) + 4)
        box_height = min(height - 2, len(names) + 5)
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)
        win = curses.newwin(box_height, box_width, y1, x1)
        win.keypad(True)
        win.erase()
        win.attrset(color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        win.addstr(0, 2, f" {title} "[: box_width - 4], color("popup.title", None))
        for i, name in enumerate(names):
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

        key = win.getch()
        if key == curses.KEY_UP:
            index = max(0, index - 1)
        elif key == curses.KEY_DOWN:
            index = min(len(names) - 1, index + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            return names[index]
        elif key == 27:
            return None


def _entry_summary(theme: dict, element_id: str) -> str:
    """One-line description of an element's current role or color override."""
    entry = theme.get("elements", {}).get(element_id)
    if entry:
        color = entry.get("color")
        if color:
            return f"color: {color['fg']}/{color['bg']}"
        role_name = entry.get("role")
        if role_name:
            return f"role: {role_name}"
    try:
        return f"role: {elements_core.get_default_role(element_id)} (default)"
    except KeyError:
        return "(unset)"


def _draw_element_list(
    stdscr, theme_id: str, element_ids: List[str], index: int, theme: dict, color: ColorFn
) -> None:
    """Render the centered, scrollable list of themeable elements."""
    height, width = stdscr.getmaxyx()
    title = f"Elements \u2014 {theme_id}"
    hint = "\u2191/\u2193 select  Enter assign  Esc back"
    rows = [f"{eid}  [{_entry_summary(theme, eid)}]" for eid in element_ids]

    box_width = min(width - 2, max((len(r) for r in rows + [title, hint]), default=20) + 4)
    box_height = min(height - 2, len(rows) + 5)
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)
    visible_rows = max(1, box_height - 4)
    top = max(0, min(index - visible_rows // 2, max(0, len(rows) - visible_rows)))

    win = curses.newwin(box_height, box_width, y1, x1)
    win.erase()
    win.attrset(color("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    win.addstr(0, 2, f" {title} "[: box_width - 4], color("popup.title", None))
    for row_i, i in enumerate(range(top, min(top + visible_rows, len(rows)))):
        row = 1 + row_i
        attr = color("popup.button_focused", None) if i == index else curses.A_NORMAL
        try:
            win.addstr(row, 2, rows[i][: box_width - 4], attr)
        except curses.error:
            pass
    try:
        win.addstr(box_height - 2, 2, hint[: box_width - 4], curses.A_DIM)
    except curses.error:
        pass
    win.noutrefresh()
    curses.doupdate()


def _choose_assign_mode(stdscr, color: ColorFn) -> Optional[str]:
    """Ask whether to point at a role or set a direct color. Returns 'role', 'color', or None."""
    options = [("role", "Point at a role"), ("color", "Direct color override")]
    index = 0
    height, width = stdscr.getmaxyx()
    labels = [label for _, label in options]
    box_width = min(width - 2, max(len(l) for l in labels) + 4)
    box_height = len(options) + 4
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)
    win = curses.newwin(box_height, box_width, y1, x1)
    win.keypad(True)

    while True:
        win.erase()
        win.attrset(color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        for i, (_, label) in enumerate(options):
            attr = color("popup.button_focused", None) if i == index else curses.A_NORMAL
            try:
                win.addstr(1 + i, 2, label[: box_width - 4], attr)
            except curses.error:
                pass
        win.noutrefresh()
        curses.doupdate()

        key = win.getch()
        if key == curses.KEY_UP:
            index = max(0, index - 1)
        elif key == curses.KEY_DOWN:
            index = min(len(options) - 1, index + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            return options[index][0]
        elif key == 27:
            return None


def _assign_element(stdscr, data_dir: Path, theme: dict, element_id: str, color: ColorFn) -> None:
    """Point the given element at a role, or give it a direct color override."""
    mode = _choose_assign_mode(stdscr, color)
    if mode is None:
        return
    entry = theme.setdefault("elements", {}).get(element_id, {})

    if mode == "role":
        chosen = _choose_role(stdscr, theme, entry.get("role"), color)
        if chosen is None:
            return
        theme["elements"][element_id] = {"role": chosen}
    else:
        current = entry.get("color") or {"fg": "white", "bg": "black", "bold": False}
        result = color_picker.pick_color(
            stdscr, f"Element: {element_id}", current["fg"], current["bg"], current.get("bold", False), color
        )
        if result is None:
            return
        fg, bg, bold = result
        theme["elements"][element_id] = {"color": {"fg": fg, "bg": bg, "bold": bold}}

    store.save_theme(data_dir, theme)


def open_element_editor(
    stdscr,
    data_dir: Path,
    theme_id: str,
    color_fn: Optional[ColorFn] = None,
) -> None:
    """List every registered element; assign the selected one a role or a direct color."""
    color = color_fn or _default_color_fn
    element_ids = elements_core.get_elements()
    index = 0
    while True:
        theme = store.get_theme(data_dir, theme_id)
        if theme is None:
            return
        index = min(index, len(element_ids) - 1) if element_ids else 0
        _draw_element_list(stdscr, theme_id, element_ids, index, theme, color)
        key = stdscr.getch()
        if key == curses.KEY_UP:
            index = max(0, index - 1)
        elif key == curses.KEY_DOWN:
            index = min(len(element_ids) - 1, index + 1)
        elif key in (curses.KEY_ENTER, 10, 13) and element_ids:
            _assign_element(stdscr, data_dir, theme, element_ids[index], color)
        elif key == 27:
            return
