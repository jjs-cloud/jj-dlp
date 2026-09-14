"""Priority tab: cross-site streamer ordering, reordering, and bypass toggle."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.config import priority as priority_config
from jj_dlp.frontends.curses.tabs.framework import Tab

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _has_override(entry: dict) -> bool:
    """True if any of the entry's seven override fields is set (non-null)."""
    return any(value is not None for value in entry.get("overrides", {}).values())


class PriorityTab(Tab):
    """Renders get_order()'s list with up/down reorder, bypass toggle, and override marker."""

    title = "Priority"

    def __init__(self, data_dir: Path, color_fn: Optional[ColorFn] = None) -> None:
        self.data_dir = Path(data_dir)
        self.color = color_fn or _default_color_fn
        self.index = 0
        self.scroll = 0
        self._page_size = 1
        self.entries: List[dict] = []
        self.reload()

    def reload(self) -> None:
        """Reload the ordered entry list from config/priority.json."""
        self.entries = priority_config.get_order(self.data_dir)
        self.index = min(self.index, max(0, len(self.entries) - 1))

    def _selected(self) -> Optional[dict]:
        """Return the currently-selected entry, or None if the list is empty."""
        if not self.entries:
            return None
        return self.entries[self.index]

    def _swap_and_save(self, other_index: int) -> None:
        """Swap the selected entry with the one at other_index, then persist the new order."""
        if not (0 <= other_index < len(self.entries)):
            return
        self.entries[self.index], self.entries[other_index] = self.entries[other_index], self.entries[self.index]
        priority_config.set_order(self.data_dir, self.entries)
        self.index = other_index
        self.reload()

    def _toggle_bypass(self) -> None:
        """Flip the bypass flag for the selected entry and persist it."""
        entry = self._selected()
        if entry is None:
            return
        priority_config.set_bypass(
            self.data_dir, entry["site"], entry["streamer"], not entry.get("bypass", False)
        )
        self.reload()

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw one row per priority entry: override marker, bypass marker, site/streamer."""
        width = x2 - x1 + 1
        height = y2 - y1 + 1
        if width <= 0 or height <= 0:
            return
        self._page_size = max(1, height)

        if not self.entries:
            try:
                stdscr.addstr(y1, x1, "(no streamers configured)"[:width], self.color("priority.row_normal", None))
            except curses.error:
                pass
            return

        if self.index < self.scroll:
            self.scroll = self.index
        elif self.index >= self.scroll + height:
            self.scroll = self.index - height + 1

        row = y1
        for i in range(self.scroll, min(len(self.entries), self.scroll + height)):
            entry = self.entries[i]
            bypass = bool(entry.get("bypass"))
            overridden = _has_override(entry)
            marker = "*" if overridden else " "
            bullet = "B" if bypass else " "
            text = f"{bullet} {entry['site']} / {entry['streamer']}"[: max(0, width - 1)]

            if i == self.index:
                row_attr = self.color("popup.button_focused", None)
                marker_attr = row_attr
            else:
                row_attr = self.color("priority.row_bypass" if bypass else "priority.row_normal", None)
                marker_attr = self.color("priority.override_marker", None) if overridden else row_attr

            try:
                stdscr.addstr(row, x1, marker, marker_attr)
                stdscr.addstr(row, x1 + 1, text, row_attr)
            except curses.error:
                pass
            row += 1

    def handle_key(self, key: int) -> bool:
        """Move the selection, reorder with K/J, and toggle bypass with b."""
        if not self.entries:
            return False
        if key == curses.KEY_UP:
            self.index = max(0, self.index - 1)
            return True
        if key == curses.KEY_DOWN:
            self.index = min(len(self.entries) - 1, self.index + 1)
            return True
        if key == curses.KEY_PPAGE:
            self.index = max(0, self.index - self._page_size)
            return True
        if key == curses.KEY_NPAGE:
            self.index = min(len(self.entries) - 1, self.index + self._page_size)
            return True
        if key == ord("K"):
            self._swap_and_save(self.index - 1)
            return True
        if key == ord("J"):
            self._swap_and_save(self.index + 1)
            return True
        if key == ord("b"):
            self._toggle_bypass()
            return True
        return False

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Navigation, reorder, and bypass-toggle hints."""
        return [("↑/↓", "move"), ("K/J", "reorder"), ("b", "toggle bypass")]
