"""Enable/disable-streamer management overlay popup (Doc 1 §17)."""

from __future__ import annotations

import curses
import logging
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from jj_dlp.core.config import sites as sites_config

log = logging.getLogger("jj_dlp.popups.mgmt_disable")

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def toggle_streamer_status(data_dir: Path, label: str, streamer: str) -> bool:
    """Move a streamer between a site's streamers and disabled lists; returns new enabled state."""
    site = sites_config.load_site(data_dir, label)
    streamers = site.setdefault("streamers", [])
    disabled = site.setdefault("disabled", [])
    if streamer in disabled:
        disabled.remove(streamer)
        streamers.append(streamer)
        now_enabled = True
    else:
        if streamer in streamers:
            streamers.remove(streamer)
        disabled.append(streamer)
        now_enabled = False
    sites_config.save_site(data_dir, label, site)
    log.info("%s streamer '%s' on site '%s'", "Enabled" if now_enabled else "Disabled", streamer, label)
    return now_enabled


class DisableStreamerPicker:
    """List picker for toggling streamers between enabled and disabled on one site."""

    def __init__(self, label: str, streamers: List[str], disabled: List[str], color_fn: Optional[ColorFn] = None) -> None:
        self.label = label
        self.streamers = list(streamers)
        self.disabled = list(disabled)
        self.color = color_fn or _default_color_fn
        self.index = 0

    def _rows(self) -> List[Tuple[str, bool]]:
        """Return (name, enabled) pairs for every streamer on the site, enabled ones first."""
        return [(s, True) for s in self.streamers] + [(s, False) for s in self.disabled]

    def draw(self, stdscr) -> None:
        """Render the centered list, marking each row's current enabled/disabled state."""
        height, width = stdscr.getmaxyx()
        title = f"Enable/disable streamers on {self.label}"
        rows = self._rows()
        labels = [f"[{'x' if enabled else ' '}] {name}" for name, enabled in rows] or ["(no streamers)"]

        box_width = max(30, min(width - 2, max(len(t) for t in labels + [title]) + 4))
        box_height = max(6, min(height - 2, len(labels) + 5))
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.erase()
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        win.addstr(0, 2, f" {title} "[: box_width - 4], self.color("popup.title", None))
        for i, text in enumerate(labels):
            row = 2 + i
            if row >= box_height - 2:
                break
            attr = self.color("popup.button_focused", None) if i == self.index else curses.A_NORMAL
            try:
                win.addstr(row, 2, text[: box_width - 4], attr)
            except curses.error:
                pass
        try:
            win.addstr(box_height - 2, 2, "Enter: toggle   Esc: done"[: box_width - 4], curses.A_DIM)
        except curses.error:
            pass
        win.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int) -> Optional[str]:
        """Apply one keypress. Returns a streamer name to toggle, "" on done, else None."""
        rows = self._rows()
        if not rows:
            if key == 27:
                return ""
            return None
        if key == curses.KEY_UP:
            self.index = (self.index - 1) % len(rows)
        elif key == curses.KEY_DOWN:
            self.index = (self.index + 1) % len(rows)
        elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
            return rows[self.index][0]
        elif key == 27:
            return ""
        return None

    def apply_toggle(self, streamer: str, now_enabled: bool) -> None:
        """Update the picker's own row lists after a toggle, keeping the selection sensible."""
        if now_enabled:
            self.disabled.remove(streamer)
            self.streamers.append(streamer)
        else:
            self.streamers.remove(streamer)
            self.disabled.append(streamer)
        self.index = min(self.index, len(self._rows()) - 1)


def toggle_streamer(stdscr, data_dir: Path, label: str, color_fn: Optional[ColorFn] = None) -> None:
    """Run the enable/disable picker, toggling streamers on Enter until the user is done."""
    site = sites_config.load_site(data_dir, label)
    picker = DisableStreamerPicker(
        label, site.get("streamers", []), site.get("disabled", []), color_fn
    )
    while True:
        picker.draw(stdscr)
        key = stdscr.getch()
        chosen = picker.handle_key(key)
        if chosen == "":
            return
        if chosen:
            now_enabled = toggle_streamer_status(data_dir, label, chosen)
            picker.apply_toggle(chosen, now_enabled)
