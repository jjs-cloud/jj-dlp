"""Log tab: scrollback with freeze/unfreeze and a per-tag filter popup."""

from __future__ import annotations

import curses
import re
import textwrap
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.notify import log_buffer
from jj_dlp.frontends.curses.tabs.framework import Tab

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ERROR_LEVELS = ("ERROR", "CRITICAL")


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _sanitize(text: str) -> str:
    """Strip control characters so a line can't corrupt the curses display."""
    return _CONTROL_RE.sub("", text.replace("\t", "    "))


class LogTab(Tab):
    """Combined activity-log scrollback with freeze and a per-tag filter."""

    title = "Log"

    def __init__(self, color_fn: Optional[ColorFn] = None) -> None:
        self.color = color_fn or _default_color_fn
        self.frozen = False
        self.scroll_offset = 0  # rows scrolled up from the bottom
        self._stdscr = None
        self._visible_height = 0

    def _visual_lines(self, width: int) -> List[Tuple[str, bool]]:
        """Build wrapped, sanitized (text, is_error) rows from the same lines written to debug.log."""
        rows: List[Tuple[str, bool]] = []
        width = max(1, width)
        for line, _tag, levelname in log_buffer.get_recent_lines():
            is_error = levelname in _ERROR_LEVELS
            line = _sanitize(line)
            for wrapped in textwrap.wrap(line, width) or [""]:
                rows.append((wrapped, is_error))
        return rows

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the visible slice of scrollback, honoring freeze and scroll offset."""
        self._stdscr = stdscr
        width = x2 - x1 + 1
        height = y2 - y1 + 1
        self._visible_height = max(0, height)
        if width <= 0 or height <= 0:
            return

        rows = self._visual_lines(width)
        max_offset = max(0, len(rows) - height)
        if not self.frozen:
            self.scroll_offset = 0
        self.scroll_offset = min(self.scroll_offset, max_offset)

        end = len(rows) - self.scroll_offset
        start = max(0, end - height)
        visible = rows[start:end]

        default_attr = self.color("log.line_default", None)
        error_attr = self.color("log.line_error", None)
        row = y1
        for text, is_error in visible:
            try:
                stdscr.addstr(row, x1, text[:width], error_attr if is_error else default_attr)
            except curses.error:
                pass
            row += 1

    def handle_key(self, key: int) -> bool:
        """Freeze toggle, scroll keys, and opening the tag-filter popup."""
        if key in (ord("f"), ord("F")):
            self.frozen = not self.frozen
            if not self.frozen:
                self.scroll_offset = 0
            return True
        if key == curses.KEY_UP:
            self.frozen = True
            self.scroll_offset += 1
            return True
        if key == curses.KEY_DOWN:
            self.scroll_offset = max(0, self.scroll_offset - 1)
            return True
        if key == curses.KEY_PPAGE:
            self.frozen = True
            self.scroll_offset += max(1, self._visible_height)
            return True
        if key == curses.KEY_NPAGE:
            self.scroll_offset = max(0, self.scroll_offset - max(1, self._visible_height))
            return True
        if key in (ord("t"), ord("T")):
            if self._stdscr is not None:
                _open_tag_filter_popup(self._stdscr, self.color)
            return True
        return False

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Keybind hints, showing the scroll hint only while scrollback is frozen."""
        hints = [("f", "unfreeze" if self.frozen else "freeze"), ("t", "filter tags")]
        if self.frozen:
            hints.append(("↑/↓/pgup/pgdn", "scroll"))
        return hints


class _TagFilterPopup:
    """Checkbox list of known log tags, toggled on/off directly in notify/log_buffer.py."""

    def __init__(self, tags: List[str], color_fn: ColorFn) -> None:
        self.tags = tags
        self.color = color_fn
        self.index = 0

    def _body_lines(self) -> List[str]:
        """Return one checkbox row per known tag, or a placeholder if there are none."""
        if not self.tags:
            return ["(no tags yet)"]
        return [f"[{'x' if log_buffer.is_tag_enabled(t) else ' '}] {t}" for t in self.tags]

    def draw(self, stdscr) -> None:
        """Render the checkbox list centered on the screen."""
        height, width = stdscr.getmaxyx()
        title = "Filter log tags (space: toggle, enter/esc: close)"
        body = self._body_lines()
        lines = [title] + body
        box_width = max(4, min(width - 2, max(len(line) for line in lines) + 4))
        box_height = max(3, min(height - 2, len(lines) + 2))
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.erase()
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        win.addstr(1, 2, title[: box_width - 4], self.color("popup.title", None))
        for i, line in enumerate(body):
            row = 2 + i
            if row >= box_height - 1:
                break
            selected = bool(self.tags) and i == self.index
            attr = self.color("popup.button_focused", None) if selected else self.color("popup.title", None)
            win.addstr(row, 2, line[: box_width - 4], attr)
        win.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int) -> bool:
        """Apply one keypress. Returns True once the popup should close."""
        if key in (27, ord("q"), curses.KEY_ENTER, 10, 13):
            return True
        if not self.tags:
            return False
        if key == curses.KEY_UP:
            self.index = (self.index - 1) % len(self.tags)
        elif key == curses.KEY_DOWN:
            self.index = (self.index + 1) % len(self.tags)
        elif key == ord(" "):
            tag = self.tags[self.index]
            log_buffer.set_tag_enabled(tag, not log_buffer.is_tag_enabled(tag))
        return False


def _open_tag_filter_popup(stdscr, color_fn: ColorFn) -> None:
    """Run the tag-filter popup's own input loop until the user closes it."""
    popup = _TagFilterPopup(log_buffer.get_known_tags(), color_fn)
    popup.draw(stdscr)
    while True:
        key = stdscr.getch()
        if popup.handle_key(key):
            return
        popup.draw(stdscr)
