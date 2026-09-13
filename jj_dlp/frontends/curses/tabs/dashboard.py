"""Dashboard tab: system panel, site panels, graph widget."""

from __future__ import annotations

import curses
import time
from typing import Callable, Dict, List, Optional, Tuple

from jj_dlp.core.engine import disk as disk_engine
from jj_dlp.core.engine.site_state import SiteState
from jj_dlp.frontends.curses.tabs.framework import Tab
from jj_dlp.frontends.curses.widgets import graph_widget

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _format_bytes(n: float) -> str:
    """Format a byte count as a short human-readable string (B/KB/MB/GB/TB)."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _format_rate(bytes_per_sec: float) -> str:
    """Format a bytes/sec rate as a short human-readable string with a /s suffix."""
    return f"{_format_bytes(bytes_per_sec)}/s"


def _format_uptime(seconds: float) -> str:
    """Format an elapsed-seconds duration as [D d ]HH:MM:SS."""
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class DashboardTab(Tab):
    """System panel (uptime, disk, graph) plus per-site streamer panels."""

    title = "Dashboard"

    def __init__(
        self,
        start_time: Optional[float] = None,
        disk_sampler: Optional[disk_engine.DiskSampler] = None,
        site_states: Optional[Dict[str, SiteState]] = None,
        color_fn: Optional[ColorFn] = None,
    ) -> None:
        self.start_time = start_time if start_time is not None else time.time()
        self.disk_sampler = disk_sampler
        self.site_states = site_states or {}
        self.color = color_fn or _default_color_fn

    def _system_panel_lines(self) -> List[str]:
        """Build the system panel's text lines: uptime plus per-drive free/used space."""
        uptime = _format_uptime(time.time() - self.start_time)
        lines = [f"Uptime: {uptime}"]
        if self.disk_sampler is None:
            return lines

        drive_parts = []
        for drive in self.disk_sampler.drives():
            usage = disk_engine.get_usage(drive)
            if usage is None:
                continue
            free, total = usage
            drive_parts.append(f"{drive} {_format_bytes(total - free)}/{_format_bytes(total)} used")
        if drive_parts:
            lines.append("  ".join(drive_parts))
        lines.append(f"Write rate: {_format_rate(self.disk_sampler.current_rate())}")
        return lines

    def _draw_system_panel(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> int:
        """Draw the top system strip (uptime, disk usage, graph); return the next free row."""
        width = x2 - x1 + 1
        if width <= 0 or y1 > y2:
            return y1

        border_attr = self.color("dashboard.system_panel.border", None)
        text_attr = self.color("dashboard.system_panel.text", None)

        row = y1
        for line in self._system_panel_lines():
            if row > y2:
                return row
            try:
                stdscr.addstr(row, x1, line[:width], text_attr)
            except curses.error:
                pass
            row += 1

        if self.disk_sampler is not None and row <= y2:
            graph_widget.draw_sparkline(stdscr, row, x1, x2, self.disk_sampler.history(), self.color)
            row += 1

        if row <= y2:
            try:
                stdscr.hline(row, x1, curses.ACS_HLINE, width, border_attr)
            except curses.error:
                pass
            row += 1

        return row

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the system panel. Site panels are added in Step 8.2."""
        self._draw_system_panel(stdscr, y1, x1, y2, x2)

    def handle_key(self, key: int) -> bool:
        """No keybinds yet; site panel scrolling arrives in Step 8.3."""
        return False

    def footer_hints(self) -> List[Tuple[str, str]]:
        """No tab-specific keybinds yet."""
        return []
