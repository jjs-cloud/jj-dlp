"""Main loop, screen setup, and resize handling."""

from __future__ import annotations

import curses
from pathlib import Path
from typing import Dict, Optional, Tuple

from jj_dlp.core.config import app as app_config
from jj_dlp.core.theme import palette, resolve, store

ColorTuple = Tuple[str, str, bool]

# Input-poll timeout (ms) driving the redraw tick between keypresses.
TICK_MS = 200

_CURSES_COLOR_NAMES: Dict[str, int] = {
    "black": curses.COLOR_BLACK,
    "red": curses.COLOR_RED,
    "green": curses.COLOR_GREEN,
    "yellow": curses.COLOR_YELLOW,
    "blue": curses.COLOR_BLUE,
    "magenta": curses.COLOR_MAGENTA,
    "cyan": curses.COLOR_CYAN,
    "white": curses.COLOR_WHITE,
}


class ColorManager:
    """Assigns and caches curses color pairs for (fg, bg) combinations."""

    def __init__(self) -> None:
        self._pair_ids: Dict[Tuple[str, str], int] = {}
        self._next_id = 1

    def attr_for(self, color: ColorTuple) -> int:
        """Return the curses attribute (color pair + bold) for an (fg, bg, bold) tuple."""
        fg, bg, bold = color
        attr = curses.color_pair(self._pair_id_for(fg, bg))
        if bold:
            attr |= curses.A_BOLD
        return attr

    def _pair_id_for(self, fg: str, bg: str) -> int:
        """Return (creating if needed) the curses color pair id for an fg/bg pair."""
        key = (fg, bg)
        pair_id = self._pair_ids.get(key)
        if pair_id is None:
            fg_code = _CURSES_COLOR_NAMES.get(fg, curses.COLOR_WHITE)
            bg_code = _CURSES_COLOR_NAMES.get(bg, curses.COLOR_BLACK)
            pair_id = self._next_id
            curses.init_pair(pair_id, fg_code, bg_code)
            self._pair_ids[key] = pair_id
            self._next_id += 1
        return pair_id


class CursesApp:
    """Owns the curses screen, active theme, and the draw/input loop."""

    def __init__(self, stdscr, data_dir: Path) -> None:
        self.stdscr = stdscr
        self.data_dir = Path(data_dir)
        self.colors = ColorManager()
        self.theme = store.get_active_theme(self.data_dir)
        self.running = True
        self.height = 0
        self.width = 0
        self._init_curses()
        self._apply_palette()
        self._layout()

    def _init_curses(self) -> None:
        """One-time curses setup: hide cursor, no echo, color mode, input timeout."""
        curses.curs_set(0)
        curses.noecho()
        curses.cbreak()
        self.stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
        self.stdscr.timeout(TICK_MS)

    def _apply_palette(self) -> None:
        """Push the active theme's true-color palette to the terminal, if enabled."""
        app_cfg = app_config.load(self.data_dir)
        if app_cfg.ui.rgb_mode:
            palette.apply_theme_palette(self.theme)

    def _layout(self) -> None:
        """Recompute screen dimensions. Called at startup and on every resize."""
        self.height, self.width = self.stdscr.getmaxyx()

    def color(self, element_id: str, runtime_pair: Optional[ColorTuple] = None) -> int:
        """Resolve a themeable element id to a ready-to-use curses attribute."""
        pair = resolve.resolve_color(self.theme, element_id, runtime_pair)
        return self.colors.attr_for(pair)

    def draw(self) -> None:
        """Draw one frame. Later phases replace this blank body with real tabs."""
        self.stdscr.erase()
        self.stdscr.attrset(self.color("dashboard.system_panel.border"))
        self.stdscr.border()
        self.stdscr.attrset(curses.A_NORMAL)
        title = " jj-dlp "
        if self.width > len(title) + 2:
            self.stdscr.addstr(0, 2, title, self.color("dashboard.system_panel.text"))
        self.stdscr.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int) -> None:
        """Handle one input event. Only quit and resize are wired at this stage."""
        if key == curses.KEY_RESIZE:
            curses.update_lines_cols()
            self._layout()
        elif key in (ord("q"), ord("Q")):
            self.running = False

    def run(self) -> None:
        """Main draw/input loop: draw a frame, wait for input, repeat until quit."""
        self.draw()
        while self.running:
            key = self.stdscr.getch()
            if key == -1:
                # Timed out with no input; redraw to pick up any external state change.
                self.draw()
                continue
            self.handle_key(key)
            self.draw()


def _entry(stdscr, data_dir: Path) -> None:
    """curses.wrapper target: build and run the app, restoring the palette on exit."""
    app = CursesApp(stdscr, data_dir)
    try:
        app.run()
    finally:
        palette.reset_palette()


def main(data_dir: Path) -> None:
    """Initialize curses and run the main loop for the given data directory."""
    curses.wrapper(_entry, data_dir)
