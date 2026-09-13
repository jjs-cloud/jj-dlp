"""Persistent write-failure alert banner."""

from __future__ import annotations

import curses
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from jj_dlp.core.engine.site_state import SiteState

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]
JumpFn = Callable[[str, str], None]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def _jump_to_config_stub(site: str, streamer: str) -> None:
    """Placeholder jump target until the Config tab exists (Phase 10)."""
    # TODO(Phase 10): open the Config tab's per-site screen for `site`, scrolled to `streamer`.
    pass


@dataclass(frozen=True)
class WriteFailureEntry:
    """One streamer currently flagged with a write failure."""

    site: str
    streamer: str


def collect_write_failures(site_states: Dict[str, SiteState]) -> List[WriteFailureEntry]:
    """Return every (site, streamer) pair currently flagged with write_failure."""
    entries: List[WriteFailureEntry] = []
    for site_label, site_state in site_states.items():
        snapshot = site_state.snapshot()
        for streamer, info in snapshot.streamers.items():
            if info.write_failure:
                entries.append(WriteFailureEntry(site=site_label, streamer=streamer))
    return sorted(entries, key=lambda e: (e.site, e.streamer))


class WriteFailureAlertBanner:
    """Persistent, per-streamer-dismissible banner listing active write-failure alerts."""

    def __init__(self, color_fn: Optional[ColorFn] = None, jump_fn: Optional[JumpFn] = None) -> None:
        self.color = color_fn or _default_color_fn
        self.jump = jump_fn or _jump_to_config_stub
        self.selected = 0

    def _clamp_selection(self, count: int) -> None:
        """Keep the selected index inside [0, count) after the entry list changes."""
        self.selected = 0 if count == 0 else max(0, min(self.selected, count - 1))

    def draw(self, stdscr, entries: List[WriteFailureEntry]) -> None:
        """Render a bordered banner near the top of the screen listing every entry."""
        self._clamp_selection(len(entries))
        if not entries:
            return

        height, width = stdscr.getmaxyx()
        visible = entries[: max(1, height - 4)]
        rows = [f"{e.site}/{e.streamer}" for e in visible]
        title = "WRITE FAILURE(S) DETECTED"
        box_width = min(width, max(len(title), *(len(r) for r in rows)) + 6)
        box_height = min(height, len(rows) + 4)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, 0, x1)
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        win.addstr(1, 2, title[: box_width - 4], self.color("popup.title", None))
        for i, row in enumerate(rows):
            y = 2 + i
            if y >= box_height - 1:
                break
            attr = self.color("popup.button_focused", None) if i == self.selected else self.color("popup.title", None)
            win.addstr(y, 2, row[: box_width - 4], attr)
        hint = "Enter: jump to site  x: dismiss"
        win.addstr(box_height - 2, 2, hint[: box_width - 4], curses.A_DIM)
        win.noutrefresh()

    def handle_key(self, key: int, entries: List[WriteFailureEntry]) -> Optional[Tuple[str, WriteFailureEntry]]:
        """Move selection, or dismiss/jump the selected entry. Returns the action taken, if any."""
        self._clamp_selection(len(entries))
        if not entries:
            return None
        if key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(self.selected + 1, len(entries) - 1)
            return None
        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(self.selected - 1, 0)
            return None

        entry = entries[self.selected]
        if key in (ord("x"), ord("X"), curses.KEY_DC):
            return ("dismiss", entry)
        if key in (curses.KEY_ENTER, 10, 13):
            return ("jump", entry)
        return None


def apply_action(
    action: Optional[Tuple[str, WriteFailureEntry]],
    site_states: Dict[str, SiteState],
    jump_fn: Optional[JumpFn] = None,
) -> None:
    """Carry out a banner action: clear the flag on dismiss, or call jump_fn on jump."""
    if action is None:
        return
    kind, entry = action
    if kind == "dismiss":
        site_state = site_states.get(entry.site)
        if site_state is not None:
            site_state.set_write_failure(entry.streamer, False)
    elif kind == "jump":
        (jump_fn or _jump_to_config_stub)(entry.site, entry.streamer)
