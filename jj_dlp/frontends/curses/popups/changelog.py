"""Post-update changelog popup."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.config import state as config_state
from jj_dlp.core.updater import diff as updater_diff

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def should_show_changelog(data_dir: Path) -> bool:
    """Return whether the changelog hasn't yet been shown for the currently installed version."""
    info = config_state.get_update_check(Path(data_dir))
    installed_sha = info.get("installed_sha")
    return installed_sha is not None and info.get("changelog_shown_for_version") != installed_sha


def mark_changelog_shown(data_dir: Path) -> None:
    """Record that the changelog has now been shown for the currently installed version."""
    data_dir = Path(data_dir)
    installed_sha = config_state.get_update_check(data_dir).get("installed_sha")
    config_state.set_update_check(data_dir, changelog_shown_for_version=installed_sha)


def build_lines_from_schema_files(old_schema_path: Path, new_schema_path: Path) -> List[str]:
    """Diff two shipped fields.json files into changelog lines via core/updater/diff.py."""
    return updater_diff.diff_shipped_files(Path(old_schema_path), Path(new_schema_path))


class ChangelogPopup:
    """Scrollable, dismissible box displaying pre-computed changelog diff lines."""

    def __init__(self, lines: List[str], color_fn: Optional[ColorFn] = None) -> None:
        self.lines = lines or [updater_diff.format_diff([])]
        self.color = color_fn or _default_color_fn
        self.scroll = 0

    def _body_height(self, box_height: int) -> int:
        """Return how many lines of content fit inside the box's interior."""
        return max(1, box_height - 4)

    def _clamp_scroll(self, box_height: int) -> None:
        """Keep the scroll offset within the visible line range."""
        max_scroll = max(0, len(self.lines) - self._body_height(box_height))
        self.scroll = max(0, min(self.scroll, max_scroll))

    def draw(self, stdscr) -> None:
        """Render the changelog box centered on the screen."""
        height, width = stdscr.getmaxyx()
        box_width = min(width, max(len(line) for line in ["What's new"] + self.lines) + 4)
        box_height = min(height, len(self.lines) + 4)
        self._clamp_scroll(box_height)
        body_height = self._body_height(box_height)

        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        win.addstr(1, 2, "What's new"[: box_width - 4], self.color("popup.title", None))
        for i, line in enumerate(self.lines[self.scroll : self.scroll + body_height]):
            win.addstr(2 + i, 2, line[: box_width - 4], curses.A_NORMAL)
        hint = "up/down: scroll  Enter/Esc: close"
        win.addstr(box_height - 2, 2, hint[: box_width - 4], curses.A_DIM)
        win.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int) -> bool:
        """Apply one keypress. Returns True once the popup should close."""
        if key in (curses.KEY_DOWN, ord("j")):
            self.scroll += 1
            return False
        if key in (curses.KEY_UP, ord("k")):
            self.scroll = max(0, self.scroll - 1)
            return False
        return key in (curses.KEY_ENTER, 10, 13, 27, ord("q"), ord("Q"))


def show_changelog(stdscr, lines: List[str], color_fn: Optional[ColorFn] = None) -> None:
    """Block until dismissed, displaying the given changelog diff lines."""
    popup = ChangelogPopup(lines, color_fn)
    popup.draw(stdscr)
    while True:
        key = stdscr.getch()
        if popup.handle_key(key):
            return
        popup.draw(stdscr)


def maybe_show_changelog(
    stdscr, data_dir: Path, lines: List[str], color_fn: Optional[ColorFn] = None
) -> None:
    """Show the changelog and mark it shown, but only if it hasn't been shown for this version."""
    data_dir = Path(data_dir)
    if not should_show_changelog(data_dir):
        return
    show_changelog(stdscr, lines, color_fn)
    mark_changelog_shown(data_dir)
