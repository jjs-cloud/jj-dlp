"""Main loop, screen setup, resize handling, and full-app tab/overlay wiring."""

from __future__ import annotations

import curses
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jj_dlp.core.config import app as app_config
from jj_dlp.core.engine.disk import DiskSampler
from jj_dlp.core.engine.site_state import SiteState
from jj_dlp.core.theme import palette, resolve, store
from jj_dlp.frontends.curses import easter_eggs, footer
from jj_dlp.frontends.curses.popups import changelog as changelog_popup
from jj_dlp.frontends.curses.popups import manage_menu, mgmt_add, mgmt_disable, mgmt_remove
from jj_dlp.frontends.curses.popups import write_failure_alert
from jj_dlp.frontends.curses.popups.exit_confirm import confirm_exit
from jj_dlp.frontends.curses.popups.help import show_help
from jj_dlp.frontends.curses.tabs.config_tab import ConfigTab, SiteSettingsScreen
from jj_dlp.frontends.curses.tabs.dashboard import DashboardTab, SitePanelSource
from jj_dlp.frontends.curses.tabs.eventsub_tab import EventsubSiteSource, EventsubTab
from jj_dlp.frontends.curses.tabs.file_manager_tab import FileManagerSiteSource, FileManagerTab
from jj_dlp.frontends.curses.tabs.framework import TabBar
from jj_dlp.frontends.curses.tabs.log import LogTab
from jj_dlp.frontends.curses.tabs.pipes import PipesTab
from jj_dlp.frontends.curses.tabs.priority_tab import PriorityTab
from jj_dlp.frontends.curses.theme_manager import role_edit, scheme_popup
from jj_dlp.frontends.curses.theme_manager.popup import open_theme_manager

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
    """Owns the curses screen, active theme, loaded-site tabs, and the draw/input loop."""

    def __init__(
        self,
        stdscr,
        data_dir: Path,
        app_state: Optional[Any] = None,
        sites: Optional[List[Tuple[str, dict, SiteState]]] = None,
        disk_sampler: Optional[DiskSampler] = None,
        session_theme_id: Optional[str] = None,
        shutdown_event: Optional[threading.Event] = None,
    ) -> None:
        self.stdscr = stdscr
        self.data_dir = Path(data_dir)
        self.app_state = app_state
        self.shutdown_event = shutdown_event
        self.colors = ColorManager()
        self.theme = self._resolve_startup_theme(session_theme_id)
        self.running = True
        self.height = 0
        self.width = 0

        self.site_sources = list(sites or [])
        self.site_states: Dict[str, SiteState] = {label: state for label, _cfg, state in self.site_sources}

        self._init_curses()
        self._apply_palette()
        self._build_tabs(disk_sampler)
        self._layout()

        self.write_failure_banner = write_failure_alert.WriteFailureAlertBanner(
            color_fn=self.color, jump_fn=self._jump_to_config
        )

    def _resolve_startup_theme(self, session_theme_id: Optional[str]) -> dict:
        """Use the session-only theme override if given, else the saved active theme."""
        if session_theme_id is not None:
            override = store.get_theme(self.data_dir, session_theme_id)
            if override is not None:
                return override
        return store.get_active_theme(self.data_dir)

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

    def _build_tabs(self, disk_sampler: Optional[DiskSampler]) -> None:
        """Build every real tab from the loaded sites and wire them into the TabBar."""
        app_cfg = app_config.load(self.data_dir)

        dash_sources = [SitePanelSource(label, cfg, state) for label, cfg, state in self.site_sources]
        es_sources = [EventsubSiteSource(label, cfg) for label, cfg, _state in self.site_sources]
        fm_sources = [FileManagerSiteSource(label, cfg, state) for label, cfg, state in self.site_sources]

        self.dashboard_tab = DashboardTab(
            disk_sampler=disk_sampler, sites=dash_sources, color_fn=self.color
        )
        self.log_tab = LogTab(color_fn=self.color)
        self.pipes_tab = PipesTab(color_fn=self.color)
        self.eventsub_tab = EventsubTab(self.data_dir, sites=es_sources, color_fn=self.color)
        self.config_tab = ConfigTab(self.data_dir, color_fn=self.color)
        self.priority_tab = PriorityTab(self.data_dir, color_fn=self.color)
        self.file_manager_tab = FileManagerTab(
            sites=fm_sources,
            subfolders_mode=app_cfg.output.subfolders,
            collapsible_folders=app_cfg.output.collapsible_folders,
            color_fn=self.color,
            data_dir=self.data_dir,
        )

        self.tab_bar = TabBar(
            [
                self.dashboard_tab,
                self.log_tab,
                self.pipes_tab,
                self.eventsub_tab,
                self.config_tab,
                self.priority_tab,
                self.file_manager_tab,
            ]
        )

    def _layout(self) -> None:
        """Recompute screen dimensions. Called at startup and on every resize."""
        self.height, self.width = self.stdscr.getmaxyx()

    def color(self, element_id: str, runtime_pair: Optional[ColorTuple] = None) -> int:
        """Resolve a themeable element id to a ready-to-use curses attribute."""
        pair = resolve.resolve_color(self.theme, element_id, runtime_pair)
        return self.colors.attr_for(pair)

    def draw(self) -> None:
        """Draw one frame: the tab strip, the active tab's body, footer, then any overlays."""
        self.stdscr.erase()
        self.tab_bar.draw_bar(self.stdscr, 0, 0, self.width - 1, self.color)
        if self.height > 3:
            self.tab_bar.draw_active(self.stdscr, 1, 0, self.height - 3, self.width - 1)
            easter_eggs.draw_dashboard_decoration(self.stdscr, 1, 0, self.height - 3, self.width - 1, self.color)
        entries = write_failure_alert.collect_write_failures(self.site_states)
        if self.height > 2:
            # Row height-2, not height-1: writing the last cell of the last row can raise curses.error.
            footer.draw_footer(
                self.stdscr, self.height - 2, 0, self.width - 1, self.tab_bar, self.color, show_failures=bool(entries)
            )
        if entries:
            self.write_failure_banner.draw(self.stdscr, entries)
        self.stdscr.noutrefresh()
        curses.doupdate()

    def request_quit(self) -> None:
        """Ask for confirmation if recordings are active, then quit if confirmed."""
        if confirm_exit(self.stdscr, self.site_states.values(), self.color):
            self.running = False

    def _jump_to_config(self, site: str, streamer: str) -> None:
        """Switch to the Config tab's per-site screen for a write-failure banner entry."""
        try:
            index = self.tab_bar.tabs.index(self.config_tab)
        except ValueError:
            return
        self.tab_bar.active_index = index
        self.config_tab.mode = "site"
        self.config_tab.site_screen = SiteSettingsScreen(self.data_dir, site, self.color)

    def _focus_write_failures(self) -> None:
        """Modal loop letting the user navigate/dismiss/jump the write-failure banner."""
        while True:
            entries = write_failure_alert.collect_write_failures(self.site_states)
            if not entries:
                return
            self.draw()
            self.write_failure_banner.draw(self.stdscr, entries)
            curses.doupdate()
            key = self.stdscr.getch()
            if key == 27:
                return
            action = self.write_failure_banner.handle_key(key, entries)
            write_failure_alert.apply_action(action, self.site_states, jump_fn=self._jump_to_config)
            if action is not None and action[0] == "jump":
                return

    def _open_management_overlay(self) -> None:
        """Global management overlay (Doc 1 §17): pick a site, then add/remove/enable-disable."""
        labels = [label for label, _cfg, _state in self.site_sources]
        site = manage_menu.choose_site(self.stdscr, labels, self.color)
        if site is None:
            return
        action = manage_menu.choose_action(self.stdscr, self.color)
        if action == "add":
            mgmt_add.add_streamer(self.stdscr, self.data_dir, site, self.color)
        elif action == "remove":
            mgmt_remove.remove_streamer(self.stdscr, self.data_dir, site, self.color)
        elif action == "disable":
            mgmt_disable.toggle_streamer(self.stdscr, self.data_dir, site, self.color)

    def _open_theme_manager(self) -> None:
        """Global theme-manager overlay; refresh the active theme/palette afterward."""
        open_theme_manager(self.stdscr, self.data_dir, self.color, edit_role_fn=role_edit.open_role_editor)
        self.theme = store.get_active_theme(self.data_dir)
        if app_config.load(self.data_dir).ui.rgb_mode:
            palette.apply_theme_palette(self.theme)

    def handle_key(self, key: int) -> None:
        """Handle one input event: resize, quit, tab switch, else delegate, else global overlays."""
        if key == curses.KEY_RESIZE:
            curses.update_lines_cols()
            self._layout()
        elif key in (ord("q"), ord("Q")):
            self.request_quit()
        elif key == ord("\t"):
            self.tab_bar.next_tab()
        elif key == curses.KEY_BTAB:
            self.tab_bar.prev_tab()
        else:
            consumed = self.tab_bar.handle_key(key)
            if not consumed:
                if key in (ord("m"), ord("M")):
                    self._open_management_overlay()
                elif key in (ord("t"), ord("T")):
                    self._open_theme_manager()
                elif key in (ord("f"), ord("F")):
                    self._focus_write_failures()
                elif key in (ord("h"), ord("H")):
                    entries = write_failure_alert.collect_write_failures(self.site_states)
                    show_help(self.stdscr, footer.build_hints(self.tab_bar, show_failures=bool(entries)), self.color)

    def run(self) -> None:
        """Main draw/input loop: draw a frame, wait for input, repeat until quit/shutdown."""
        self.draw()
        while self.running:
            if self.shutdown_event is not None and self.shutdown_event.is_set():
                return
            key = self.stdscr.getch()
            if key == -1:
                # Timed out with no input; redraw to pick up any external state change.
                self.draw()
                continue
            self.handle_key(key)
            self.draw()


def _maybe_show_changelog(stdscr, data_dir: Path, color_fn) -> None:
    """Show the post-update changelog once, diffing against the pre-update schema snapshot."""
    data_dir = Path(data_dir)
    if not changelog_popup.should_show_changelog(data_dir):
        return
    snapshot = data_dir / "schema" / ".previous_fields.json"
    current = data_dir / "schema" / "fields.json"
    lines = changelog_popup.build_lines_from_schema_files(snapshot, current) if snapshot.exists() else []
    changelog_popup.maybe_show_changelog(stdscr, data_dir, lines, color_fn)
    snapshot.unlink(missing_ok=True)


def _entry(
    stdscr,
    data_dir: Path,
    app_state: Optional[Any],
    sites: Optional[List[Tuple[str, dict, SiteState]]],
    disk_sampler: Optional[DiskSampler],
    shutdown_event: Optional[threading.Event],
) -> None:
    """curses.wrapper target: build and run the app, restoring the palette on exit."""
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
    session_theme_id = scheme_popup.maybe_offer_random_scheme(stdscr, data_dir)
    app = CursesApp(
        stdscr, data_dir, app_state=app_state, sites=sites, disk_sampler=disk_sampler,
        session_theme_id=session_theme_id, shutdown_event=shutdown_event,
    )
    _maybe_show_changelog(stdscr, data_dir, app.color)
    try:
        app.run()
    finally:
        palette.reset_palette()


def main(
    data_dir: Path,
    app_state: Optional[Any] = None,
    sites: Optional[List[Tuple[str, dict, SiteState]]] = None,
    disk_sampler: Optional[DiskSampler] = None,
    shutdown_event: Optional[threading.Event] = None,
) -> None:
    """Initialize curses and run the main loop for the given data directory."""
    curses.wrapper(_entry, data_dir, app_state, sites, disk_sampler, shutdown_event)
