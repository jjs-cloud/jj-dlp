"""Dashboard tab: system panel, site panels, graph widget."""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from jj_dlp.core.engine import disk as disk_engine
from jj_dlp.core.engine.site_state import SiteState, StreamerSnapshot
from jj_dlp.frontends.curses.tabs.framework import Tab
from jj_dlp.frontends.curses.widgets import graph_widget

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_STATE_GLYPHS = {"recording": "●", "live": "○", "offline": "–"}


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


def _row_state(snapshot: Optional[StreamerSnapshot]) -> str:
    """Classify a streamer row as recording, live (not recording), or offline."""
    if snapshot is None or not snapshot.live_since:
        return "offline"
    return "recording" if snapshot.recording else "live"


def _status_string(snapshot: Optional[StreamerSnapshot]) -> str:
    """Pick the most relevant short status word for a streamer row."""
    if snapshot is None:
        return ""
    if snapshot.write_failure:
        return "WRITE-FAIL"
    if snapshot.stall_since:
        return "STALLED"
    if snapshot.ad_alert_active:
        return "AD"
    if snapshot.recording:
        return "REC"
    if snapshot.live_since:
        return "LIVE"
    return ""


def _duration_bar(width: int, live_since: Optional[float], max_hours: float) -> str:
    """Build a fixed-width bar showing the current live session's progress toward max_hours."""
    if width <= 0:
        return ""
    if not live_since:
        return "·" * width
    elapsed = max(0.0, time.time() - live_since)
    cap_seconds = max(1.0, max_hours * 3600)
    fraction = min(1.0, elapsed / cap_seconds)
    filled = int(round(fraction * width))
    return "█" * filled + "·" * (width - filled)


def _draw_box(stdscr, y1: int, x1: int, y2: int, x2: int, title: str, border_attr: int, title_attr: int) -> None:
    """Draw a bordered box with a title in its top edge."""
    height = y2 - y1 + 1
    width = x2 - x1 + 1
    if height < 2 or width < 2:
        return
    try:
        stdscr.attrset(border_attr)
        stdscr.hline(y1, x1 + 1, curses.ACS_HLINE, width - 2)
        stdscr.hline(y2, x1 + 1, curses.ACS_HLINE, width - 2)
        stdscr.vline(y1 + 1, x1, curses.ACS_VLINE, max(0, height - 2))
        stdscr.vline(y1 + 1, x2, curses.ACS_VLINE, max(0, height - 2))
        stdscr.addch(y1, x1, curses.ACS_ULCORNER)
        stdscr.addch(y1, x2, curses.ACS_URCORNER)
        stdscr.addch(y2, x1, curses.ACS_LLCORNER)
        stdscr.addch(y2, x2, curses.ACS_LRCORNER)
        stdscr.attrset(curses.A_NORMAL)
    except curses.error:
        pass
    if title:
        label = f" {title} "[: max(0, width - 2)]
        try:
            stdscr.addstr(y1, x1 + 1, label, title_attr)
        except curses.error:
            pass


@dataclass(frozen=True)
class SitePanelSource:
    """One loaded site's config plus its runtime SiteState, for panel rendering."""

    label: str
    config: dict
    site_state: SiteState


class DashboardTab(Tab):
    """System panel (uptime, disk, graph) plus per-site streamer panels."""

    title = "Dashboard"

    def __init__(
        self,
        start_time: Optional[float] = None,
        disk_sampler: Optional[disk_engine.DiskSampler] = None,
        sites: Optional[List[SitePanelSource]] = None,
        color_fn: Optional[ColorFn] = None,
    ) -> None:
        self.start_time = start_time if start_time is not None else time.time()
        self.disk_sampler = disk_sampler
        self.sites = sorted(sites or [], key=lambda s: (s.config.get("order", 0), s.label))
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

    def _draw_site_panel(self, stdscr, source: SitePanelSource, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw one site's bordered box: title plus one row per streamer."""
        border_attr = self.color("dashboard.site_panel.border", None)
        title_attr = self.color("dashboard.site_panel.title", None)
        _draw_box(stdscr, y1, x1, y2, x2, source.label, border_attr, title_attr)

        inner_x1, inner_x2 = x1 + 1, x2 - 1
        inner_width = inner_x2 - inner_x1 + 1
        if inner_width <= 0:
            return

        display_cfg = source.config.get("display", {})
        max_hours = float(display_cfg.get("progress_bar_max_hours", 10))
        name_width = 16
        bar_width = max(0, min(int(display_cfg.get("progress_bar_width", 40)), inner_width - name_width - 14))

        snapshot = source.site_state.snapshot()
        row = y1 + 1
        for name in source.config.get("streamers", []):
            if row >= y2:
                break
            stream_snap = snapshot.streamers.get(name)
            state = _row_state(stream_snap)
            glyph = _STATE_GLYPHS[state]
            row_attr = self.color(f"dashboard.streamer.row_{state}", None)
            dot_attr = self.color("dashboard.streamer.recording_dot", None) if state == "recording" else row_attr

            live_since = stream_snap.live_since if stream_snap else None
            bar = _duration_bar(bar_width, live_since, max_hours)
            status = _status_string(stream_snap)
            rest = f" {name:<{name_width}.{name_width}} {bar} {status}"[: inner_width - 1]
            try:
                stdscr.addstr(row, inner_x1, glyph, dot_attr)
                stdscr.addstr(row, inner_x1 + 1, rest, row_attr)
            except curses.error:
                pass
            row += 1

    def _draw_site_panels(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Stack every loaded site's panel vertically, clipped to the available rows."""
        row = y1
        for source in self.sites:
            if row > y2:
                break
            needed = len(source.config.get("streamers", [])) + 2
            panel_bottom = min(y2, row + needed - 1)
            if panel_bottom - row + 1 < 2:
                break
            self._draw_site_panel(stdscr, source, row, x1, panel_bottom, x2)
            row = panel_bottom + 1

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the system panel, then every site's panel stacked below it."""
        next_row = self._draw_system_panel(stdscr, y1, x1, y2, x2)
        if next_row <= y2:
            self._draw_site_panels(stdscr, next_row, x1, y2, x2)

    def handle_key(self, key: int) -> bool:
        """No keybinds yet; site panel scrolling arrives in Step 8.3."""
        return False

    def footer_hints(self) -> List[Tuple[str, str]]:
        """No tab-specific keybinds yet."""
        return []
