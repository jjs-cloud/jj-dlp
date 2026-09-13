"""Christmas Day dashboard decoration."""

from __future__ import annotations

import curses
from datetime import date
from typing import Callable, Optional

DECORATION = "*  \u2744  Merry Christmas  \u2744  *"


def is_christmas(today: Optional[date] = None) -> bool:
    """Return True if today (local date) is December 25."""
    today = today or date.today()
    return today.month == 12 and today.day == 25


def draw_dashboard_decoration(stdscr, y1: int, x1: int, y2: int, x2: int, color_fn: Callable[[str], int]) -> None:
    """Overlay a small festive decoration on the dashboard's top border, on Christmas Day only."""
    if not is_christmas():
        return
    if y1 > y2:
        return
    width = x2 - x1 + 1
    if width <= len(DECORATION):
        return
    col = x1 + (width - len(DECORATION)) // 2
    try:
        stdscr.addstr(y1, col, DECORATION, color_fn("dashboard.system_panel.border"))
    except curses.error:
        pass
