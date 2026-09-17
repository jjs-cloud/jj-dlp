"""Site/action picker popups backing the management overlay (Doc 1 §17)."""

from __future__ import annotations

import curses
from typing import Callable, List, Optional, Tuple

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _choose(stdscr, title: str, options: List[str], color_fn: Optional[ColorFn] = None) -> Optional[str]:
    """Run a small bordered single-select list popup; returns the chosen option or None."""
    color = color_fn or _default_color_fn
    index = 0
    while True:
        height, width = stdscr.getmaxyx()
        box_width = max(20, min(width - 2, max(len(o) for o in options + [title]) + 4))
        box_height = min(height - 2, len(options) + 4)
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.erase()
        win.attrset(color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        try:
            win.addstr(0, 2, f" {title} "[: box_width - 4], color("popup.title", None))
            for i, option in enumerate(options):
                row = 2 + i
                if row >= box_height - 1:
                    break
                attr = color("popup.button_focused", None) if i == index else curses.A_NORMAL
                win.addstr(row, 2, option[: box_width - 4], attr)
        except curses.error:
            pass
        win.noutrefresh()
        curses.doupdate()

        key = stdscr.getch()
        if key == curses.KEY_UP:
            index = (index - 1) % len(options)
        elif key == curses.KEY_DOWN:
            index = (index + 1) % len(options)
        elif key in (curses.KEY_ENTER, 10, 13):
            return options[index]
        elif key == 27:
            return None


def choose_site(stdscr, labels: List[str], color_fn: Optional[ColorFn] = None) -> Optional[str]:
    """Let the user pick which loaded site to manage; skips the popup if there's only one."""
    if not labels:
        return None
    if len(labels) == 1:
        return labels[0]
    return _choose(stdscr, "Manage which site?", labels, color_fn)


_ACTIONS = ["Add streamer", "Remove streamer", "Enable/disable streamer"]
_ACTION_IDS = {"Add streamer": "add", "Remove streamer": "remove", "Enable/disable streamer": "disable"}


def choose_action(stdscr, color_fn: Optional[ColorFn] = None) -> Optional[str]:
    """Let the user pick a management action, returning 'add', 'remove', or 'disable'."""
    choice = _choose(stdscr, "Manage streamers", _ACTIONS, color_fn)
    return _ACTION_IDS.get(choice)
