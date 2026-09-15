"""File Manager tab: grouped/collapsible file list, sortable, scan-driven."""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from jj_dlp.core.engine import file_scan
from jj_dlp.core.engine.site_state import SiteState
from jj_dlp.frontends.curses.tabs.framework import Tab

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_SORT_MODES = ("name", "size", "modified", "natural")
_EXPANDED_GLYPH = "▾"
_COLLAPSED_GLYPH = "▸"

# Minimum seconds between filesystem rescans; avoids re-walking every draw tick.
DEFAULT_RESCAN_INTERVAL = 3.0


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _format_size(n: int) -> str:
    """Format a byte count as a short human-readable string."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _format_mtime(ts: float) -> str:
    """Format a modified-time epoch as YYYY-MM-DD HH:MM."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _sort_key(row: file_scan.FileRow, mode: str):
    """Return the key used to order rows within a group for the given sort mode."""
    if mode == "name":
        return Path(row.path).name.lower()
    if mode == "size":
        return -row.size
    if mode == "modified":
        return -row.modified
    return row.sort_key


@dataclass(frozen=True)
class FileManagerSiteSource:
    """One loaded site's config plus its runtime SiteState, for file scanning."""

    label: str
    config: dict
    site_state: SiteState


@dataclass(frozen=True)
class _DisplayRow:
    """One flattened, drawable row: either a folder header or a file entry."""

    kind: str  # "folder" or "file"
    group_key: str
    file_row: Optional[file_scan.FileRow] = None
    count: int = 0
    total_size: int = 0


class FileManagerTab(Tab):
    """Grouped, collapsible, sortable listing of every loaded site's output files."""

    title = "Files"

    def __init__(
        self,
        sites: Optional[List[FileManagerSiteSource]] = None,
        subfolders_mode: str = "streamer-only",
        collapsible_folders: bool = True,
        rescan_interval: float = DEFAULT_RESCAN_INTERVAL,
        color_fn: Optional[ColorFn] = None,
    ) -> None:
        self.sites = sites or []
        self.subfolders_mode = subfolders_mode
        self.collapsible = collapsible_folders
        self.rescan_interval = rescan_interval
        self.color = color_fn or _default_color_fn

        self.sort_mode_index = 0
        self.scroll_offset = 0

        self._groups: Dict[str, List[file_scan.FileRow]] = {}
        self._expanded: Dict[str, bool] = {}
        self._last_scan = 0.0

    @property
    def sort_mode(self) -> str:
        """Return the currently active sort mode name."""
        return _SORT_MODES[self.sort_mode_index]

    def _rescan(self, force: bool = False) -> None:
        """Re-walk the output directories if the rescan interval has elapsed."""
        now = time.time()
        if not force and self._groups and now - self._last_scan < self.rescan_interval:
            return
        sites_cfg = {s.label: s.config for s in self.sites}
        site_states = {s.label: s.site_state for s in self.sites}
        rows = file_scan.scan(sites_cfg, site_states, self.subfolders_mode, self.collapsible)
        groups: Dict[str, List[file_scan.FileRow]] = {}
        for row in rows:
            groups.setdefault(row.group_key, []).append(row)
        self._groups = groups
        for key in groups:
            self._expanded.setdefault(key, True)
        self._last_scan = now

    def _flatten(self) -> List[_DisplayRow]:
        """Build the ordered, sort/collapse-aware list of rows to draw."""
        display: List[_DisplayRow] = []
        for group_key in sorted(self._groups):
            rows = self._groups[group_key]
            expanded = self._expanded.get(group_key, True) or not self.collapsible
            display.append(
                _DisplayRow(
                    kind="folder",
                    group_key=group_key,
                    count=len(rows),
                    total_size=sum(r.size for r in rows),
                )
            )
            if not expanded:
                continue
            for row in sorted(rows, key=lambda r: _sort_key(r, self.sort_mode)):
                display.append(_DisplayRow(kind="file", group_key=group_key, file_row=row))
        return display

    def expand_all(self) -> None:
        """Mark every known folder group as expanded."""
        for key in self._groups:
            self._expanded[key] = True

    def collapse_all(self) -> None:
        """Mark every known folder group as collapsed (only when collapsible)."""
        if not self.collapsible:
            return
        for key in self._groups:
            self._expanded[key] = False

    def cycle_sort(self) -> None:
        """Advance to the next sort mode, wrapping around."""
        self.sort_mode_index = (self.sort_mode_index + 1) % len(_SORT_MODES)

    def _draw_folder_row(self, stdscr, y: int, x1: int, width: int, row: _DisplayRow) -> None:
        """Draw a single folder-header row."""
        is_expanded = self._expanded.get(row.group_key, True) or not self.collapsible
        prefix = f"{_EXPANDED_GLYPH if is_expanded else _COLLAPSED_GLYPH} " if self.collapsible else "  "
        text = f"{prefix}{row.group_key} ({row.count} files, {_format_size(row.total_size)})"
        attr = self.color("file_manager.folder_header", None)
        try:
            stdscr.addstr(y, x1, text[:width], attr)
        except curses.error:
            pass

    def _draw_file_row(self, stdscr, y: int, x1: int, width: int, row: file_scan.FileRow) -> None:
        """Draw a single file row, colored by writing/idle state."""
        name = Path(row.path).name
        marker = "W" if row.state == "WRITING" else " "
        text = f"    [{marker}] {name}  {_format_size(row.size):>8}  {_format_mtime(row.modified)}"
        role = "file_manager.row_writing" if row.state == "WRITING" else "file_manager.row_idle"
        attr = self.color(role, None)
        try:
            stdscr.addstr(y, x1, text[:width], attr)
        except curses.error:
            pass

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the visible slice of the grouped/sorted file list."""
        self._rescan()
        width = x2 - x1 + 1
        height = y2 - y1 + 1
        if width <= 0 or height <= 0:
            return

        display = self._flatten()
        max_offset = max(0, len(display) - height)
        self.scroll_offset = min(self.scroll_offset, max_offset)
        visible = display[self.scroll_offset : self.scroll_offset + height]

        row_y = y1
        for entry in visible:
            if entry.kind == "folder":
                self._draw_folder_row(stdscr, row_y, x1, width, entry)
            else:
                self._draw_file_row(stdscr, row_y, x1, width, entry.file_row)
            row_y += 1

    def handle_key(self, key: int) -> bool:
        """Handle sort-cycle and expand/collapse-all keys."""
        if key in (ord("s"), ord("S")):
            self.cycle_sort()
            return True
        if key == ord("e"):
            self.expand_all()
            return True
        if key == ord("c"):
            self.collapse_all()
            return True
        return False

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Keybind hints for sorting and expand/collapse-all."""
        return [
            ("s", f"sort: {self.sort_mode}"),
            ("e", "expand all"),
            ("c", "collapse all"),
        ]
