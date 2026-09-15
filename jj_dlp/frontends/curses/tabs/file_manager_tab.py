"""File Manager tab: grouped/collapsible file list, sortable, scan-driven."""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from jj_dlp.core.engine import file_ops, file_scan
from jj_dlp.core.engine.site_state import SiteState
from jj_dlp.core.notify import logger
from jj_dlp.frontends.curses.popups.file_manager.menu import open_file_menu
from jj_dlp.frontends.curses.tabs.framework import Tab

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

_SORT_MODES = ("name", "size", "modified", "natural")
_EXPANDED_GLYPH = "▾"
_COLLAPSED_GLYPH = "▸"
_DELETE_MODES = ("trash", "permanent")

# Minimum seconds between filesystem rescans; avoids re-walking every draw tick.
DEFAULT_RESCAN_INTERVAL = 3.0


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _confirm(stdscr, message: str, color_fn: ColorFn) -> bool:
    """Blocking yes/no confirmation box centered on the screen."""
    if stdscr is None:
        return False
    height, width = stdscr.getmaxyx()
    lines = [message, "(y/n)"]
    box_width = min(width - 2, max(len(line) for line in lines) + 4)
    box_height = len(lines) + 2
    y1 = max(0, (height - box_height) // 2)
    x1 = max(0, (width - box_width) // 2)

    win = curses.newwin(box_height, box_width, y1, x1)
    win.attrset(color_fn("popup.border", None))
    win.border()
    win.attrset(curses.A_NORMAL)
    for i, line in enumerate(lines):
        try:
            win.addstr(1 + i, 2, line[: box_width - 4], color_fn("file_manager.delete_confirm", None))
        except curses.error:
            pass
    win.noutrefresh()
    curses.doupdate()

    while True:
        key = stdscr.getch()
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27):
            return False


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
        self.index = 0
        self.delete_mode_index = 0
        self.status_message = ""

        self._groups: Dict[str, List[file_scan.FileRow]] = {}
        self._expanded: Dict[str, bool] = {}
        self._last_scan = 0.0
        self._display: List[_DisplayRow] = []
        self._page_size = 1

    @property
    def sort_mode(self) -> str:
        """Return the currently active sort mode name."""
        return _SORT_MODES[self.sort_mode_index]

    @property
    def delete_mode(self) -> str:
        """Return the currently active delete mode: 'trash' or 'permanent'."""
        return _DELETE_MODES[self.delete_mode_index]

    def toggle_delete_mode(self) -> None:
        """Switch delete mode between trash and permanent."""
        self.delete_mode_index = (self.delete_mode_index + 1) % len(_DELETE_MODES)

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

    def _refresh_display(self, force_scan: bool = False) -> None:
        """Rescan and reflatten, keeping the selection on the same file if it still exists."""
        selected_path = self._selected_row_path()
        self._rescan(force=force_scan)
        self._display = self._flatten()
        if selected_path is not None:
            for i, entry in enumerate(self._display):
                if entry.file_row is not None and entry.file_row.path == selected_path:
                    self.index = i
                    break
        self.index = max(0, min(self.index, len(self._display) - 1)) if self._display else 0

    def _selected_row_path(self) -> Optional[str]:
        """Return the path of the currently selected file row, if any."""
        entry = self._selected()
        return entry.file_row.path if entry is not None and entry.file_row is not None else None

    def _selected(self) -> Optional[_DisplayRow]:
        """Return the currently selected display row, or None if the list is empty."""
        if not self._display:
            return None
        return self._display[self.index]

    def selected_file(self) -> Optional[file_scan.FileRow]:
        """Return the currently selected FileRow, or None if a folder header is selected."""
        entry = self._selected()
        return entry.file_row if entry is not None else None

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

    def _draw_folder_row(
        self, stdscr, y: int, x1: int, width: int, row: _DisplayRow, selected: bool = False
    ) -> None:
        """Draw a single folder-header row."""
        is_expanded = self._expanded.get(row.group_key, True) or not self.collapsible
        prefix = f"{_EXPANDED_GLYPH if is_expanded else _COLLAPSED_GLYPH} " if self.collapsible else "  "
        text = f"{prefix}{row.group_key} ({row.count} files, {_format_size(row.total_size)})"
        attr = self.color("popup.button_focused", None) if selected else self.color("file_manager.folder_header", None)
        try:
            stdscr.addstr(y, x1, text[:width], attr)
        except curses.error:
            pass

    def _draw_file_row(
        self, stdscr, y: int, x1: int, width: int, row: file_scan.FileRow, selected: bool
    ) -> None:
        """Draw a single file row, colored by writing/idle state or selection."""
        name = Path(row.path).name
        marker = "W" if row.state == "WRITING" else " "
        text = f"    [{marker}] {name}  {_format_size(row.size):>8}  {_format_mtime(row.modified)}"
        if selected:
            attr = self.color("popup.button_focused", None)
        else:
            role = "file_manager.row_writing" if row.state == "WRITING" else "file_manager.row_idle"
            attr = self.color(role, None)
        try:
            stdscr.addstr(y, x1, text[:width], attr)
        except curses.error:
            pass

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the visible slice of the grouped/sorted file list, with the selection highlighted."""
        self._refresh_display()
        width = x2 - x1 + 1
        height = y2 - y1 + 1
        if width <= 0 or height <= 0:
            return
        self._page_size = max(1, height)

        show_status = bool(self.status_message) and height > 1
        list_height = height - 1 if show_status else height

        max_offset = max(0, len(self._display) - list_height)
        if self.index < self.scroll_offset:
            self.scroll_offset = self.index
        elif self.index >= self.scroll_offset + list_height:
            self.scroll_offset = self.index - list_height + 1
        self.scroll_offset = min(self.scroll_offset, max_offset)
        visible = self._display[self.scroll_offset : self.scroll_offset + list_height]

        row_y = y1
        for offset, entry in enumerate(visible):
            is_selected = self.scroll_offset + offset == self.index
            if entry.kind == "folder":
                self._draw_folder_row(stdscr, row_y, x1, width, entry, is_selected)
            else:
                self._draw_file_row(stdscr, row_y, x1, width, entry.file_row, is_selected)
            row_y += 1

        if show_status:
            try:
                stdscr.addstr(y2, x1, self.status_message[:width], self.color("file_manager.delete_confirm", None))
            except curses.error:
                pass

    def _set_status(self, msg: str) -> None:
        """Set the transient status/error line shown at the bottom of the tab body."""
        self.status_message = msg

    def _move(self, delta: int) -> None:
        """Move the selection by delta rows, clamped to the list bounds."""
        if not self._display:
            return
        self.index = max(0, min(len(self._display) - 1, self.index + delta))

    def _open_selected(self) -> None:
        """Open the selected file with the OS-native default application."""
        row = self.selected_file()
        if row is None:
            return
        try:
            file_ops.open_file(row.path)
            self._set_status("")
        except file_ops.FileOpError as exc:
            logger.dbg(str(exc), tag="file_manager")
            self._set_status(str(exc))

    def _open_selected_folder(self) -> None:
        """Open the OS file browser at the folder containing the selected file."""
        row = self.selected_file()
        if row is None:
            return
        try:
            file_ops.open_containing_folder(row.path)
            self._set_status("")
        except file_ops.FileOpError as exc:
            logger.dbg(str(exc), tag="file_manager")
            self._set_status(str(exc))

    def _delete_selected(self, stdscr) -> None:
        """Trash or permanently delete the selected file, per the active delete mode."""
        row = self.selected_file()
        if row is None:
            return
        name = Path(row.path).name
        verb = "Permanently delete" if self.delete_mode == "permanent" else "Move to trash"
        if not _confirm(stdscr, f"{verb}: {name}?", self.color):
            return
        try:
            if self.delete_mode == "permanent":
                file_ops.permanent_delete(row.path)
            else:
                file_ops.move_to_trash(row.path)
            self._set_status(f"Deleted {name}")
        except file_ops.FileOpError as exc:
            logger.dbg(str(exc), tag="file_manager")
            self._set_status(str(exc))
        self._refresh_display(force_scan=True)

    def _open_menu(self, stdscr) -> None:
        """Open the File Options menu for the selected file and apply its result."""
        row = self.selected_file()
        if row is None or stdscr is None:
            return
        result = open_file_menu(stdscr, row, self.color)
        if result:
            self._set_status(result)
        self._refresh_display(force_scan=True)

    def handle_key(self, key: int, stdscr=None) -> bool:
        """Selection movement, sort/expand-collapse, open/open-folder/delete, and the file menu."""
        if key in (ord("s"), ord("S")):
            self.cycle_sort()
            return True
        if key == ord("e"):
            self.expand_all()
            return True
        if key == ord("c"):
            self.collapse_all()
            return True
        if key == curses.KEY_UP:
            self._move(-1)
            return True
        if key == curses.KEY_DOWN:
            self._move(1)
            return True
        if key == curses.KEY_PPAGE:
            self._move(-self._page_size)
            return True
        if key == curses.KEY_NPAGE:
            self._move(self._page_size)
            return True
        if key in (curses.KEY_HOME, ord("g")):
            self.index = 0
            return True
        if key in (curses.KEY_END, ord("G")):
            self.index = max(0, len(self._display) - 1)
            return True
        if key == ord("o"):
            self._open_selected()
            return True
        if key == ord("O"):
            self._open_selected_folder()
            return True
        if key == ord("t"):
            self.toggle_delete_mode()
            return True
        if key in (ord("x"), curses.KEY_DC):
            self._delete_selected(stdscr)
            return True
        if key in (curses.KEY_ENTER, 10, 13):
            self._open_menu(stdscr)
            return True
        return False

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Keybind hints for navigation, sorting, expand/collapse-all, and file actions."""
        return [
            ("↑/↓", "move"),
            ("s", f"sort: {self.sort_mode}"),
            ("e", "expand all"),
            ("c", "collapse all"),
            ("o", "open"),
            ("O", "open folder"),
            ("x", f"delete ({self.delete_mode})"),
            ("t", "toggle trash/permanent"),
            ("Enter", "file options"),
        ]
