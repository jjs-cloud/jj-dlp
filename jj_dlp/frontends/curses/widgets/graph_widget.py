"""Draw-only sparkline widget."""

from __future__ import annotations

import curses
from typing import Callable, List, Optional, Sequence, Tuple

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_GLYPHS = "▁▂▃▄▅▆▇█"


def _glyph_for(value: float, max_value: float) -> str:
    """Map a value to one of the block-height glyphs, scaled against max_value."""
    if max_value <= 0:
        return _GLYPHS[0]
    ratio = max(0.0, min(1.0, value / max_value))
    index = min(len(_GLYPHS) - 1, int(round(ratio * (len(_GLYPHS) - 1))))
    return _GLYPHS[index]


def draw_sparkline(
    stdscr,
    y: int,
    x1: int,
    x2: int,
    values: Sequence[float],
    color_fn: ColorFn,
    low_role: str = "dashboard.graph.bar_low",
    high_role: str = "dashboard.graph.bar_high",
) -> None:
    """Draw a one-row sparkline of `values` between columns x1 and x2 on row y."""
    width = x2 - x1 + 1
    if width <= 0:
        return

    visible = list(values[-width:]) if len(values) > width else list(values)
    max_value = max(visible) if visible else 0.0
    low_attr = color_fn(low_role, None)
    high_attr = color_fn(high_role, None)

    col = x1 + (width - len(visible))
    for value in visible:
        glyph = _glyph_for(value, max_value)
        ratio = 0.0 if max_value <= 0 else value / max_value
        attr = high_attr if ratio >= 0.5 else low_attr
        try:
            stdscr.addstr(y, col, glyph, attr)
        except curses.error:
            pass
        col += 1
