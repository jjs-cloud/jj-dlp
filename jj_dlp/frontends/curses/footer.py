"""Footer/help bar rendering."""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from jj_dlp.frontends.curses.tabs.framework import TabBar

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

# Always-present global keybinds, shown after the active tab's own hints.
GLOBAL_HINTS: List[Tuple[str, str]] = [
    ("q", "quit"),
    ("tab", "switch tab"),
    ("h", "help"),
]


def build_hints(tab_bar: TabBar) -> List[Tuple[str, str]]:
    """Return the active tab's footer hints followed by the global hints."""
    return list(tab_bar.footer_hints()) + GLOBAL_HINTS


def draw_footer(stdscr, y: int, x1: int, x2: int, tab_bar: TabBar, color_fn: ColorFn) -> None:
    """Render the active tab's hints plus the global keys on row y, truncated to fit."""
    width = x2 - x1 + 1
    if width <= 0:
        return

    col = x1
    for key, label in build_hints(tab_bar):
        key_part = f"{key}:"
        text_part = f"{label}  "
        if col + len(key_part) > x2 + 1:
            break
        stdscr.addstr(y, col, key_part, color_fn("footer.hint_key"))
        col += len(key_part)

        remaining = x2 + 1 - col
        if remaining <= 0:
            break
        stdscr.addstr(y, col, text_part[:remaining], color_fn("footer.hint_text"))
        col += min(len(text_part), remaining)
