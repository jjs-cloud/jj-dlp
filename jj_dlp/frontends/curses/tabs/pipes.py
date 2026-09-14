"""stdout/stderr pipe tabs per recording streamer."""

from __future__ import annotations

import curses
import re
import textwrap
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.notify import pipe_capture
from jj_dlp.frontends.curses.tabs.framework import Tab

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_STREAMS = ("stdout", "stderr")


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _sanitize(text: str) -> str:
    """Strip control characters so a line can't corrupt the curses display."""
    return _CONTROL_RE.sub("", text.replace("\t", "    "))


class PipesTab(Tab):
    """One sub-view per currently- or recently-recording streamer, stdout/stderr toggle."""

    title = "Pipes"

    def __init__(self, color_fn: Optional[ColorFn] = None) -> None:
        self.color = color_fn or _default_color_fn
        self.streamer_index = 0
        self.stream_index = 0  # index into _STREAMS
        self.frozen = False
        self.scroll_offset = 0
        self._visible_height = 0

    def _streamers(self) -> List[Tuple[str, str]]:
        """Return the current (site, streamer) pairs with any captured output."""
        return pipe_capture.get_streamers()

    def _current(self) -> Optional[Tuple[str, str]]:
        """Return the selected (site, streamer) pair, or None if there are none."""
        streamers = self._streamers()
        if not streamers:
            return None
        self.streamer_index %= len(streamers)
        return streamers[self.streamer_index]

    def _visual_lines(self, width: int) -> List[str]:
        """Build wrapped, sanitized rows for the selected streamer's active stream."""
        current = self._current()
        if current is None:
            return []
        site, streamer = current
        stream = _STREAMS[self.stream_index]
        rows: List[str] = []
        width = max(1, width)
        for raw in pipe_capture.get_lines(site, streamer, stream):
            for wrapped in textwrap.wrap(_sanitize(raw), width) or [""]:
                rows.append(wrapped)
        return rows

    def _header(self, width: int) -> str:
        """Build the sub-view selector line: streamer position and active stream."""
        streamers = self._streamers()
        if not streamers:
            return "(no recording activity yet)"[:width]
        site, streamer = streamers[self.streamer_index]
        stream = _STREAMS[self.stream_index]
        pos = f"{self.streamer_index + 1}/{len(streamers)}"
        return f"< {site}/{streamer} [{stream}] ({pos}) >"[:width]

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the header row plus the visible slice of the selected stream's lines."""
        width = x2 - x1 + 1
        height = y2 - y1 + 1
        if width <= 0 or height <= 0:
            return

        header_attr = self.color("tabs.bar.active", None)
        try:
            stdscr.addstr(y1, x1, self._header(width), header_attr)
        except curses.error:
            pass

        body_y1 = y1 + 1
        body_height = max(0, height - 1)
        self._visible_height = body_height
        if body_height <= 0:
            return

        rows = self._visual_lines(width)
        max_offset = max(0, len(rows) - body_height)
        if not self.frozen:
            self.scroll_offset = 0
        self.scroll_offset = min(self.scroll_offset, max_offset)

        end = len(rows) - self.scroll_offset
        start = max(0, end - body_height)
        visible = rows[start:end]

        stream = _STREAMS[self.stream_index]
        line_attr = self.color("log.line_error" if stream == "stderr" else "log.line_default", None)
        row = body_y1
        for text in visible:
            try:
                stdscr.addstr(row, x1, text[:width], line_attr)
            except curses.error:
                pass
            row += 1

    def handle_key(self, key: int) -> bool:
        """Switch streamer/stream, freeze toggle, and scroll keys."""
        streamers = self._streamers()
        if key == curses.KEY_LEFT:
            if streamers:
                self.streamer_index = (self.streamer_index - 1) % len(streamers)
                self.scroll_offset = 0
            return True
        if key == curses.KEY_RIGHT:
            if streamers:
                self.streamer_index = (self.streamer_index + 1) % len(streamers)
                self.scroll_offset = 0
            return True
        if key in (ord("v"), ord("V")):
            self.stream_index = (self.stream_index + 1) % len(_STREAMS)
            self.scroll_offset = 0
            return True
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
        return False

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Keybind hints, showing the scroll hint only while scrollback is frozen."""
        hints = [
            ("\u2190/\u2192", "streamer"),
            ("v", "stdout/stderr"),
            ("f", "unfreeze" if self.frozen else "freeze"),
        ]
        if self.frozen:
            hints.append(("\u2191/\u2193/pgup/pgdn", "scroll"))
        return hints
