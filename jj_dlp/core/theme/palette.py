"""RGB/OSC4 terminal palette application."""

from __future__ import annotations

import os
import sys
from typing import Dict, Optional

# Standard ANSI base-16 index for each portable curses color name.
_COLOR_NAME_TO_INDEX: Dict[str, int] = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}

_RESET_SEQUENCE = "\x1b]104\x07"


def supports_true_color() -> bool:
    """Detect true-color (24-bit) terminal support from environment variables."""
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return True
    return "direct" in os.environ.get("TERM", "").lower()


def _hex_to_rgb_triplet(hex_color: str) -> Optional[str]:
    """Convert '#rrggbb' to an OSC 4 'rr/gg/bb' hex triplet, or None if invalid."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return None
    try:
        int(hex_color, 16)
    except ValueError:
        return None
    return f"{hex_color[0:2]}/{hex_color[2:4]}/{hex_color[4:6]}"


def _set_index(index: int, hex_color: str) -> None:
    """Emit an OSC 4 escape sequence redefining one base color index."""
    triplet = _hex_to_rgb_triplet(hex_color)
    if triplet is None:
        return
    sys.stdout.write(f"\x1b]4;{index};rgb:{triplet}\x1b\\")


def apply_theme_palette(theme: dict) -> None:
    """Redefine each role's base color index to its theme's true-color hex value."""
    if not supports_true_color():
        return
    roles = theme.get("roles", {})
    rgb_colors = theme.get("rgb_mode_colors", {})
    for role_name, rgb in rgb_colors.items():
        role = roles.get(role_name)
        if not role:
            continue
        fg_index = _COLOR_NAME_TO_INDEX.get(role.get("fg"))
        bg_index = _COLOR_NAME_TO_INDEX.get(role.get("bg"))
        if fg_index is not None and rgb.get("fg"):
            _set_index(fg_index, rgb["fg"])
        if bg_index is not None and rgb.get("bg"):
            _set_index(bg_index, rgb["bg"])
    sys.stdout.flush()


def reset_palette() -> None:
    """Restore the terminal's original base color palette via OSC 104."""
    sys.stdout.write(_RESET_SEQUENCE)
    sys.stdout.flush()
