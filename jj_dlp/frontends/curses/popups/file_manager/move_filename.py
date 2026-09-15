"""Move-filename popup: adjust destination filename."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, Optional, Tuple

from jj_dlp.core.engine import file_ops

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def open_move_filename_popup(stdscr, file_row, destination_dir: str, color_fn: Optional[ColorFn] = None) -> Optional[str]:
    """Move straight to destination_dir under its original name; Step 13.8 adds filename editing."""
    color = color_fn or _default_color_fn
    try:
        moved_path = file_ops.move(file_row.path, destination_dir)
    except file_ops.FileOpError as exc:
        return f"Move failed: {exc}"
    return f"Moved -> {Path(moved_path).name}"
