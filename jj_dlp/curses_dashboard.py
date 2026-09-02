#!/usr/bin/env python3
"""jj-dlp — MenuWorks-style curses dashboard.

Everything that touches the `curses` module lives here: the dashboard UI
itself (JJDlpDashboard), the startup config chooser, and the two public
entry points (run_dashboard, choose_config) that main.py calls into.
main.py has no curses-awareness of its own.
"""

import curses
import os
import sys
import textwrap
import time
import re as _re
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from . import theme
from . import graph as _graph
from .file_manager import FileManagerTab
from .config_editor import SiteSortManager
from .logger import dbg, get_debug_log_path

from .main import (
    __version__,
    _SCRIPT_START_TIME,
    _CHECKER_STDOUT_PREFIX,
    _CHECKER_STDERR_PREFIX,
    load_config,
    _write_global_conf_key,
    _modify_config_streamer,
    _save_skip_disabled,
    _safe_disk_usage,
    _load_disk_rate_history,
    _save_disk_rate_history,
)

if TYPE_CHECKING:
    from .main import AppState, SiteState

# ── Debug filter per-filter mode ──────────────────────────────────────────────
# Each debug-log filter now carries its own mode instead of a single global
# "highlight" checkbox applying to every filter.
DEBUG_FILTER_MODES: List[str] = ["filter_highlight", "filter_only", "highlight_only"]
DEBUG_FILTER_MODE_LABELS: Dict[str, str] = {
    "filter_highlight": "Filter+Highlight",
    "filter_only":       "Filter Only",
    "highlight_only":    "Highlight Only",
}
DEBUG_FILTER_MODE_TAGS: Dict[str, str] = {
    "filter_highlight": "F+H",
    "filter_only":       "F",
    "highlight_only":    "H",
}

# ── Dashboard quality-display grace period ───────────────────────────────────
# How long to wait after a recording attempt starts before falling back to
# the checker-reported recording_resolution for the dashboard's "Xp" column.
_QUALITY_DISPLAY_GRACE_SECS: float = 60.0


# ══════════════════════════════════════════════════════════════════════════════
# Curses Dashboard — MenuWorks style
# ══════════════════════════════════════════════════════════════════════════════

ASCII_LOGO = [
    r"     __     __              .___.__          ",
    r"    |__|   |__|           __| _/|  | ______  ",
    r"    |  |   |  |  ______  / __ | |  | \____ \ ",
    r"    |  |   |  | /_____/ / /_/ | |  |_|  |_> >",
    r"/\__|  /\__|  |         \____ | |____/   __/ ",
    r"\______\______|              \/      |__|    ",
]

def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m:
        return f"{m}m {s:02d}s"
    return f"{s}s"

def _live_bar(seconds: float, width: int = 14, max_secs: int = 6 * 3600) -> str:
    filled = min(int(width * seconds / max(1, max_secs)), width)
    return "█" * filled + "░" * (width - filled)

def _live_bar_dashed(seconds: float, width: int = 14, max_secs: int = 6 * 3600) -> str:
    """Like _live_bar(), but for disabled streamers: the filled portion is a
    dashed line (the "─" character with spaces between each dash) and the
    unfilled portion is a solid "─" line. Total length is always `width`."""
    filled = min(int(width * seconds / max(1, max_secs)), width)
    dashed = ("─ " * filled)[:width]
    return dashed + "─" * (width - len(dashed))


class JJDlpDashboard:
    """
    MenuWorks-style curses TUI.

    PANEL LAYOUT (easy to rearrange):
    The dashboard tab shows one panel per site. With 1 site: full width.
    With 2+ sites: 2 columns, stacked rows.

    To change panel order, just reorder the sites list passed to __init__.
    Panel grid: sites[0]=top-left, sites[1]=top-right, sites[2]=bot-left, etc.
    """

    @staticmethod
    def draw_box(stdscr, y1, x1, y2, x2, pair, tag="main_jjdlpdashboard_safe_ch_pair"):
        h, w = stdscr.getmaxyx()
        def safe_ch(y, x, ch):
            if 0 <= y < h and 0 <= x < w - 1:
                try:
                    stdscr.addch(y, x, ch, theme.attr(JJDlpDashboard, tag, pair))
                except curses.error:
                    pass
        for x in range(x1 + 1, x2):
            safe_ch(y1, x, curses.ACS_HLINE)
            safe_ch(y2, x, curses.ACS_HLINE)
        for y in range(y1 + 1, y2):
            safe_ch(y, x1, curses.ACS_VLINE)
            safe_ch(y, x2, curses.ACS_VLINE)
        safe_ch(y1, x1, curses.ACS_ULCORNER)
        safe_ch(y1, x2, curses.ACS_URCORNER)
        safe_ch(y2, x1, curses.ACS_LLCORNER)
        safe_ch(y2, x2, curses.ACS_LRCORNER)

    @staticmethod
    def safe_addstr(stdscr, y, x, text, attr=0):
        h, w = stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        max_len = w - x - 1
        if max_len <= 0:
            return
        try:
            stdscr.addstr(y, x, str(text)[:max_len], attr)
        except curses.error:
            pass

    FLASH_CYCLE = 8

    # ── Tab definitions — configured dynamically in __init__ based on enabled features ──

    def __init__(self, stdscr, app: "AppState", global_cfg: dict = None):
        self.stdscr       = stdscr
        self.app           = app
        self.sites        = app.sites
        self.global_cfg   = global_cfg or {}   # app-wide settings from global.conf
        
        # --- Dynamic Tab Logic ---
        # Start with the mandatory tabs
        self.TABS = ["Dashboard", "Log", "Stdout", "Stderr"]

        # Check if ANY site has Twitch EventSub enabled
        any_eventsub = False
        for site in self.sites:
            cfg = site.get_cached_config()
            if cfg.get("twitch_enabled"):
                any_eventsub = True
                break
        if any_eventsub:
            self.TABS.append("EventSub")

        self.TABS.append("Config")  # Config tab is second-to-last
        self.TABS.append("File Manager")  # File Manager tab is always last
        # --------------------------

        self.selected_tab = 0
        self.selected_site_idx = 0   # for log/config/eventsub tabs
        # Log tab has its own independent site selector (separate from the
        # Config/Stdout/Stderr ']'/'[' selector above): -1 = "All" (default),
        # 0..N-1 = index into self.sites for a single site.
        self._log_site_idx = -1
        # Debug-log toggle used only while the Log tab's selector is on
        # "All" — a single site's own show_debug_log flag isn't meaningful
        # once logs from every site are being merged together, and the
        # toggle/debug output are hidden entirely once a specific site is
        # selected (see draw_log_tab).
        self._log_all_show_debug = False
        self.tick         = 0
        # Streamer management mode: None, or ("add"/"remove"/"disable", site_idx)
        self._mgmt_mode   = None
        self._mgmt_buf    = ""
        self._mgmt_result = ""
        self._mgmt_sel    = 0   # selected index for disable/remove list
        self._mgmt_scroll = 0   # scroll offset for disable/remove list
        # Color scheme index for randomization
        self._color_scheme_idx = theme.DEFAULT_SCHEME_IDX
        # When the 'c'-key scheme-list popup should stop being drawn (epoch secs).
        self._scheme_popup_until = 0.0
        # Scroll offsets for log/stdout/stderr tabs (lines from bottom; 0 = newest at bottom)
        self._log_scroll    = 0
        self._stdout_scroll = 0
        self._stderr_scroll = 0
        # Filters apply only to debug lines in the Log tab.  Multiple
        # expressions are combined with OR; no expression shows all debug.
        self._debug_filter_popup_open = False
        self._debug_filter_entry_open = False
        # Each entry: {"pattern": <regex str>, "mode": one of
        # DEBUG_FILTER_MODES} — mode (Filter+Highlight / Filter Only /
        # Highlight Only) is per-filter, not a single global toggle.
        self._debug_filter_patterns: List[dict] = []
        self._debug_filter_buf = ""
        self._debug_filter_cursor = 0
        self._debug_filter_sel = 0       # 0=input, 1..N=patterns, N+1=export
        self._debug_filter_error = ""
        # ── "create/edit filter" sub-popup state ──────────────────────────
        self._debug_filter_edit_index = None    # None=creating new filter, else index of filter being edited
        self._debug_filter_entry_sel = 0        # 0=regex text field, 1=mode selector
        self._debug_filter_mode = DEBUG_FILTER_MODES[0]
        # When scrolled up (scroll > 0), the displayed lines are frozen to a
        # snapshot so live output can't keep shoving the viewport around. Set
        # while scroll > 0, cleared when the user scrolls back to 0 (live).
        self._log_frozen_lines    = None
        self._stdout_frozen_lines = None
        self._stderr_frozen_lines = None
        # STREAMERS panel (Stdout/Stderr tabs): 0 = "All Streamers",
        # 1..N = index+1 into site.dash_all_streamers. Shared by both tabs
        # and reset whenever the selected site changes.
        self._streamer_panel_sel    = 0
        self._streamer_panel_scroll = 0
        # Which of the two panels (STREAMERS list vs content pane) UP/DOWN
        # currently drives on the Stdout/Stderr tabs. Toggled with Tab, the
        # same way the Config tab cycles its panels.
        self._pipe_focus = "streamers"

        # Disk usage cache — refreshed at most once every 10 seconds
        self._disk_cache_time: float = 0.0
        self._disk_cache_drives: list = []
        self._disk_cache_results: list = []  # list of (drive, usage) or (drive, None) on error

        # ── Top-bar disk-rate sparkline ──────────────────────────────────────
        # All graph state + logic now lives in graph.Graph (hot-swappable
        # via the 'p' knob popup → "Reload graph.py"). GRAPH_SCALE (seconds
        # per bar) stays here as dashboard-owned config the graph reads each
        # tick. It counts only files that are actively being recorded by
        # yt-dlp (per each site's recording_output_paths registry), never
        # File Manager artifact files (Move/Fixup/Trim/Split output).
        # History is kept far longer than any realistic terminal width so
        # widening the window doesn't lose data.
        self.graph_scale: int = max(1, int(self.global_cfg.get("graph_scale", 1)))
        self.graph = _graph.Graph(self)
        # Bars persisted to global.json on the previous run are restored here
        # so the graph comes back with its recent history instead of starting
        # empty.
        self.graph.disk_rate_history.extend(_load_disk_rate_history(self.app))

        from .config_editor import ConfigEditor
        self.config_editor = ConfigEditor(self)

        # Sort manager — controls streamer ordering in site panels
        self.sort_manager = SiteSortManager(self)

        # File Manager tab — watches OUTPUT_DIRs for the current set of sites
        self.file_manager = FileManagerTab(self)

        # Theme manager — owns the theme popup (base scheme, role colors,
        # and per-call-site overrides), bound to the 'n' key.
        self.theme_manager = theme.ThemeManager(self)

        # ── Changelog popup state ─────────────────────────────────────────────
        # Shown once after startup when update_available=false & changelog_shown=false.
        self._changelog_popup_open   = False
        self._changelog_scroll       = 0   # lines scrolled up from the bottom (0 = top)
        self._changelog_lines: List[str] = []
        self._changelog_popup_queued = False   # will be set to True after first frame

        # ── Bake-to-source popup state (dev feature, hidden 'W' hotkey) ──────
        self._bake_popup_open   = False
        self._bake_popup_lines: List[str] = []

        # ── Exit-confirmation popup state ─────────────────────────────────────
        self._exit_confirm_open      = False
        self._exit_confirm_sel       = 0   # 0 = Yes (default), 1 = No

        # ── Recording-failure alert state ───────────────────────────────────
        # Full-screen, flashing, does-not-auto-dismiss modal shown whenever
        # NOTIFY_NO_CONFIRM_FILE fires for a streamer (see flag_write_failure).
        self._write_failure_alert_open = False
        self._write_failure_names: List[str] = []

    # ── Color palette ────────────────────────────────────────────────────────
    # Pair numbers and their meanings — easy to change here
    C_CHROME    = 1   # borders, labels
    C_HILIGHT   = 2   # selected tab
    C_WARN      = 3   # countdown, warnings
    C_LIVE      = 4   # live status
    C_INVHEAD   = 5   # inverse headers
    C_LOGO      = 6   # logo
    C_REC       = 7   # recording dot
    C_DIM       = 8   # dim / offline
    C_LIVEBADGE = 9   # live badge bg
    C_NORMAL    = 10  # normal text
    C_DISABLED  = 11  # disabled/blocked
    C_SYSTEM    = 12  # system panel header/border
    C_DELETE    = 13  # permanent delete warning (white on red)

    # Color schemes now live in theme.py (theme.COLOR_SCHEMES /
    # theme._SCHEME_BACKGROUND) so base-scheme role-color overrides can be
    # baked there directly, without touching this file.

    def randomize_colors(self):
        """Cycle to the next color scheme. Bound to the 'c' key ('C' for
        Colors); the 'n' key opens the full theme editor popup instead,
        which offers the same scheme picker plus role/site customization."""
        self._color_scheme_idx = (self._color_scheme_idx + 1) % len(theme.COLOR_SCHEMES)
        theme.get_state()['base_scheme_idx'] = self._color_scheme_idx
        self._apply_color_scheme()
        theme.save_theme(theme.get_state())
        self._scheme_popup_until = time.time() + 2.0

    def _apply_color_scheme(self):
        """Re-initialize all 13 curses pairs. Delegates to theme.py, which
        layers any saved role-color overrides on top of the active base
        scheme. self._color_scheme_idx is kept in sync with theme's saved
        base_scheme_idx so existing readers (e.g. the DOS Red bold-tabs
        check in draw_tabs) keep working unchanged."""
        self._color_scheme_idx = theme.get_state().get('base_scheme_idx', theme.DEFAULT_SCHEME_IDX) % len(theme.COLOR_SCHEMES)
        theme.apply_palette(self)

    def setup_colors(self):
        curses.start_color()
        curses.use_default_colors()
        if theme.apply_pending_theme_push():
            dbg(f"[THEME] applied one-time theme push (scheme idx {theme.THEME_PUSH})")
        self._apply_color_scheme()

    def draw_scheme_popup(self) -> None:
        """Display-only popup listing every color scheme (current one
        highlighted). Shown for ~2s after the 'c' key cycles schemes."""
        if time.time() >= self._scheme_popup_until:
            return
        h, w = self.stdscr.getmaxyx()
        names = theme.SCHEME_NAMES
        box_h = len(names) + 4
        box_w = min(w - 4, 44)
        by1 = max(0, (h - box_h) // 2)
        bx1 = max(0, (w - box_w) // 2)
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1),
                        theme.attr(self, "main_jjdlpdashboard_draw_scheme_popup_normal_1"))

        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_CHROME)
        self.safe_addstr(self.stdscr, by1, bx1 + 2, " COLOR SCHEMES ",
                    theme.attr(self, "main_jjdlpdashboard_draw_scheme_popup_hilight"))

        for i, name in enumerate(names):
            if i == self._color_scheme_idx:
                attr_ = theme.attr(self, "main_jjdlpdashboard_draw_scheme_popup_live")
                prefix = "* "
            else:
                attr_ = theme.attr(self, "main_jjdlpdashboard_draw_scheme_popup_normal_2")
                prefix = "  "
            self.safe_addstr(self.stdscr, by1 + 2 + i, bx1 + 2,
                        (prefix + name)[:box_w - 4], attr_)

        self.safe_addstr(self.stdscr, by2, bx1 + 2, " c: next scheme ",
                    theme.attr(self, "main_jjdlpdashboard_draw_scheme_popup_invhead"))


    # ── Logo ─────────────────────────────────────────────────────────────────
    def draw_logo(self, y, x):
        for i, line in enumerate(ASCII_LOGO):
            self.safe_addstr(self.stdscr, y + i, x, line,
                        theme.attr(self, "main_jjdlpdashboard_draw_logo_logo"))


    # ── Christmas Day easter egg ────────────────────────────────────────────
    @staticmethod
    def _is_christmas_day() -> bool:
        """Return True on December 24th and 25th (local system date)."""
        _today = datetime.now()
        return _today.month == 12 and _today.day in (24, 25)

    def draw_christmas_easter_egg(self, y, x):
        """A small festive tree shown only on Christmas Eve/Day, below the version number."""
        tree = [
            "   *   ",
            "  /_\\  ",
            " /___\\ ",
            "/_____\\",
            "  | |  ",
        ]

        for i, line in enumerate(tree):
            # Alternate red/green per row for a bit of festive sparkle;
            # the star on top gets the warm/gold color.
            if i == 0:
                pair = self.C_WARN
            elif i % 2 == 1:
                pair = self.C_LIVE
            else:
                pair = self.C_REC
            self.safe_addstr(self.stdscr, y + i, x, line,
                        theme.attr(self, "main_jjdlpdashboard_draw_christmas_easte_pair", pair))

    # ── Tab bar ──────────────────────────────────────────────────────────────
    def draw_tabs(self, y, x):
        for i, tab in enumerate(self.TABS):
            label = f"  {tab}  "
            if i == self.selected_tab:
                self.safe_addstr(self.stdscr, y, x, label,
                            theme.attr(self, "main_jjdlpdashboard_draw_tabs_hilight"))
            else:
                self.safe_addstr(self.stdscr, y, x, label,
                            theme.attr(self, "main_jjdlpdashboard_draw_tabs_invhead"))
            x += len(label) + 1

    # ── System status sidebar ────────────────────────────────────────────────
    def draw_system_panel(self, y1, x1, y2, x2):
        """Draws the SYSTEM info panel (from demo). Placed in the sidebar."""
        self.draw_box(self.stdscr, y1, x1, y2, x2, self.C_SYSTEM)
        self.safe_addstr(self.stdscr, y1, x1 + 2, " SYSTEM ",
                    theme.attr(self, "main_jjdlpdashboard_draw_system_panel_system"))

        # Aggregate counts across all sites
        total_streamers = 0
        live_cnt = 0
        rec_cnt  = 0
        off_cnt  = 0
        dis_cnt  = 0
        site_setting_values = []

        for site in self.sites:
            with site.dash_lock:
                all_s      = list(site.dash_all_streamers)
                blocked    = set(site.dash_blocked)
            live_since = site.snapshot_live_since()
            with site.lock:
                recording  = set(site.currently_recording)
                intro_delay_pending = set(site.intro_delay_pending)
            try:
                cfg = site.get_cached_config()
                site_label = cfg.get("site_label", os.path.basename(site.config_path))
                site_setting_values.append((site_label, cfg))
            except Exception as e:
                dbg(f"draw_system_panel: {e}")
                pass
            total_streamers += len(all_s)
            live_cnt += sum(1 for s in all_s if s in live_since)
            rec_cnt  += sum(1 for s in recording if s not in intro_delay_pending)
            off_cnt  += sum(1 for s in all_s if s not in live_since and s not in blocked)
            dis_cnt  += sum(1 for s in all_s if s in blocked)

        # Uptime
        uptime_secs = int(time.time() - _SCRIPT_START_TIME)
        uptime_str  = _fmt_duration(uptime_secs)

        def _on_off(value) -> str:
            return "ON" if value else "OFF"

        def _site_setting_rows(label, key, formatter, enabled_color=None):
            values = []
            for site_label, cfg in site_setting_values:
                value = formatter(cfg.get(key))
                color = enabled_color(cfg.get(key)) if enabled_color else self.C_CHROME
                values.append((site_label, value, color))
            if not values:
                return [(label, "", self.C_DIM)]
            unique_values = {value for _, value, _ in values}
            if len(unique_values) == 1:
                _, value, color = values[0]
                return [(label, value, color)]
            rows_out = [(label, "", self.C_CHROME)]
            rows_out.extend((f"  {site_label}", value, color) for site_label, value, color in values)
            return rows_out

        def _split_after_rows():
            values = []
            for site_label, cfg in site_setting_values:
                try:
                    split_after = int(cfg.get("split_after", 0) or 0)
                except Exception as e:
                    dbg(f"_split_after_rows: {e}")
                    split_after = 0
                if split_after > 0:
                    values.append((site_label, f"{split_after}m", self.C_CHROME))
            if not values:
                return []
            if len(values) == len(site_setting_values) and len({value for _, value, _ in values}) == 1:
                _, value, color = values[0]
                return [("Split After", value, color)]
            rows_out = [("Split After", "", self.C_CHROME)]
            rows_out.extend((f"  {site_label}", value, color) for site_label, value, color in values)
            return rows_out

        rows = [
            ("Streamers", str(total_streamers), self.C_CHROME),
            ("Live",      str(live_cnt),        self.C_LIVE),
            ("Recording", str(rec_cnt),         self.C_REC),
            ("Offline",   str(off_cnt),         self.C_DIM),
            ("Disabled",  str(dis_cnt),         self.C_DISABLED),
            ("",          "",                   0),
        ]
        rows.extend(_site_setting_rows("Interval", "check_interval", lambda v: f"{60 if v is None else v}s"))
        rows.extend(_site_setting_rows("Logging", "logging", _on_off,
                    lambda v: self.C_LIVE if v else self.C_DIM))
        rows.extend(_site_setting_rows("Popups", "popup_notifications", _on_off,
                    lambda v: self.C_LIVE if v else self.C_DIM))
        rows.extend(_split_after_rows())

        inner_w = x2 - x1 - 2
        label_w = min(13, max(10, inner_w // 2))

        for i, (label, val, cpair) in enumerate(rows):
            row_y = y1 + 2 + i
            if row_y >= y2 - 1:
                break
            if label:
                self.safe_addstr(self.stdscr, row_y, x1 + 2,
                            label[:label_w].ljust(label_w),
                            theme.attr(self, "main_jjdlpdashboard_split_after_rows_dim"))
                self.safe_addstr(self.stdscr, row_y, x1 + 2 + label_w + 1,
                            str(val)[:inner_w - label_w - 1],
                            theme.attr(self, "main_jjdlpdashboard_split_after_rows_cpair", cpair))

        # Disk space rows — drives from global.conf take precedence; fall back to per-site
        disk_row_y = y1 + 2 + len(rows) + 1

        # ffmpeg error counts — one row per streamer that has errors, hidden when none
        ffmpeg_row_y = y1 + 2 + len(rows) + 1
        try:
            # Gather per-streamer counts across all sites
            all_ffmpeg_errors: List[Tuple[str, int]] = []
            for _site in self.sites:
                with _site.dash_lock:
                    site_counts = dict(_site.ffmpeg_error_counts)
                for _streamer, _count in sorted(site_counts.items()):
                    if _count > 0:
                        all_ffmpeg_errors.append((_streamer, _count))

            if all_ffmpeg_errors:
                if ffmpeg_row_y < y2 - 1:
                    self.safe_addstr(self.stdscr, ffmpeg_row_y, x1 + 2,
                                "── ffmpeg errors ──"[:inner_w],
                                theme.attr(self, "main_jjdlpdashboard_split_after_rows_rec_1"))
                    ffmpeg_row_y += 1
                for _streamer, _count in all_ffmpeg_errors:
                    if ffmpeg_row_y >= y2 - 1:
                        break
                    _label = _streamer[:label_w].ljust(label_w)
                    _val   = str(_count)
                    # Same color as "ad detected" counter
                    _ffmpeg_attr = theme.attr(self, "main_jjdlpdashboard_split_after_rows_warn_2")
                    self.safe_addstr(self.stdscr, ffmpeg_row_y, x1 + 2,
                                _label, _ffmpeg_attr)
                    self.safe_addstr(self.stdscr, ffmpeg_row_y, x1 + 2 + label_w + 1,
                                _val[:inner_w - label_w - 1], _ffmpeg_attr)
                    ffmpeg_row_y += 1
                disk_row_y = ffmpeg_row_y + 1
        except Exception as _ffmpeg_err_exc:
            dbg(f"[SYSTEM] ffmpeg error section exception: {_ffmpeg_err_exc!r}")

        # Stall duration — one row per streamer stalled >= 5s, hidden otherwise
        try:
            _now = time.time()
            all_stalls: List[Tuple[str, float]] = []
            for _site in self.sites:
                with _site.dash_lock:
                    site_stalls = dict(_site.stall_since)
                for _streamer, _since in sorted(site_stalls.items()):
                    _secs = _now - _since
                    if _secs >= 5.0:
                        all_stalls.append((_streamer, _secs))

            if all_stalls:
                if disk_row_y < y2 - 1:
                    self.safe_addstr(self.stdscr, disk_row_y, x1 + 2,
                                "── stalled ──"[:inner_w],
                                theme.attr(self, "main_jjdlpdashboard_split_after_rows_rec_4"))
                    disk_row_y += 1
                for _streamer, _secs in all_stalls:
                    if disk_row_y >= y2 - 1:
                        break
                    _label = _streamer[:label_w].ljust(label_w)
                    _val   = _fmt_duration(int(_secs))
                    self.safe_addstr(self.stdscr, disk_row_y, x1 + 2,
                                _label,
                                theme.attr(self, "main_jjdlpdashboard_split_after_rows_rec_5"))
                    self.safe_addstr(self.stdscr, disk_row_y, x1 + 2 + label_w + 1,
                                _val[:inner_w - label_w - 1],
                                theme.attr(self, "main_jjdlpdashboard_split_after_rows_rec_6"))
                    disk_row_y += 1
                disk_row_y += 1
        except Exception as _stall_exc:
            dbg(f"[SYSTEM] stall section exception: {_stall_exc!r}")

        # Ad alerts — one row per streamer with a recent ad signal.
        try:
            all_ad_alerts: List[str] = []
            for _site in self.sites:
                with _site.dash_lock:
                    site_ads = dict(_site.ad_alerts)
                for _streamer in sorted(site_ads.keys()):
                    all_ad_alerts.append(_streamer)

            if all_ad_alerts:
                if disk_row_y < y2 - 1:
                    self.safe_addstr(self.stdscr, disk_row_y, x1 + 2,
                                "── ads ──"[:inner_w],
                                theme.attr(self, "main_jjdlpdashboard_split_after_rows_warn_1"))
                    disk_row_y += 1
                for _streamer in all_ad_alerts:
                    if disk_row_y >= y2 - 1:
                        break
                    _label = _streamer[:label_w].ljust(label_w)
                    _attr  = theme.attr(self, "main_jjdlpdashboard_split_after_rows_warn_2")
                    self.safe_addstr(self.stdscr, disk_row_y, x1 + 2,
                                _label, _attr)
                    self.safe_addstr(self.stdscr, disk_row_y, x1 + 2 + label_w + 1,
                                "Ad detected"[:inner_w - label_w - 1], _attr)
                    disk_row_y += 1
                disk_row_y += 1
        except Exception as _ad_exc:
            dbg(f"[SYSTEM] ad alerts section exception: {_ad_exc!r}")

        # Disk space rows — drives from global.conf take precedence; fall back to per-site
        try:
            now = time.monotonic()
            if now - self._disk_cache_time >= 10.0:
                # Rebuild the drives list
                seen_drives: list = []
                seen_drives_set: set = set()      # dedupe key -> resolved filesystem (st_dev), falls back to normcased path
                seen_fallback_dirs: set = set()    # dedupe key -> resolved filesystem, for output_dir fallbacks specifically

                def _dedupe_key(path: str) -> str:
                    # Prefer deduping by the actual filesystem/mount (st_dev) so that two
                    # different paths on the same drive don't produce duplicate rows, and
                    # so a typo'd/missing path doesn't silently merge with an unrelated one.
                    try:
                        return f"dev:{os.stat(path).st_dev}"
                    except Exception as e:
                        dbg(f"_dedupe_key: {e}")
                        return f"path:{os.path.normcase(path)}"

                # 1. Global drives (from global.conf) — shown first if configured
                global_drives = self.global_cfg.get("disk_drives", [])
                for d in global_drives:
                    key = _dedupe_key(d)
                    if key not in seen_drives_set:
                        seen_drives_set.add(key)
                        seen_drives.append(d)

                # 2. Per-site drives (merged in, deduped) — explicit disk_drives take
                #    precedence per-site; otherwise fall back to that site's own
                #    output_dir so EVERY site's actual output filesystem is represented,
                #    not just the first site encountered.
                for _site in self.sites:
                    try:
                        _cfg = _site.get_cached_config()
                        drives_for_site = _cfg.get("disk_drives", [])
                        if drives_for_site:
                            for d in drives_for_site:
                                key = _dedupe_key(d)
                                if key not in seen_drives_set:
                                    seen_drives_set.add(key)
                                    seen_drives.append(d)
                        else:
                            out_dir = _cfg.get("output_dir", "/")
                            key = _dedupe_key(out_dir)
                            if key not in seen_drives_set and key not in seen_fallback_dirs:
                                seen_fallback_dirs.add(key)
                                seen_drives_set.add(key)
                                seen_drives.append(out_dir)
                    except Exception as _disk_site_exc:
                        dbg(f"[DISK] exception reading site config: {_disk_site_exc!r}")

                drives = seen_drives if seen_drives else ["/"]
                dbg(f"[DISK] refreshing cache — drives={drives!r}")

                self._disk_cache_time = now  # update immediately to prevent multiple threads
                
                # Query disk usage for each drive in a background thread
                def _update_disk_usage(drives_to_check):
                    import threading
                    t0 = time.time()
                    results = []
                    for drive in drives_to_check:
                        try:
                            usage = _safe_disk_usage(drive)
                            results.append((drive, usage))
                            dbg(f"[DISK] {drive!r} → free={usage.free/(1024**3):.1f}G")
                        except Exception as _disk_exc:
                            results.append((drive, None))
                            dbg(f"[DISK] _safe_disk_usage({drive!r}) FAILED: {type(_disk_exc).__name__}: {_disk_exc}")
                    
                    self._disk_cache_results = results
                    self._disk_cache_drives  = drives_to_check
                    dbg(f"[PERF][disk_usage] background check for {drives_to_check} took {(time.time() - t0)*1000:.2f}ms")

                import threading
                threading.Thread(target=_update_disk_usage, args=(drives,), daemon=True).start()

            if disk_row_y < y2 - 1:
                _disk_header = "── Disk ──"
                self.safe_addstr(self.stdscr, disk_row_y, x1 + 2,
                            _disk_header[:inner_w],
                            theme.attr(self, "main_jjdlpdashboard_update_disk_usage_system"))
                disk_row_y += 1
            for drive, usage in self._disk_cache_results:
                if disk_row_y >= y2 - 1:
                    break
                if usage is None:
                    continue
                pct     = (usage.used / usage.total * 100) if usage.total else 0
                free_gb = usage.free / (1024**3)
                # Short label: last component or drive letter
                drv_label = os.path.basename(drive.rstrip("/\\")) or drive
                drv_label = drv_label[:10]
                disk_str  = f"{drv_label:<10} {free_gb:>4.1f}G {pct:>3.0f}%"
                color = self.C_LIVE if pct < 80 else (self.C_WARN if pct < 95 else self.C_REC)
                self.safe_addstr(self.stdscr, disk_row_y, x1 + 2,
                            disk_str[:inner_w],
                            theme.attr(self, "main_jjdlpdashboard_update_disk_usage_color", color))
                disk_row_y += 1
        except Exception as _disk_outer_exc:
            dbg(f"[DISK] outer exception in disk section: {type(_disk_outer_exc).__name__}: {_disk_outer_exc}")

        # Uptime at bottom
        self.safe_addstr(self.stdscr, y2 - 1, x1 + 2,
                    f"Up: {uptime_str}"[:inner_w],
                    theme.attr(self, "main_jjdlpdashboard_update_disk_usage_chrome"))

    # ── Site panel (one per config) ──────────────────────────────────────────
    def draw_site_panel(self, site: "SiteState", y1, x1, y2, x2, is_selected: bool = False):
        """
        Draws one site's streamer list inside the given bounding box.
        This is the main reusable panel — rearrange by changing caller geometry.
        """
        now = time.time()
        #Pick border color based on selection
        border_pair = self.C_HILIGHT if is_selected else self.C_CHROME
        border_tag = ("main_jjdlpdashboard_draw_site_panel_border_hilight"
                      if is_selected else "main_jjdlpdashboard_draw_site_panel_border_chrome")
        self.draw_box(self.stdscr, y1, x1, y2, x2, border_pair, border_tag)

        # ── Panel header ──
        _panel_cfg = site.get_cached_config()
        _normal_w = None

        # Width of the live-duration column (enough for "999h 59m 59s" without truncation)
        DURATION_WIDTH = 12

        with site.dash_lock:
            cfg_label    = _panel_cfg.get("site_label",
                                       os.path.basename(site.config_path))
            all_s        = list(site.dash_all_streamers)
            last_live    = dict(site.dash_last_live)
            blocked      = set(site.dash_blocked)
            next_in      = site.dash_next_check_in
        live_since   = site.snapshot_live_since()
        with site.dash_lock:
            ad_alert_streamers = set(site.ad_alerts.keys())
            ffmpeg_error_streamers = {s for s, c in site.ffmpeg_error_counts.items() if c > 0}
        with site.lock:
            recording     = set(site.currently_recording)
            intro_delay_pending = set(site.intro_delay_pending)
            # Prefer the ffprobe-measured on-disk resolution
            # fall back to the checker-reported
            recording_res = {
                s: h for s, h in site.recording_resolution.items()
                if (now - site.recording_attempt_started.get(s, 0)) >= _QUALITY_DISPLAY_GRACE_SECS
            }
            # Add an asterisk when we use the checker-reported fallback.
            recording_res_is_fallback = set(recording_res.keys()) - set(site.display_resolution.keys())
            recording_res.update(site.display_resolution)

        # Apply the active sort order to the streamer list.
        all_s = self.sort_manager.get_sorted_streamers(site, all_s, live_since, last_live)

        try:
            _bar_max_secs = _panel_cfg.get("progress_bar_max_hours", 6) * 3600
            _bar_cfg_w    = max(4, _panel_cfg.get("progress_bar_width", 14))
            _last_live_highlight_days = _panel_cfg.get("last_live_highlight", 0)
        except Exception as e:
            dbg(f"draw: bad panel config, using defaults: {e}")
            _bar_max_secs = 6 * 3600
            _bar_cfg_w    = 14
            _last_live_highlight_days = 0

        # Counts for header badges
        live_cnt = sum(1 for s in all_s if s in live_since)
        rec_cnt  = sum(1 for s in recording if s not in intro_delay_pending)
        off_cnt  = sum(1 for s in all_s if s not in live_since and s not in blocked)
        dis_cnt  = sum(1 for s in all_s if s in blocked)

        header_y = y1
        # Site label on top border
        label_text = f"  {cfg_label}  "
        self.safe_addstr(self.stdscr, header_y, x1 + 2, label_text,
                    theme.attr(self, "main_jjdlpdashboard_draw_site_panel_chrome_1"))

        # Status badge row
        badge_y = y1 + 1
        bx = x1 + 2
        live_text = f"LIVE:{live_cnt}"
        self.safe_addstr(self.stdscr, badge_y, bx,
                    live_text,  theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_1"))
        bx += len(live_text) + 1
        rec_text = f"REC:{rec_cnt}"
        self.safe_addstr(self.stdscr, badge_y, bx,
                    rec_text,    theme.attr(self, "main_jjdlpdashboard_draw_site_panel_rec_1"))
        bx += len(rec_text) + 1
        off_text = f"OFF:{off_cnt}"
        self.safe_addstr(self.stdscr, badge_y, bx,
                    off_text,    theme.attr(self, "main_jjdlpdashboard_draw_site_panel_dim_1"))
        bx += len(off_text) + 1
        if dis_cnt:
            self.safe_addstr(self.stdscr, badge_y, bx,
                        f"DIS:{dis_cnt}", theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_1"))

        # ── Streamer rows ──
        panel_width  = x2 - x1 - 2   # usable inner width
        row_start    = y1 + 3
        max_rows     = y2 - row_start - 1   # leave 1 row at bottom for countdown

        # Resolve COMPACT_VIEW mode
        _compact_cfg = self.global_cfg.get("compact_view", "auto")
        _compact_forced = (_compact_cfg == "true")
        _compact_disabled = (_compact_cfg == "false")
        _compact_auto = (not _compact_forced and not _compact_disabled)
        # In normal view, 1 streamer per row. In compact, 2 per row.
        num_streamers = len(all_s)
        _use_compact = _compact_forced or (_compact_auto and num_streamers > max_rows)

        if _use_compact:
            # Compact: 2 columns, no progress bar, no duration, shows last_live
            half_w = max(20, (panel_width - 2) // 2)
            last_live_w_compact = 7
            # Size the name column to the longest actual name (capped), so short
            # names don't leave a big gap before the status tag.
            _max_name_len = max((len(s) for s in all_s), default=6)
            name_w_compact = max(6, min(_max_name_len + 1, half_w - 16))
            _col_gap = 2
            _sep_col = x1 + 2 + half_w + (_col_gap // 2)
            _rows_used = min(max_rows, (len(all_s) + 1) // 2)
            for _sy in range(row_start, row_start + _rows_used):
                self.safe_addstr(self.stdscr, _sy, _sep_col, "│",
                            theme.attr(self, "main_jjdlpdashboard_draw_site_panel_chrome_2"))
            for i, s in enumerate(all_s):
                row_idx = i // 2
                col_idx = i % 2
                if row_idx >= max_rows:
                    break
                row_y = row_start + row_idx
                is_dis = s in blocked
                since = live_since.get(s)
                is_rec = s in recording and s not in intro_delay_pending

                # "Last Live" value for this streamer
                ll_ts = last_live.get(s)
                if is_rec and recording_res.get(s) is not None:
                    _suffix = "*" if s in recording_res_is_fallback else ""
                    last_live_str = f"{recording_res.get(s)}p{_suffix}"
                elif ll_ts is not None:
                    ll_ago = int(now - ll_ts)
                    if ll_ago < 60:
                        last_live_str = f"{ll_ago}s ago"
                    elif ll_ago < 3600:
                        last_live_str = f"{ll_ago//60}m ago"
                    elif ll_ago < 86400:
                        last_live_str = f"{ll_ago//3600}h ago"
                    else:
                        last_live_str = f"{ll_ago//86400}d ago"
                else:
                    last_live_str = ""

                if is_dis:
                    name_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_2")
                    if since is not None:
                        if (self.tick % self.FLASH_CYCLE) < (self.FLASH_CYCLE // 2):
                            status_str = "[● Live]"
                            status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_3")
                        else:
                            status_str = "[x  DIS]"
                            status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_4")
                    else:
                        status_str = "[x  DIS]"
                        status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_5")
                elif since is not None:
                    name_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_2")
                    if is_rec:
                        if (self.tick % self.FLASH_CYCLE) < (self.FLASH_CYCLE // 2):
                            status_str = "[● Live]"
                            status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_3")
                        else:
                            status_str = "[►  REC] "
                            status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_rec_2")
                    else:
                        status_str = "[● Live]"
                        status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_4")
                    if not (is_rec and recording_res.get(s) is not None):
                        last_live_str = ""  # currently live, no "last live"
                else:
                    name_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_dim_2")
                    status_str = "[○  off]"
                    status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_dim_3")

                col = x1 + 2 + col_idx * (half_w + _col_gap)
                self.safe_addstr(self.stdscr, row_y, col,
                            s[:name_w_compact].ljust(name_w_compact), name_attr)
                col += name_w_compact + 1
                self.safe_addstr(self.stdscr, row_y, col,
                            status_str[:8].ljust(8), status_attr)
                col += 9
                if last_live_str:
                    if (ll_ts is not None
                            and _last_live_highlight_days > 0
                            and (now - ll_ts) <= _last_live_highlight_days * 86400):
                        ll_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_5")
                    else:
                        ll_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_dim_4")
                    # Clamp to the panel's inner boundary so this never draws
                    # into/through the right border on narrow terminals.
                    _avail = max(0, (x2 - 1) - col)
                    self.safe_addstr(self.stdscr, row_y, col,
                                last_live_str[:min(last_live_w_compact, _avail)],
                                ll_attr)
        else:
            # Normal: 1 column with progress bar, duration, last_live
            # Column widths — bar_w honours PROGRESS_BAR_WIDTH but won't overflow the row.
            # Row layout: [name_w] 1 [status=8] 1 [bar_w] 1 [dur=9] 1 [last_live_w]
            # So the actual space available for the bar is what's left after the fixed columns.
            name_w      = max(10, min(18, panel_width // 4))
            last_live_w = 12
            _fixed_cols = name_w + 1 + 8 + 1 + 1 + DURATION_WIDTH + 1 + last_live_w  # everything except bar
            bar_w       = max(4, min(_bar_cfg_w, panel_width - _fixed_cols))

            for i, s in enumerate(all_s):
                if i >= max_rows:
                    break
                row_y    = row_start + i
                is_dis   = s in blocked
                since    = live_since.get(s)
                is_rec   = s in recording and s not in intro_delay_pending

                # "Last Live" value for this streamer
                ll_ts = last_live.get(s)
                if is_rec and recording_res.get(s) is not None:
                    _suffix = "*" if s in recording_res_is_fallback else ""
                    last_live_str = f"{recording_res.get(s)}p{_suffix}"
                elif ll_ts is not None:
                    ll_ago = int(now - ll_ts)
                    if ll_ago < 60:
                        last_live_str = f"{ll_ago}s ago"
                    elif ll_ago < 3600:
                        last_live_str = f"{ll_ago//60}m ago"
                    elif ll_ago < 86400:
                        last_live_str = f"{ll_ago//3600}h ago"
                    else:
                        last_live_str = f"{ll_ago//86400}d ago"
                else:
                    last_live_str = ""

                if is_dis:
                    name_attr   = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_6")
                    bar_attr    = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_7")
                    if since is not None:
                        elapsed     = now - since
                        bar_str     = _live_bar_dashed(elapsed, bar_w, _bar_max_secs)
                        dur_str     = _fmt_duration(elapsed)
                        if (self.tick % self.FLASH_CYCLE) < (self.FLASH_CYCLE // 2):
                            status_str  = "[● Live]"
                            status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_8")
                        else:
                            status_str  = "[x  DIS]"
                            status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_9")
                    else:
                        bar_str     = _live_bar_dashed(0, bar_w, _bar_max_secs)
                        dur_str     = ""
                        status_str  = "[x  DIS]"
                        status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_disabled_10")
                elif since is not None:
                    elapsed     = now - since
                    name_attr   = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_6")
                    if is_rec:
                        if (self.tick % self.FLASH_CYCLE) < (self.FLASH_CYCLE // 2):
                            status_str  = "[● Live]"
                            status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_7")
                        else:
                            status_str  = "[►  REC] "
                            status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_rec_3")
                    else:
                        status_str  = "[● Live]"
                        status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_8")
                    bar_str     = (_live_bar_dashed(elapsed, bar_w, _bar_max_secs)
                                   if recording_res.get(s) is None
                                   else _live_bar(elapsed, bar_w, _bar_max_secs))
                    bar_attr    = (theme.attr(self, "main_jjdlpdashboard_split_after_rows_warn_2")
                                   if s in ad_alert_streamers or s in ffmpeg_error_streamers
                                   else theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_9"))
                    dur_str     = _fmt_duration(elapsed)
                    if not (is_rec and recording_res.get(s) is not None):
                        last_live_str = ""  # currently live, no "last live"
                else:
                    name_attr   = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_dim_5")
                    status_str  = "[○  off]"
                    status_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_dim_6")
                    bar_str     = "─" * bar_w
                    bar_attr    = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_dim_7")
                    dur_str     = ""

                col = x1 + 2
                self.safe_addstr(self.stdscr, row_y, col,
                            s[:name_w].ljust(name_w), name_attr)
                col += name_w + 1
                self.safe_addstr(self.stdscr, row_y, col,
                            status_str[:8].ljust(8), status_attr)
                col += 9
                self.safe_addstr(self.stdscr, row_y, col, bar_str, bar_attr)
                col += bar_w + 1
                if dur_str:
                    self.safe_addstr(self.stdscr, row_y, col,
                                dur_str[:DURATION_WIDTH].ljust(DURATION_WIDTH), theme.attr(self, "main_jjdlpdashboard_draw_site_panel_chrome_3"))
                else:
                    self.safe_addstr(self.stdscr, row_y, col, " " * DURATION_WIDTH, 0)
                col += DURATION_WIDTH + 1
                if last_live_str:
                    if (ll_ts is not None
                            and _last_live_highlight_days > 0
                            and (now - ll_ts) <= _last_live_highlight_days * 86400):
                        ll_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_live_10")
                    else:
                        ll_attr = theme.attr(self, "main_jjdlpdashboard_draw_site_panel_dim_8")
                    # Clamp to the panel's inner boundary so this never draws
                    # into/through the right border on narrow terminals.
                    _avail = max(0, (x2 - 1) - col)
                    self.safe_addstr(self.stdscr, row_y, col,
                                last_live_str[:min(last_live_w, _avail)],
                                ll_attr)

        # ── Countdown ──
        nxt = max(0.0, next_in)
        if nxt <= 0:
            # Bouncing-dot ellipsis while waiting for the next check to kick off.
            # Cycles through three frames at the same rate as the Live/REC flash:
            #   frame 0 → ".    "  (left dot)
            #   frame 1 → "  .  "  (middle dot)
            #   frame 2 → "    ."  (right dot)
            _ell_frame = (self.tick // (self.FLASH_CYCLE // 2)) % 3
            _ell_frames = (".    ", "  .  ", "    .")
            _nxt_str = _ell_frames[_ell_frame]
        else:
            _nxt_str = f"{nxt:>4.0f}s"
        self.safe_addstr(self.stdscr, y2 - 1, x1 + 2,
                    f"Next check: {_nxt_str}",
                    theme.attr(self, "main_jjdlpdashboard_draw_site_panel_warn"))

    # ── Dashboard tab ────────────────────────────────────────────────────────
    def draw_dashboard_tab(self, y1, x1, y2, x2):
        """
        LAYOUT LOGIC — easy to rearrange:
        1 site  → single panel filling the whole area
        2 sites → side by side (2 columns)
        3 sites → [A][B] top, [C][ ] bottom
        4 sites → [A][B] top, [C][D] bottom
        5+ sites→ 2-column grid, panels share available height

        To reorder panels, just reorder self.sites in __init__.
        """
        n       = len(self.sites)
        cols    = min(2, n)
        if cols == 0:
            return

        total_w = x2 - x1
        total_h = y2 - y1

        base_rows = (n + cols - 1) // cols
        base_panel_h = total_h // max(1, base_rows)
        base_max_streamers = max(0, base_panel_h - 5)

        site_zones = []
        for site in self.sites:
            cfg = site.get_cached_config()
            panel_resize = cfg.get("panel_resize", True)
            with site.dash_lock:
                num_streamers = len(site.dash_all_streamers)
            
            if panel_resize and num_streamers >= base_max_streamers:
                site_zones.append(2)
            else:
                site_zones.append(1)

        col_heights = [0] * cols
        site_positions = []
        
        for span in site_zones:
            if cols == 1:
                col = 0
            else:
                col = 0 if col_heights[0] <= col_heights[1] else 1
            
            start_row = col_heights[col]
            site_positions.append((col, start_row, span))
            col_heights[col] += span

        total_rows = max(max(col_heights), 1)
        panel_w = total_w // cols
        panel_h = total_h // total_rows

        for idx, site in enumerate(self.sites):
            col, start_row, span = site_positions[idx]

            px1 = x1 + col * panel_w
            px2 = px1 + panel_w - (0 if col == cols - 1 else 1)
            py1 = y1 + start_row * panel_h
            
            end_row = start_row + span
            py2 = py1 + span * panel_h - (0 if end_row == total_rows else 1)

            # Keep panels within bounds
            px2 = min(px2, x2)
            py2 = min(py2, y2)

            # Check if this is the active site
            is_selected = (idx == self.selected_site_idx)
            
            self.draw_site_panel(site, py1, px1, py2, px2, is_selected)

    # ── Line-wrap helper ─────────────────────────────────────────────────────
    _CONTROL_CHAR_RE = _re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')

    @staticmethod
    def _apply_freeze(scroll: int, live_lines: List[str],
                      frozen: Optional[List[str]]) -> "Tuple[Optional[List[str]], List[str]]":
        """Return (updated_frozen, lines_to_draw) for the scroll-up freeze.

        scroll == 0 means live-following: drop any stale snapshot.
        scroll >  0 freezes the display on a snapshot so active writers
        (ffmpeg) can't keep shifting the viewport while the user reads.
        """
        if scroll == 0:
            return None, live_lines
        if frozen is None:
            return list(live_lines), list(live_lines)
        return frozen, frozen

    @classmethod
    def _sanitize_line(cls, line: str) -> str:
        """Strip control characters (stray '\\r' in particular) that would
        otherwise make curses jump the cursor mid-draw and smear output
        over neighboring panels."""
        return cls._CONTROL_CHAR_RE.sub("", line)

    @classmethod
    def _wrap_lines(cls, lines: List[str], max_width: int) -> List[str]:
        """Wrap each line to max_width characters, preserving order.

        Wraps on word boundaries so a word that would run into the margin
        is pushed to the next line whole, rather than being cut off
        mid-word. Words longer than max_width on their own are still
        hard-broken (there's nowhere else to put them).
        """
        if max_width <= 0:
            return lines
        wrapped = []
        for line in lines:
            line = cls._sanitize_line(line)
            if not line:
                wrapped.append("")
                continue
            if len(line) <= max_width:
                wrapped.append(line)
                continue
            wrapped.extend(textwrap.wrap(
                line,
                width=max_width,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""])
        return wrapped

    # ── Log tab ──────────────────────────────────────────────────────────────
    def draw_log_tab(self, y1, x1, y2, x2):
        # Site selector across the top — Log tab has its own independent
        # selector (self._log_site_idx), with an "All" option in addition
        # to each individual site. -1 == "All" (the default).
        is_all_sites = (self._log_site_idx == -1)
        sel_site = (None if is_all_sites or not self.sites
                    else self.sites[self._log_site_idx])
        tab_x    = x1 + 1
        self.safe_addstr(self.stdscr, y1, x1, "  Site: ",
                    theme.attr(self, "main_jjdlpdashboard_draw_log_tab_dim_1"))
        tab_x += 8

        all_label = " All "
        all_attr  = (theme.attr(self, "main_jjdlpdashboard_draw_log_tab_hilight")
                     if is_all_sites
                     else theme.attr(self, "main_jjdlpdashboard_draw_log_tab_chrome"))
        self.safe_addstr(self.stdscr, y1, tab_x, all_label, all_attr)
        tab_x += len(all_label) + 1

        for i, site in enumerate(self.sites):
            lbl = site.get_cached_config().get("site_label",
                              os.path.basename(site.config_path))
            label = f" {lbl} "
            attr  = (theme.attr(self, "main_jjdlpdashboard_draw_log_tab_hilight")
                     if (not is_all_sites and i == self._log_site_idx)
                     else theme.attr(self, "main_jjdlpdashboard_draw_log_tab_chrome"))
            self.safe_addstr(self.stdscr, y1, tab_x, label, attr)
            tab_x += len(label) + 1

        # The debug-log toggle/output only applies while "All" is selected —
        # once a single site is picked, the debug toggle line and any debug
        # lines are hidden entirely.
        show_debug = self._log_all_show_debug if is_all_sites else False
        if is_all_sites:
            title = (" ACTIVITY LOG — Show Debug: ON  (Press A to toggle) (Press F to configure filters) "
                     if show_debug
                     else " ACTIVITY LOG — Show Debug: OFF (Press A to toggle) ")
        else:
            title = " ACTIVITY LOG "

        # Keep the title, but leave the log itself unframed so terminal text
        # selection can target just the log lines without collecting box art.
        self.safe_addstr(self.stdscr, y1 + 1, x1, title,
                    theme.attr(self, "main_jjdlpdashboard_draw_log_tab_dim_2"))

        if not self.sites:
            return

        visible_rows = (y2 - y1) - 2
        line_width   = max(1, x2 - x1 + 1)

        if is_all_sites:
            # Merge activity/debug lines from every site, tagged with the
            # site's label so lines can still be told apart, plus the
            # process-wide lines (e.g. web UI startup), which aren't tied
            # to any one site and so appear once here, untagged.
            raw_lines: List[str] = []
            raw_debug: List[str] = []
            with self.app.global_log_lock:
                raw_lines.extend(self.app.global_log_lines)
            for site in self.sites:
                lbl = site.get_cached_config().get(
                    "site_label", os.path.basename(site.config_path))
                with site.dash_lock:
                    site_lines = list(site.dash_log_lines)
                    site_debug = list(site.dash_debug_lines) if show_debug else []
                raw_lines.extend(
                    f"{line[:21]} [{lbl}]{line[21:]}" if line[:1] == "[" else line
                    for line in site_lines
                )
                raw_debug.extend(
                    f"{line[:21]} [{lbl}]{line[21:]}" if line[:1] == "[" else line
                    for line in site_debug
                )
            raw_lines.sort(key=lambda ln: ln[:20] if ln[:1] == "[" else "")
            raw_debug.sort(key=lambda ln: ln[:20] if ln[:1] == "[" else "")
        else:
            with sel_site.dash_lock:
                raw_lines = list(sel_site.dash_log_lines)
                raw_debug = []

        # Compile per-filter, keeping each filter's own mode alongside it.
        # "filter_only"/"filter_highlight" filters restrict which debug
        # lines are shown; "highlight_only" filters never hide lines, they
        # only mark up matches on lines that are already shown.
        compiled_entries = []
        for entry in self._debug_filter_patterns:
            try:
                compiled_entries.append((_re.compile(entry["pattern"]), entry.get("mode", "filter_highlight")))
            except _re.error:
                continue
        filtering_patterns = [c for c, mode in compiled_entries if mode != "highlight_only"]
        highlighting_patterns = [c for c, mode in compiled_entries if mode != "filter_only"]
        if filtering_patterns:
            raw_debug = [line for line in raw_debug
                         if any(pattern.search(line) for pattern in filtering_patterns)]

        # Activity lines are always shown. Debug lines are only pulled in
        # (and merged back into chronological order) when the toggle is on —
        # they live in their own buffer so they never displaced activity
        # lines in the first place.
        def _timestamp(line: str) -> str:
            return line[:20] if line[:1] == "[" else ""

        # Preserve the source type for optional match highlighting.  Activity
        # lines remain visible regardless of the debug-filter configuration.
        activity_entries = [(line, False) for line in raw_lines]
        debug_entries = [(line, True) for line in raw_debug]
        display_lines = []
        i = j = 0
        while i < len(activity_entries) and j < len(debug_entries):
            if _timestamp(activity_entries[i][0]) <= _timestamp(debug_entries[j][0]):
                display_lines.append(activity_entries[i]); i += 1
            else:
                display_lines.append(debug_entries[j]); j += 1
        display_lines.extend(activity_entries[i:])
        display_lines.extend(debug_entries[j:])

        self._log_frozen_lines, display_lines = self._apply_freeze(
            self._log_scroll, display_lines, self._log_frozen_lines)

        wrapped = []
        for line, is_debug in display_lines:
            line = self._sanitize_line(line)
            if not line:
                wrapped.append(("", is_debug))
                continue
            while len(line) > line_width:
                wrapped.append((line[:line_width], is_debug))
                line = line[line_width:]
            wrapped.append((line, is_debug))

        # Clamp scroll so it never exceeds available history
        max_scroll = max(0, len(wrapped) - visible_rows)
        self._log_scroll = min(self._log_scroll, max_scroll)

        # 0 = tail (newest); positive = scrolled up
        start = max(0, len(wrapped) - visible_rows - self._log_scroll)
        view  = wrapped[start : start + visible_rows]

        for i, (line, is_debug) in enumerate(view):
            attr = theme.attr(self, "main_jjdlpdashboard_draw_log_tab_dim_3")
            if "Live now" in line or "Recording started" in line:
                attr = theme.attr(self, "main_jjdlpdashboard_draw_log_tab_live")
            elif "ERROR" in line or "Stall" in line or "STOPPED" in line or "Warning" in line:
                attr = theme.attr(self, "main_jjdlpdashboard_draw_log_tab_rec")
            elif "Info" in line:
                attr = theme.attr(self, "main_jjdlpdashboard_draw_log_tab_warn_1")
            self.safe_addstr(self.stdscr, y1 + 2 + i, x1, line, attr)
            if is_debug and highlighting_patterns:
                for pattern in highlighting_patterns:
                    for match in pattern.finditer(line):
                        if match.start() != match.end():
                            self.safe_addstr(self.stdscr, y1 + 2 + i,
                                x1 + match.start(), match.group(),
                                theme.attr(self, "main_jjdlpdashboard_draw_log_tab_match"))

        # Scroll indicator
        if max_scroll > 0:
            scroll_info = f" ↑{self._log_scroll}/{max_scroll} " if self._log_scroll else " (end) "
            self.safe_addstr(self.stdscr, y1 + 1, x2 - len(scroll_info) + 1,
                        scroll_info, theme.attr(self, "main_jjdlpdashboard_draw_log_tab_warn_2"))

    def _draw_pipe_tab_bar(self, y1, x1, x2) -> None:
        """Draw the '  Site: <site> <site> ...' switcher row shared by the
        Stdout/Stderr tabs."""
        tab_x = x1 + 1
        self.safe_addstr(self.stdscr, y1, x1, "  Site: ",
                    theme.attr(self, "main_jjdlpdashboard_draw_pipe_tab_bar_dim"))
        tab_x += 8
        for i, site in enumerate(self.sites):
            lbl = site.get_cached_config().get("site_label",
                              os.path.basename(site.config_path))
            label = f" {lbl} "
            attr  = (theme.attr(self, "main_jjdlpdashboard_draw_pipe_tab_bar_hilight")
                     if i == self.selected_site_idx
                     else theme.attr(self, "main_jjdlpdashboard_draw_pipe_tab_bar_chrome"))
            self.safe_addstr(self.stdscr, y1, tab_x, label, attr)
            tab_x += len(label) + 1

    def _draw_streamer_panel(self, y1, x1, y2, x2, site: Optional["SiteState"],
                             is_active: bool = False) -> None:
        """Draw the STREAMERS panel: 'All Streamers' plus one row per
        streamer on the currently selected site. Selection is tracked in
        self._streamer_panel_sel (0 = All Streamers, 1..N = streamer index).
        """
        border_pair = self.C_HILIGHT if is_active else self.C_DIM
        self.draw_box(self.stdscr, y1, x1, y2, x2, border_pair)
        title = " STREAMERS " if not is_active else " STREAMERS [  ] "
        self.safe_addstr(self.stdscr, y1, x1 + 2, title,
                    theme.attr(self, "main_jjdlpdashboard_draw_streamer_panel_border_pair", border_pair))

        streamers = list(site.dash_all_streamers) if site is not None else []
        # Clamp selection in case the streamer list shrank (e.g. one removed).
        self._streamer_panel_sel = min(self._streamer_panel_sel, len(streamers))

        rows = ["All Streamers"] + streamers
        visible_rows = (y2 - y1) - 1
        sel = self._streamer_panel_sel

        max_scroll = max(0, len(rows) - visible_rows)
        self._streamer_panel_scroll = min(self._streamer_panel_scroll, max_scroll)
        if sel < self._streamer_panel_scroll:
            self._streamer_panel_scroll = sel
        elif sel >= self._streamer_panel_scroll + visible_rows:
            self._streamer_panel_scroll = sel - visible_rows + 1

        start = self._streamer_panel_scroll
        view  = rows[start : start + visible_rows]
        row_width = max(1, (x2 - x1) - 2)

        for i, name in enumerate(view):
            idx = start + i
            label = name[:row_width].ljust(row_width)
            if idx == sel:
                attr = (theme.attr(self, "main_jjdlpdashboard_draw_streamer_panel_hilight")
                         | (curses.A_REVERSE if is_active else 0))
            else:
                attr = theme.attr(self, "main_jjdlpdashboard_draw_streamer_panel_dim")
            self.safe_addstr(self.stdscr, y1 + 1 + i, x1 + 1, label, attr)

    def _draw_pipe_tab(self, y1, x1, y2, x2, title: str, lines: List[str],
                       scroll: int = 0, is_active: bool = False) -> int:
        """Draw a pipe-output content box (STREAMERS panel is drawn
        separately). Returns the clamped scroll value."""
        sel_site = self.sites[self.selected_site_idx] if self.sites else None

        border_pair = self.C_HILIGHT if is_active else self.C_DIM
        self.draw_box(self.stdscr, y1, x1, y2, x2, border_pair)
        title_suffix = " [  ]" if is_active else ""
        self.safe_addstr(self.stdscr, y1, x1 + 2, f" {title}{title_suffix} ",
                    theme.attr(self, "main_jjdlpdashboard_draw_pipe_tab_border_pair", border_pair))

        if sel_site is None:
            return 0

        visible_rows = (y2 - y1) - 2
        line_width   = max(1, (x2 - x1) - 4)

        wrapped   = self._wrap_lines(lines, line_width)
        max_scroll = max(0, len(wrapped) - visible_rows)
        scroll    = min(scroll, max_scroll)

        start = max(0, len(wrapped) - visible_rows - scroll)
        view  = wrapped[start : start + visible_rows]

        for i, line in enumerate(view):
            self.safe_addstr(self.stdscr, y1 + 1 + i, x1 + 2, line,
                        theme.attr(self, "main_jjdlpdashboard_draw_pipe_tab_dim"))

        # Scroll indicator
        if max_scroll > 0:
            scroll_info = f" ↑{scroll}/{max_scroll} " if scroll else " (end) "
            self.safe_addstr(self.stdscr, y1, x2 - len(scroll_info) - 1,
                        scroll_info, theme.attr(self, "main_jjdlpdashboard_draw_pipe_tab_warn"))

        return scroll

    # Width of the STREAMERS panel on the Stdout/Stderr tabs.
    _STREAMER_PANEL_W = 24

    def draw_stdout_tab(self, y1, x1, y2, x2):
        sel_site = self.sites[self.selected_site_idx] if self.sites else None

        self._draw_pipe_tab_bar(y1, x1, x2)

        panel_x2 = min(x2, x1 + self._STREAMER_PANEL_W)
        self._draw_streamer_panel(y1 + 1, x1, y2, panel_x2, sel_site,
                                   is_active=(self._pipe_focus == "streamers"))

        streamers = list(sel_site.dash_all_streamers) if sel_site is not None else []
        sel_idx   = self._streamer_panel_sel

        lines = []
        show_all = False
        if sel_site is not None:
            if sel_idx == 0:
                # All Streamers — unchanged behaviour, including the
                # checker "Show All" toggle.
                show_all = sel_site.show_checker_stdout
                with sel_site.dash_lock:
                    raw = list(sel_site.dash_stdout_lines)
                if show_all:
                    # Strip the internal prefix tag before displaying
                    lines = [
                        (ln[len(_CHECKER_STDOUT_PREFIX):] if ln.startswith(_CHECKER_STDOUT_PREFIX) else ln)
                        for ln in raw
                    ]
                else:
                    # Only downloader output (no checker prefix)
                    lines = [ln for ln in raw if not ln.startswith(_CHECKER_STDOUT_PREFIX)]
            else:
                # One specific streamer — its own yt-dlp output only, no
                # checker JSON.
                streamer = streamers[sel_idx - 1] if sel_idx - 1 < len(streamers) else ""
                with sel_site.dash_lock:
                    lines = list(sel_site.dash_stdout_lines_by_streamer.get(streamer, ()))

        if sel_idx == 0:
            title = " STDOUT — Show All: ON  (Press A to toggle) " if show_all else " STDOUT — Show All: OFF (Press A to toggle) "
        else:
            title = f" STDOUT — {streamers[sel_idx - 1] if sel_idx - 1 < len(streamers) else ''} "
        self._stdout_frozen_lines, lines = self._apply_freeze(
            self._stdout_scroll, lines, self._stdout_frozen_lines)
        self._stdout_scroll = self._draw_pipe_tab(
            y1 + 1, panel_x2 + 1, y2, x2, title, lines, self._stdout_scroll,
            is_active=(self._pipe_focus == "content"))

    def draw_stderr_tab(self, y1, x1, y2, x2):
        sel_site = self.sites[self.selected_site_idx] if self.sites else None

        self._draw_pipe_tab_bar(y1, x1, x2)

        panel_x2 = min(x2, x1 + self._STREAMER_PANEL_W)
        self._draw_streamer_panel(y1 + 1, x1, y2, panel_x2, sel_site,
                                   is_active=(self._pipe_focus == "streamers"))

        streamers = list(sel_site.dash_all_streamers) if sel_site is not None else []
        sel_idx   = self._streamer_panel_sel

        lines = []
        show_all = False
        if sel_site is not None:
            if sel_idx == 0:
                show_all = sel_site.show_checker_stderr
                with sel_site.dash_lock:
                    raw = list(sel_site.dash_stderr_lines)
                if show_all:
                    lines = [
                        (ln[len(_CHECKER_STDERR_PREFIX):] if ln.startswith(_CHECKER_STDERR_PREFIX) else ln)
                        for ln in raw
                    ]
                else:
                    lines = [ln for ln in raw if not ln.startswith(_CHECKER_STDERR_PREFIX)]
            else:
                streamer = streamers[sel_idx - 1] if sel_idx - 1 < len(streamers) else ""
                with sel_site.dash_lock:
                    lines = list(sel_site.dash_stderr_lines_by_streamer.get(streamer, ()))

        if sel_idx == 0:
            title = " STDERR — Show All: ON  (Press A to toggle) " if show_all else " STDERR — Show All: OFF (Press A to toggle) "
        else:
            title = f" STDERR — {streamers[sel_idx - 1] if sel_idx - 1 < len(streamers) else ''} "
        self._stderr_frozen_lines, lines = self._apply_freeze(
            self._stderr_scroll, lines, self._stderr_frozen_lines)
        self._stderr_scroll = self._draw_pipe_tab(
            y1 + 1, panel_x2 + 1, y2, x2, title, lines, self._stderr_scroll,
            is_active=(self._pipe_focus == "content"))

    # ── EventSub tab ─────────────────────────────────────────────────────────
    def draw_eventsub_tab(self, y1, x1, y2, x2):
        self.draw_box(self.stdscr, y1, x1, y2, x2, self.C_CHROME)
        self.safe_addstr(self.stdscr, y1, x1 + 2, " TWITCH EVENTSUB ",
                    theme.attr(self, "main_jjdlpdashboard_draw_eventsub_tab_invhead_1"))

        row_y = y1 + 2
        for site in self.sites:
            if row_y >= y2 - 1:
                break
            lbl = site.get_cached_config().get("site_label",
                              os.path.basename(site.config_path))
            self.safe_addstr(self.stdscr, row_y, x1 + 2, f"-- {lbl} --",
                        theme.attr(self, "main_jjdlpdashboard_draw_eventsub_tab_warn"))
            row_y += 1

            es = site.eventsub_state
            if es is None:
                self.safe_addstr(self.stdscr, row_y, x1 + 4, "EventSub not available",
                            theme.attr(self, "main_jjdlpdashboard_draw_eventsub_tab_dim"))
                row_y += 2
                continue

            srv_status = es.get_server_status()
            last_notif, notif_total = es.get_notification_info()
            sub_ids = es.get_subscription_ids()

            rows = [
                ("Server", srv_status,
                 self.C_LIVE if "listening" in srv_status else
                 self.C_REC if "ERROR" in srv_status else self.C_DIM),
                ("Subscriptions",
                 f"{len(sub_ids)} active" if sub_ids else "none (subscribing...)",
                 self.C_LIVE if sub_ids else self.C_WARN),
                ("Notifications",
                 f"{notif_total} received" + (f"  last: {last_notif}" if last_notif else ""),
                 self.C_LIVE if notif_total else self.C_DIM),
            ]
            if site.eventsub is not None:
                cb = getattr(site.eventsub, "_initial_cfg", {}).get("twitch_callback_url", "")
                if cb:
                    rows.append(("Callback URL", cb, self.C_DIM))

            for label, val, cpair in rows:
                if row_y >= y2 - 1:
                    break
                self.safe_addstr(self.stdscr, row_y, x1 + 4,
                            f"{label:<16}", theme.attr(self, "main_jjdlpdashboard_draw_eventsub_tab_invhead_2"))
                self.safe_addstr(self.stdscr, row_y, x1 + 21, val, theme.attr(self, "main_jjdlpdashboard_draw_eventsub_tab_cpair", cpair))
                row_y += 1
            row_y += 1

    # ── Config tab ───────────────────────────────────────────────────────────
    def draw_config_tab(self, y1, x1, y2, x2):
        self.config_editor.draw_tab(self.stdscr, y1, x1, y2, x2)

    # ── Footer ────────────────────────────────────────────────────────────────
    def draw_footer(self):
        h, w = self.stdscr.getmaxyx()
        if self._mgmt_mode:
            action, site_idx = self._mgmt_mode
            site_lbl = os.path.basename(self.sites[site_idx].config_path)
            if action == "disable":
                hints = (f"  [{action.upper()} streamer on {site_lbl}]  "
                         f"\u2191\u2193: select  Enter: confirm  S: Skip this stream  Esc: Go back  ")
            elif action == "remove":
                hints = (f"  [{action.upper()} streamer on {site_lbl}]  "
                         f"\u2191\u2193: select  Enter: confirm  Esc: Go back  ")
            else:
                hints = (f"  [{action.upper()} streamer on {site_lbl}]  "
                         f"\u2191\u2193: select disabled  Type: new name  Enter: add/enable  Esc: Go back  ")
        else:
            current_tab = self.TABS[self.selected_tab]
            if current_tab in ("Log",):
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  UP: scroll up  DOWN: scroll down"
                         f"  C: Colors  Q: quit  ")
            elif current_tab == "Stdout":
                sel_site = self.sites[self.selected_site_idx] if self.sites else None
                show_all = sel_site.show_checker_stdout if sel_site else False
                show_label = "ON " if show_all else "OFF"
                focus_hint = ("UP/DOWN: select streamer" if self._pipe_focus == "streamers"
                               else "UP/DOWN: scroll")
                if self._streamer_panel_sel == 0:
                    hints = (f"  LEFT/RIGHT: switch tabs"
                             f"  [: prev site  ]: next site"
                             f"  Tab: switch panel  {focus_hint}"
                             f"  A: Show All [{show_label}]"
                             f"  C: Colors  Q: quit  ")
                else:
                    hints = (f"  LEFT/RIGHT: switch tabs"
                             f"  [: prev site  ]: next site"
                             f"  Tab: switch panel  {focus_hint}"
                             f"  C: Colors  Q: quit  ")
            elif current_tab == "Stderr":
                sel_site = self.sites[self.selected_site_idx] if self.sites else None
                show_all = sel_site.show_checker_stderr if sel_site else False
                show_label = "ON " if show_all else "OFF"
                focus_hint = ("UP/DOWN: select streamer" if self._pipe_focus == "streamers"
                               else "UP/DOWN: scroll")
                if self._streamer_panel_sel == 0:
                    hints = (f"  LEFT/RIGHT: switch tabs"
                             f"  [: prev site  ]: next site"
                             f"  Tab: switch panel  {focus_hint}"
                             f"  A: Show All [{show_label}]"
                             f"  C: Colors  Q: quit  ")
                else:
                    hints = (f"  LEFT/RIGHT: switch tabs"
                             f"  [: prev site  ]: next site"
                             f"  Tab: switch panel  {focus_hint}"
                             f"  C: Colors  Q: quit  ")
            elif current_tab == "Dashboard":
                sort_lbl = self.sort_manager.current_sort_label
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  A: add/enable streamer R: remove streamer D: disable streamer"
                         f"  S: Sort"
                         f"  C: Colors  Q: quit  ")
            elif current_tab == "Config":
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  Tab: Next Panel"
                         f"  G: Changelog"
                         f"  C: Colors  N: Theme Manager  Q: quit  ")
            elif current_tab == "File Manager":
                hints = (f"  \u2191\u2193: select  Space: show folder"
                         f"  DEL: delete  S: sort  T: toggle trash"
                         f"  C: Colors  Q: quit  ")
            else:
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  Tab: Next Panel"
                         f"  C: Colors  Q: quit  ")
        self.safe_addstr(self.stdscr, h - 1, 0,
                    hints.ljust(w - 1)[:w - 1],
                    theme.attr(self, "main_jjdlpdashboard_draw_footer_invhead"))

    # ── Streamer management overlay ───────────────────────────────────────────
    def _mgmt_enabled_streamers(self, site) -> list:
        """Return enabled (non-blocked) streamers for the given site."""
        with site.dash_lock:
            all_s   = list(site.dash_all_streamers)
            blocked = set(site.dash_blocked)
        return [s for s in all_s if s not in blocked]

    def _mgmt_disabled_streamers(self, site) -> list:
        """Return streamers that are in both [Streamers] and [Block] (disabled, not removed)."""
        with site.dash_lock:
            all_s   = set(site.dash_all_streamers)
            blocked = sorted(site.dash_blocked)
        return [s for s in blocked if s in all_s]

    def _mgmt_removable_streamers(self, site) -> list:
        """Return every streamer in [Streamers], enabled or disabled (for the REMOVE popup)."""
        with site.dash_lock:
            all_s = list(site.dash_all_streamers)
        return all_s

    def draw_mgmt_overlay(self):
        if not self._mgmt_mode:
            return
        h, w = self.stdscr.getmaxyx()
        action, site_idx = self._mgmt_mode
        site = self.sites[site_idx]
        site_lbl = site.get_cached_config().get("site_label",
                                                os.path.basename(site.config_path))

        # DISABLE gets a wider box than ADD/REMOVE to fit its longer legend
        # (" ...  S: Skip this stream  Esc: Go back ").
        max_box_w = 68 if action == "disable" else 60
        box_h, box_w = min(20, h - 4), min(max_box_w, w - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        # Fill background
        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1),
                        theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_normal_1"))

        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_WARN)
        title = f" {action.upper()} STREAMER "
        self.safe_addstr(self.stdscr, by1, bx1 + 2, title,
                    theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_warn_1"))
        self.safe_addstr(self.stdscr, by1 + 1, bx1 + 2,
                    f"Site: {site_lbl}", theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_dim_1"))

        if action in ("disable", "remove"):
            # ── List-picker mode: arrow up/down to select a streamer ──────────
            # REMOVE should list every streamer (enabled or disabled); DISABLE
            # only makes sense for streamers that aren't already disabled.
            if action == "remove":
                enabled = self._mgmt_removable_streamers(site)
                with site.dash_lock:
                    blocked_set = set(site.dash_blocked)
            else:
                enabled = self._mgmt_enabled_streamers(site)
                blocked_set = set()

            # Result message (shown after an action completes)
            if self._mgmt_result:
                self.safe_addstr(self.stdscr, by1 + 2, bx1 + 2,
                            self._mgmt_result[:box_w - 4],
                            theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_live_1"))

            if not enabled:
                empty_msg = "No streamers." if action == "remove" else "No enabled streamers."
                self.safe_addstr(self.stdscr, by1 + 3, bx1 + 2,
                            empty_msg,
                            theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_dim_2"))
                self.safe_addstr(self.stdscr, by2, bx1 + 2,
                            " Esc: Go back ",
                            theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_invhead_1"))
                return

            # Clamp selection
            self._mgmt_sel = max(0, min(self._mgmt_sel, len(enabled) - 1))

            list_top    = by1 + 3
            list_bottom = by2 - 1          # leave 1 row for legend
            visible     = list_bottom - list_top

            # Scroll to keep selection visible
            if self._mgmt_sel < self._mgmt_scroll:
                self._mgmt_scroll = self._mgmt_sel
            elif self._mgmt_sel >= self._mgmt_scroll + visible:
                self._mgmt_scroll = self._mgmt_sel - visible + 1

            for i in range(self._mgmt_scroll,
                           min(len(enabled), self._mgmt_scroll + visible)):
                s      = enabled[i]
                row_y  = list_top + (i - self._mgmt_scroll)
                is_sel = (i == self._mgmt_sel)
                prefix = "> " if is_sel else "  "
                attr   = (theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_hilight_1")
                          if is_sel else theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_normal_2"))
                suffix = " (disabled)" if s in blocked_set else ""
                self.safe_addstr(self.stdscr, row_y, bx1 + 2,
                            (prefix + s + suffix)[:box_w - 4], attr)

            if action == "disable":
                legend = " \u2191\u2193: select  Enter: confirm  S: Skip this stream  Esc: Go back "
            else:
                legend = " \u2191\u2193: select  Enter: confirm  Esc: Go back "
            self.safe_addstr(self.stdscr, by2, bx1 + 2, legend,
                        theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_invhead_2"))

        else:
            # ── ADD mode: disabled-streamer list + text input for new names ───
            disabled = self._mgmt_disabled_streamers(site)

            # Result message
            if self._mgmt_result:
                self.safe_addstr(self.stdscr, by1 + 2, bx1 + 2,
                            self._mgmt_result[:box_w - 4],
                            theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_live_2"))

            # Fixed rows at the bottom for text input + legend
            input_row  = by2 - 2
            legend_row = by2

            # Row layout (from top):
            #   by1+1 : site label
            #   by1+2 : result message
            #   by1+3 : "Re-enable disabled:" header
            #   by1+4 : list starts
            list_header = by1 + 3
            list_top    = by1 + 4
            list_bottom = input_row - 2   # one blank row gap above input
            visible     = max(0, list_bottom - list_top)

            if disabled:
                self.safe_addstr(self.stdscr, list_header, bx1 + 2,
                            "Re-enable disabled:",
                            theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_chrome"))

                # Clamp selection (-1 = text input focused, >=0 = list item)
                if self._mgmt_sel >= 0:
                    self._mgmt_sel = min(self._mgmt_sel, len(disabled) - 1)

                    # Scroll to keep selection visible
                    if self._mgmt_sel < self._mgmt_scroll:
                        self._mgmt_scroll = self._mgmt_sel
                    elif self._mgmt_sel >= self._mgmt_scroll + visible:
                        self._mgmt_scroll = self._mgmt_sel - visible + 1

                for i in range(self._mgmt_scroll,
                               min(len(disabled), self._mgmt_scroll + visible)):
                    s      = disabled[i]
                    row_y  = list_top + (i - self._mgmt_scroll)
                    is_sel = (self._mgmt_sel == i)
                    prefix = "> " if is_sel else "  "
                    attr   = (theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_hilight_2")
                              if is_sel else theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_dim_3"))
                    suffix = "  (skipping this stream)" if s in site.skip_disabled else ""
                    self.safe_addstr(self.stdscr, row_y, bx1 + 2,
                                (prefix + s + suffix)[:box_w - 4], attr)
            else:
                self.safe_addstr(self.stdscr, list_top, bx1 + 2,
                            "No disabled streamers.",
                            theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_dim_4"))

            # Text input (always shown at bottom)
            self.safe_addstr(self.stdscr, input_row, bx1 + 2, "New username:",
                        theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_warn_2"))
            input_attr = (theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_hilight_3")
                          if self._mgmt_sel == -1
                          else theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_normal_3"))
            self.safe_addstr(self.stdscr, input_row, bx1 + 16,
                        (self._mgmt_buf + "_")[:box_w - 18], input_attr)

            if disabled:
                legend = " \u2191\u2193: select disabled  Enter: add/enable  Esc: Go back "
            else:
                legend = " Enter: add  Esc: Go back "
            self.safe_addstr(self.stdscr, legend_row, bx1 + 2,
                        legend[:box_w - 4],
                        theme.attr(self, "main_jjdlpdashboard_draw_mgmt_overlay_invhead_3"))

    # ── Full screen refresh ───────────────────────────────────────────────────
    def _open_debug_filter_popup(self) -> None:
        self._debug_filter_popup_open = True
        self._debug_filter_sel = 0
        self._debug_filter_error = ""

    def _write_filtered_debug_view(self) -> None:
        """Write the selected site's regex-matched debug lines to a new file."""
        if not self.sites:
            self._debug_filter_error = "No site is selected."
            return
        filter_entries = [entry for entry in self._debug_filter_patterns
                          if entry.get("mode", "filter_highlight") != "highlight_only"]
        if not filter_entries:
            self._debug_filter_error = "Add a Filter (not Highlight-only) filter before exporting."
            return

        try:
            patterns = [_re.compile(entry["pattern"]) for entry in filter_entries]
        except _re.error as exc:
            self._debug_filter_error = f"Invalid regex: {exc}"
            return

        site = self.sites[self.selected_site_idx]
        with site.dash_lock:
            matched_lines = [line for line in site.dash_debug_lines
                             if any(pattern.search(line) for pattern in patterns)]

        cfg = site.get_cached_config()
        debug_path = get_debug_log_path(cfg)
        export_dir = os.path.dirname(os.path.abspath(debug_path)) or os.getcwd()
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        export_path = os.path.join(export_dir, f"debug-filtered-{stamp}.log")
        try:
            os.makedirs(export_dir, exist_ok=True)
            with open(export_path, "x", encoding="utf-8", newline="\n") as f:
                for line in matched_lines:
                    f.write(line.rstrip("\r\n") + "\n")
        except OSError as exc:
            self._debug_filter_error = f"Could not write export: {exc}"
            return

        self._debug_filter_error = f"Wrote {len(matched_lines)} lines: {export_path}"

    def _open_debug_filter_entry(self, edit_index: "int | None") -> None:
        """Open the CREATE/EDIT DEBUG FILTER sub-popup.

        edit_index=None opens it blank for creating a new filter; otherwise
        it's pre-filled with the pattern/mode of the filter at that index
        so the user can edit it in place.
        """
        if edit_index is None:
            self._debug_filter_buf = ""
            self._debug_filter_mode = DEBUG_FILTER_MODES[0]
        else:
            entry = self._debug_filter_patterns[edit_index]
            self._debug_filter_buf = entry["pattern"]
            self._debug_filter_mode = entry.get("mode", DEBUG_FILTER_MODES[0])
        self._debug_filter_cursor = len(self._debug_filter_buf)
        self._debug_filter_edit_index = edit_index
        self._debug_filter_entry_sel = 0
        self._debug_filter_error = ""
        self._debug_filter_entry_open = True

    def _handle_debug_filter_key(self, key) -> bool:
        """Handle the filter-list popup (not the regex text editor)."""
        count = len(self._debug_filter_patterns)
        export_row = count + 1   # 0=create, 1..N=filters, N+1=export
        if key in (27, ord('q'), ord('Q')):
            self._debug_filter_popup_open = False
        elif key in (curses.KEY_UP, ord('k')):
            self._debug_filter_sel = max(0, self._debug_filter_sel - 1)
        elif key in (curses.KEY_DOWN, ord('j')):
            self._debug_filter_sel = min(export_row, self._debug_filter_sel + 1)
        elif key in (curses.KEY_DC, ord('d'), ord('D')) and 1 <= self._debug_filter_sel <= count:
            del self._debug_filter_patterns[self._debug_filter_sel - 1]
            self._debug_filter_sel = min(self._debug_filter_sel, len(self._debug_filter_patterns) + 1)
            self._log_scroll = 0
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if self._debug_filter_sel == 0:
                self._open_debug_filter_entry(None)
            elif self._debug_filter_sel == export_row:
                self._write_filtered_debug_view()
            elif 1 <= self._debug_filter_sel <= count:
                # Select an existing filter to reopen it for editing.
                self._open_debug_filter_entry(self._debug_filter_sel - 1)
        return True

    def _handle_debug_filter_entry_key(self, key) -> bool:
        """Handle the CREATE/EDIT DEBUG FILTER sub-popup: a regex text field
        plus a per-filter Mode multi-select (Left/Right to switch)."""
        cur = self._debug_filter_cursor
        if key == 27:
            self._debug_filter_entry_open = False
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            self._debug_filter_entry_sel = 1 - self._debug_filter_entry_sel
        elif key in (curses.KEY_BACKSPACE, 127, 8) and self._debug_filter_entry_sel == 0:
            if cur > 0:
                self._debug_filter_buf = self._debug_filter_buf[:cur - 1] + self._debug_filter_buf[cur:]
                self._debug_filter_cursor = cur - 1
        elif key == curses.KEY_DC and self._debug_filter_entry_sel == 0 and cur < len(self._debug_filter_buf):
            self._debug_filter_buf = self._debug_filter_buf[:cur] + self._debug_filter_buf[cur + 1:]
        elif key == curses.KEY_LEFT:
            if self._debug_filter_entry_sel == 1:
                idx = DEBUG_FILTER_MODES.index(self._debug_filter_mode)
                self._debug_filter_mode = DEBUG_FILTER_MODES[(idx - 1) % len(DEBUG_FILTER_MODES)]
            elif cur > 0:
                self._debug_filter_cursor = cur - 1
        elif key == curses.KEY_RIGHT:
            if self._debug_filter_entry_sel == 1:
                idx = DEBUG_FILTER_MODES.index(self._debug_filter_mode)
                self._debug_filter_mode = DEBUG_FILTER_MODES[(idx + 1) % len(DEBUG_FILTER_MODES)]
            elif cur < len(self._debug_filter_buf):
                self._debug_filter_cursor = cur + 1
        elif key == curses.KEY_HOME and self._debug_filter_entry_sel == 0:
            self._debug_filter_cursor = 0
        elif key == curses.KEY_END and self._debug_filter_entry_sel == 0:
            self._debug_filter_cursor = len(self._debug_filter_buf)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            expression = self._debug_filter_buf.strip()
            try:
                _re.compile(expression)
            except _re.error as exc:
                self._debug_filter_error = f"Invalid regex: {exc}"
            else:
                if expression:
                    entry = {"pattern": expression, "mode": self._debug_filter_mode}
                    if self._debug_filter_edit_index is not None:
                        self._debug_filter_patterns[self._debug_filter_edit_index] = entry
                    else:
                        self._debug_filter_patterns.append(entry)
                    self._log_scroll = 0
                self._debug_filter_entry_open = False
        elif 32 <= key < 127 and self._debug_filter_entry_sel == 0:
            self._debug_filter_buf = self._debug_filter_buf[:cur] + chr(key) + self._debug_filter_buf[cur:]
            self._debug_filter_cursor = cur + 1
            self._debug_filter_error = ""
        return True

    def draw_debug_filter_popup(self) -> None:
        h, w = self.stdscr.getmaxyx()
        box_w = min(max(58, min(96, w - 4)), w - 4)
        box_h = min(max(10, len(self._debug_filter_patterns) + 8), h - 4)
        by1, bx1 = max(0, (h - box_h) // 2), max(0, (w - box_w) // 2)
        by2, bx2 = by1 + box_h, bx1 + box_w
        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1), theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_normal"))
        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_CHROME)
        self.safe_addstr(self.stdscr, by1, bx1 + 2, " DEBUG LOG FILTERS ", theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_title"))
        self.safe_addstr(self.stdscr, by1 + 1, bx1 + 2, "Show matching debug lines only; regular log lines are unaffected.", theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_text"))
        create_attr = theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_selected") if self._debug_filter_sel == 0 else theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_text")
        self.safe_addstr(self.stdscr, by1 + 2, bx1 + 2, "> Create new filter" if self._debug_filter_sel == 0 else "  Create new filter", create_attr)
        max_rows = max(0, box_h - 7)
        for index, entry in enumerate(self._debug_filter_patterns[:max_rows]):
            attr = theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_selected") if self._debug_filter_sel == index + 1 else theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_text")
            prefix = ">" if self._debug_filter_sel == index + 1 else " "
            tag = DEBUG_FILTER_MODE_TAGS.get(entry.get("mode", "filter_highlight"), "F+H")
            self.safe_addstr(self.stdscr, by1 + 3 + index, bx1 + 2, f"{prefix} [{tag}] {entry['pattern']}"[:box_w - 4], attr)
        export_y = by1 + 3 + min(len(self._debug_filter_patterns), max_rows)
        export_attr = theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_selected") if self._debug_filter_sel == len(self._debug_filter_patterns) + 1 else theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_text")
        self.safe_addstr(self.stdscr, export_y, bx1 + 2, "Write filtered view to file", export_attr)
        if self._debug_filter_error:
            self.safe_addstr(self.stdscr, by2 - 1, bx1 + 2, self._debug_filter_error[:box_w - 4], theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_error"))
        self.safe_addstr(self.stdscr, by2, bx1 + 2, "Enter: select/edit  Up/Down: select  D/Del: remove  Esc: close"[:box_w - 4], theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_legend"))

    def draw_debug_filter_entry_popup(self) -> None:
        h, w = self.stdscr.getmaxyx()
        box_w, box_h = min(70, w - 4), min(9, h - 4)
        by1, bx1 = (h - box_h) // 2, (w - box_w) // 2
        by2, bx2 = by1 + box_h, bx1 + box_w
        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1), theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_normal"))
        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_CHROME)
        editing = self._debug_filter_edit_index is not None
        title = " EDIT DEBUG FILTER " if editing else " CREATE DEBUG FILTER "
        self.safe_addstr(self.stdscr, by1, bx1 + 2, title, theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_title"))
        self.safe_addstr(self.stdscr, by1 + 1, bx1 + 2, "Regex expression:", theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_text"))
        text_attr = (theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_selected")
                     if self._debug_filter_entry_sel == 0
                     else theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_text"))
        if self._debug_filter_entry_sel == 0:
            display = self._debug_filter_buf[:self._debug_filter_cursor] + "_" + self._debug_filter_buf[self._debug_filter_cursor:]
        else:
            display = self._debug_filter_buf
        self.safe_addstr(self.stdscr, by1 + 3, bx1 + 2, display[:box_w - 4], text_attr)

        # Per-filter Mode multi-select: Left/Right arrows cycle through the
        # three modes. This setting is saved with this filter only.
        mode_attr = (theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_selected")
                     if self._debug_filter_entry_sel == 1
                     else theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_text"))
        mode_label = DEBUG_FILTER_MODE_LABELS[self._debug_filter_mode]
        mode_line = f"Mode: < {mode_label} >"
        self.safe_addstr(self.stdscr, by1 + 5, bx1 + 2, mode_line[:box_w - 4], mode_attr)

        if self._debug_filter_error:
            self.safe_addstr(self.stdscr, by1 + 6, bx1 + 2, self._debug_filter_error[:box_w - 4], theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_error"))
        legend = "Enter: save  Up/Down: field  Left/Right: mode  Esc: cancel"
        self.safe_addstr(self.stdscr, by2, bx1 + 2, legend[:box_w - 4], theme.attr(self, "main_jjdlpdashboard_draw_debug_filter_popup_legend"))

    def refresh_screen(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        self.stdscr.bkgd(" ", theme.attr(self, "main_jjdlpdashboard_refresh_screen_normal"))

        # Logo (6 lines tall, starts at row 1)
        self.draw_logo(1, 2)

        # System time top-right
        sys_time_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        time_x = w - len(sys_time_str) - 3
        self.safe_addstr(self.stdscr, 1, time_x, sys_time_str,
                    theme.attr(self, "main_jjdlpdashboard_refresh_screen_chrome_1"))

        # Disk-rate sparkline — fills the gap between the logo and the clock,
        # same height as the logo (rows 1-6). Graph.draw also renders the
        # "max …" scale label above the graph's right edge.
        self.graph.tick()
        _logo_w = max(len(_l) for _l in ASCII_LOGO)
        _graph_x0 = 2 + _logo_w + 3
        _graph_x1 = time_x - 2
        _graph_y1 = 1 + len(ASCII_LOGO) - 1
        if _graph_x1 > _graph_x0:
            self.graph.draw(1, _graph_x0, _graph_x1, _graph_y1)

        # Track the next available row on the right side
        next_right_row = 2

        # Update Available indicator (below system time)
        with self.app.update_available_lock:
            if self.app.update_available:
                update_str = "Update Available"
                self.safe_addstr(self.stdscr, next_right_row, w - len(update_str) - 3, update_str,
                            theme.attr(self, "main_jjdlpdashboard_refresh_screen_warn"))
                next_right_row += 1
        
        # App version indicator (Below Update Available, or directly below time)
        version_str = f"v{__version__}"
        self.safe_addstr(self.stdscr, next_right_row, w - len(version_str) - 3, version_str,
                    theme.attr(self, "main_jjdlpdashboard_refresh_screen_dim"))

        # Christmas Day easter egg — tree below the version indicator, shown
        # only on Dec 24th and 25th. Right-aligned to the same margin as the
        # version number above it.
        if self._is_christmas_day():
            self.draw_christmas_easter_egg(next_right_row + 1, w - 3 - 7)

        # Blank line after logo (row 7), then tab bar at row 8
        # (Logo occupies rows 1-6, row 7 is blank, tabs at row 8)
        self.draw_tabs(8, 2)

        # Separator
        self.safe_addstr(self.stdscr, 9, 1, "-" * (w - 2), theme.attr(self, "main_jjdlpdashboard_refresh_screen_chrome_2"))

        # Content area starts at row 10
        content_y1 = 10
        content_y2 = h - 2

        # Get the name of the currently selected tab
        current_tab_name = self.TABS[self.selected_tab]

        # The Log tab uses the whole available width so its text can be
        # selected and copied cleanly. Other tabs retain the system sidebar.
        if current_tab_name == "Log":
            content_x2 = w - 2
            _t_system_panel = 0.0
        else:
            sidebar_w  = 28
            sidebar_x1 = w - sidebar_w - 1
            sidebar_x2 = w - 2
            _t0 = time.time()
            self.draw_system_panel(content_y1, sidebar_x1, content_y2, sidebar_x2)
            _t_system_panel = time.time() - _t0
            content_x2 = sidebar_x1 - 1

        _t0 = time.time()
        if current_tab_name == "Dashboard":
            self.draw_dashboard_tab(content_y1, 1, content_y2, content_x2)
        elif current_tab_name == "Log":
            self.draw_log_tab(content_y1, 1, content_y2, content_x2)
        elif current_tab_name == "Stdout":
            self.draw_stdout_tab(content_y1, 1, content_y2, content_x2)
        elif current_tab_name == "Stderr":
            self.draw_stderr_tab(content_y1, 1, content_y2, content_x2)
        elif current_tab_name == "EventSub":
            self.draw_eventsub_tab(content_y1, 1, content_y2, content_x2)
        elif current_tab_name == "Config":
            self.draw_config_tab(content_y1, 1, content_y2, content_x2)
        elif current_tab_name == "File Manager":
            # Poll at the tab's normal (snappier) cadence while it's focused;
            # draw_system_panel() also polls at a slower cadence so the
            # sidebar rate stays fresh even when this tab isn't active.
            self.file_manager.maybe_poll()
            self.file_manager.draw(self.stdscr, content_y1, 1, content_y2, content_x2)
        _t_main_tab = time.time() - _t0

        self.draw_footer()

        if self._mgmt_mode:
            self.draw_mgmt_overlay()

        if self._debug_filter_popup_open:
            self.draw_debug_filter_popup()

        if self._debug_filter_entry_open:
            self.draw_debug_filter_entry_popup()

        # Sort popup — drawn on top of everything else.
        if self.sort_manager.popup_open:
            self.sort_manager.draw_popup(self.stdscr)

        # File Manager popups (sort / File Options / Fixup) — drawn on top when active.
        if hasattr(self, 'file_manager'):
            if self.file_manager.any_popup_open() or time.time() < self.file_manager._sort_popup_until:
                self.file_manager.draw_popups(self.stdscr)

        # Theme popup — drawn on top of sort/file-manager popups if both somehow open.
        if self.theme_manager.popup_open:
            self.theme_manager.draw_popup(self.stdscr)

        # Changelog popup — drawn on top of sort popup if both somehow open.
        if self._changelog_popup_open:
            self.draw_changelog_popup()

        # Bake-to-source popup (dev feature) — drawn above the changelog.
        if self._bake_popup_open:
            self.draw_bake_popup()

        # Graph-knob popup (dev feature) — drawn above the bake popup.
        if self.graph.popup_open:
            self.graph.draw_popup()

        # Transient scheme-list popup ('c' key) — drawn above the other popups
        # but below the exit-confirm / failure alerts.
        self.draw_scheme_popup()

        # Exit-confirmation popup — drawn on top of everything else.
        if self._exit_confirm_open:
            self.draw_exit_confirm_popup()

        # Recording-failure alert — drawn last, on top of absolutely
        # everything (including the exit-confirmation popup), since it's
        # the most urgent thing this app can show.
        if self._write_failure_alert_open:
            self.draw_write_failure_alert()

        self.stdscr.refresh()

        # Log timing every 100 frames (~5 seconds at 20fps)
        if self.tick % 100 == 0:
            dbg(
                f"[PERF][refresh_screen] tick={self.tick} tab={current_tab_name!r} "
                f"system_panel_ms={_t_system_panel*1000:.2f} "
                f"main_tab_ms={_t_main_tab*1000:.2f}"
            )

    # ── Input handling ────────────────────────────────────────────────────────
    def handle_key(self, key) -> bool:
        """Returns False to quit."""
        # Recording-failure alert intercepts all keys while open — it's
        # drawn on top of everything else, including the exit-confirm popup.
        if self._write_failure_alert_open:
            return self._handle_write_failure_alert_key(key)

        # Exit-confirmation popup intercepts all keys while open.
        if self._exit_confirm_open:
            return self._handle_exit_confirm_key(key)

        if self._debug_filter_popup_open:
            if self._debug_filter_entry_open:
                return self._handle_debug_filter_entry_key(key)
            return self._handle_debug_filter_key(key)

        # Changelog popup intercepts all keys while open.
        if self._changelog_popup_open:
            if key in (ord('q'), ord('Q'), 27,            # Q / Esc → close
                       ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
                self._changelog_popup_open = False
            elif key in (curses.KEY_UP, ord('k')):
                self._changelog_scroll = max(0, self._changelog_scroll - 1)
            elif key in (curses.KEY_DOWN, ord('j')):
                self._changelog_scroll += 1   # clamped in draw method
            elif key == curses.KEY_PPAGE:
                h, _ = self.stdscr.getmaxyx()
                page = max(1, min(h - 4, 40) - 3)
                self._changelog_scroll = max(0, self._changelog_scroll - page)
            elif key == curses.KEY_NPAGE:
                h, _ = self.stdscr.getmaxyx()
                page = max(1, min(h - 4, 40) - 3)
                self._changelog_scroll += page   # clamped in draw method
            return True

        # Bake-to-source popup intercepts all keys while open.
        if self._bake_popup_open:
            if key in (ord('q'), ord('Q'), 27,
                       ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
                self._bake_popup_open = False
            return True

        # Graph-knob popup intercepts all keys while open.
        if self.graph.popup_open:
            return self.graph.handle_key(key)

        if self._mgmt_mode:
            return self._handle_mgmt_key(key)

        # Theme popup intercepts all keys while open.
        if self.theme_manager.popup_open:
            return self.theme_manager.handle_key(key)

        # Sort popup intercepts all keys while open.
        if self.sort_manager.popup_open:
            return self.sort_manager.handle_key(key)

        current_tab_name = self.TABS[self.selected_tab]
        if current_tab_name == "Config":
            # Pass keys to ConfigEditor first. But still handle global site switching:
            if key not in (ord(']'), curses.KEY_NPAGE, ord('['), curses.KEY_PPAGE):
                dbg(f"[CONFIG] main.handle_key() dispatch key={key} tab={current_tab_name!r}")
                if self.config_editor.handle_key(key):
                    dbg(f"[CONFIG] main.handle_key() config_editor consumed key={key}")
                    return True
                dbg(f"[CONFIG] main.handle_key() config_editor did not consume key={key}")

        if current_tab_name == "File Manager":
            # Pass keys to FileManagerTab first. Still allow global tab/site switching.
            excluded = [ord(']'), ord('[')]
            if not self.file_manager._move_filename_open:
                excluded += [curses.KEY_LEFT, ord('h'), curses.KEY_RIGHT, ord('l')]
            if key not in excluded:
                if self.file_manager.handle_key(key):
                    return True

        if key in (ord('q'), ord('Q'), 27):
            self._open_exit_confirm()
        elif key in (curses.KEY_RIGHT, ord('l')):
            self.selected_tab = (self.selected_tab + 1) % len(self.TABS)
        elif key in (curses.KEY_LEFT, ord('h')):
            self.selected_tab = (self.selected_tab - 1) % len(self.TABS)
        elif key in (ord(']'), curses.KEY_NPAGE) and current_tab_name == "Log":
            # Log tab's site selector is independent and includes "All":
            # -1 ("All") .. len(sites)-1.
            n = len(self.sites)
            self._log_site_idx = (self._log_site_idx + 2) % (n + 1) - 1
            self._log_scroll = 0
            self._log_frozen_lines = None
        elif key in (ord('['), curses.KEY_PPAGE) and current_tab_name == "Log":
            n = len(self.sites)
            self._log_site_idx = (self._log_site_idx % (n + 1)) - 1
            self._log_scroll = 0
            self._log_frozen_lines = None
        elif key in (ord(']'), curses.KEY_NPAGE):   # next site (config/stdout/stderr tabs)
            self.selected_site_idx = (self.selected_site_idx + 1) % max(1, len(self.sites))
            # Reset scroll when switching sites
            self._stdout_scroll = self._stderr_scroll = 0
            self._streamer_panel_sel = self._streamer_panel_scroll = 0
            self.config_editor.notify_site_changed(self.selected_site_idx)
        elif key in (ord('['), curses.KEY_PPAGE):   # prev site
            self.selected_site_idx = (self.selected_site_idx - 1) % max(1, len(self.sites))
            # Reset scroll when switching sites
            self._stdout_scroll = self._stderr_scroll = 0
            self._streamer_panel_sel = self._streamer_panel_scroll = 0
            self.config_editor.notify_site_changed(self.selected_site_idx)
        elif key == ord('\t') and current_tab_name in ("Stdout", "Stderr"):
            # Cycle focus between the STREAMERS panel and the content pane,
            # the same way Tab cycles panels on the Config tab.
            self._pipe_focus = "content" if self._pipe_focus == "streamers" else "streamers"
        elif key == curses.KEY_UP:
            tab = self.TABS[self.selected_tab]
            if tab == "Log":
                self._log_scroll += 1
            elif tab in ("Stdout", "Stderr"):
                if self._pipe_focus == "streamers":
                    self._streamer_panel_sel = max(0, self._streamer_panel_sel - 1)
                    # A different streamer buffer starts back at live-following.
                    self._stdout_scroll = self._stderr_scroll = 0
                elif tab == "Stdout":
                    self._stdout_scroll += 1
                else:
                    self._stderr_scroll += 1
        elif key == curses.KEY_DOWN:
            tab = self.TABS[self.selected_tab]
            if tab == "Log":
                self._log_scroll = max(0, self._log_scroll - 1)
            elif tab in ("Stdout", "Stderr"):
                if self._pipe_focus == "streamers":
                    sel_site = self.sites[self.selected_site_idx] if self.sites else None
                    max_idx  = len(sel_site.dash_all_streamers) if sel_site is not None else 0
                    self._streamer_panel_sel = min(max_idx, self._streamer_panel_sel + 1)
                    # A different streamer buffer starts back at live-following.
                    self._stdout_scroll = self._stderr_scroll = 0
                elif tab == "Stdout":
                    self._stdout_scroll = max(0, self._stdout_scroll - 1)
                else:
                    self._stderr_scroll = max(0, self._stderr_scroll - 1)
        elif key == ord('k'):   # Log tab only — Stdout/Stderr use Tab + arrow keys
            if self.TABS[self.selected_tab] == "Log":
                self._log_scroll += 1
        elif key == ord('j'):
            if self.TABS[self.selected_tab] == "Log":
                self._log_scroll = max(0, self._log_scroll - 1)
        elif key in (ord('a'), ord('A')):
            if current_tab_name == "Log" and self.sites:
                # Debug toggle only applies while "All" is selected — when a
                # specific site is selected, the toggle/debug output are
                # hidden entirely (see draw_log_tab), so 'A' is a no-op then.
                if self._log_site_idx == -1:
                    self._log_all_show_debug = not self._log_all_show_debug
                    self._log_scroll = 0
                    self._log_frozen_lines = None
            elif current_tab_name == "Stdout" and self.sites:
                # "Show All" only means anything on the All Streamers view —
                # a specific streamer never has checker JSON to show.
                if self._streamer_panel_sel == 0:
                    sel = self.sites[self.selected_site_idx]
                    sel.show_checker_stdout = not sel.show_checker_stdout
                    self._stdout_scroll = 0
            elif current_tab_name == "Stderr" and self.sites:
                if self._streamer_panel_sel == 0:
                    sel = self.sites[self.selected_site_idx]
                    sel.show_checker_stderr = not sel.show_checker_stderr
                    self._stderr_scroll = 0
            else:
                self._start_mgmt("add")
        elif key in (ord('f'), ord('F')):
            if current_tab_name == "Log":
                self._open_debug_filter_popup()
        elif key in (ord('r'), ord('R')):
            self._start_mgmt("remove")
        elif key in (ord('d'), ord('D')):
            self._start_mgmt("disable")
        elif key in (ord('c'), ord('C')):
            self.randomize_colors()
        elif key in (ord('n'), ord('N')):
            self.theme_manager.open_popup()
        elif key in (ord('w'), ord('W')):
            # Dev feature: bake the current base-scheme + per-site
            # customizations into theme.py (the only file this writes to).
            self.open_bake_popup()
        elif key in (ord('s'), ord('S')):
            if current_tab_name == "Dashboard":
                self.sort_manager.open_popup()
        elif key in (ord('p'), ord('P')):
            # Dev feature: tune the top-bar disk-rate graph knobs live, or
            # hot-reload graph.py from disk ("Reload graph.py" row).
            self.graph.toggle_popup()
        elif key in (ord('g'), ord('G')):
            self.open_changelog_popup()
        return True

    def _start_mgmt(self, action: str):
        if not self.sites:
            return
        self._mgmt_mode   = (action, self.selected_site_idx)
        self._mgmt_buf    = ""
        self._mgmt_result = ""
        # For add: -1 = text input focused; >=0 = disabled-list item selected.
        # For disable/remove: start at first item.
        self._mgmt_sel    = -1 if action == "add" else 0
        self._mgmt_scroll = 0

    def _clear_skip_disabled(self, site, username: str) -> None:
        """Drop *username* from site.skip_disabled (and persist), if present.

        Called whenever a streamer is manually re-enabled or removed, so a
        stale skip_disabled entry can't later cause the checker to auto
        un-block a streamer the user has since dealt with some other way.
        """
        username = username.strip().lower()
        with site.dash_lock:
            if username not in site.skip_disabled:
                return
            site.skip_disabled.discard(username)
            skip_snapshot = set(site.skip_disabled)
        _save_skip_disabled(site.config_path, skip_snapshot)

    def _handle_mgmt_key(self, key) -> bool:
        action, site_idx = self._mgmt_mode
        site = self.sites[site_idx]

        if action in ("disable", "remove"):
            # ── List-picker mode ───────────────────────────────────────────────
            list_fn = (self._mgmt_removable_streamers if action == "remove"
                       else self._mgmt_enabled_streamers)
            enabled = list_fn(site)
            if key == 27:  # Escape
                self._mgmt_mode   = None
                self._mgmt_buf    = ""
                self._mgmt_result = ""
            elif key == curses.KEY_UP:
                self._mgmt_sel = max(0, self._mgmt_sel - 1)
            elif key == curses.KEY_DOWN:
                self._mgmt_sel = min(max(0, len(enabled) - 1), self._mgmt_sel + 1)
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
                if enabled:
                    username = enabled[self._mgmt_sel]
                    result   = _modify_config_streamer(site.config_path, username, action)
                    if action == "remove":
                        # Removed from [Streamers] entirely — no longer a
                        # "skip this stream" situation even if it was one;
                        # the checker's offline-triggered auto-unblock must
                        # not resurrect it.
                        self._clear_skip_disabled(site, username)
                    site.invalidate_config_cache()
                    self.config_editor.load_config(site.config_path)
                    self.config_editor.priority_editor.force_reload()
                    site.trigger_event.set()
                    self._mgmt_result = result
                    # Keep selection clamped to the (now shorter) list
                    new_enabled = list_fn(site)
                    self._mgmt_sel = min(self._mgmt_sel, max(0, len(new_enabled) - 1))
                else:
                    self._mgmt_mode   = None
                    self._mgmt_buf    = ""
                    self._mgmt_result = ""
            elif action == "disable" and key in (ord('s'), ord('S')):
                # "Skip this stream" — disable for the remainder of the
                # current live session only. Only makes sense for a
                # streamer that's actually live right now.
                if enabled:
                    username = enabled[self._mgmt_sel]
                    if not site.is_live(username):
                        self._mgmt_result = "Error: user is not currently live"
                    else:
                        result = _modify_config_streamer(site.config_path, username, "disable")
                        if result == f"Disabled '{username}'.":
                            result = f"Skipped '{username}'."
                        with site.dash_lock:
                            site.skip_disabled.add(username)
                            skip_snapshot = set(site.skip_disabled)
                        _save_skip_disabled(self.app, site.config_path, skip_snapshot)
                        site.invalidate_config_cache()
                        self.config_editor.load_config(site.config_path)
                        self.config_editor.priority_editor.force_reload()
                        site.trigger_event.set()
                        self._mgmt_result = result
                        new_enabled = list_fn(site)
                        self._mgmt_sel = min(self._mgmt_sel, max(0, len(new_enabled) - 1))
        else:
            # ── ADD mode: select a disabled streamer OR type a new name ────────
            disabled = self._mgmt_disabled_streamers(site)
            if key == 27:  # Escape → go back
                self._mgmt_mode   = None
                self._mgmt_buf    = ""
                self._mgmt_result = ""
            elif key == curses.KEY_UP:
                if disabled:
                    # Move up in list; clamp at 0 (don't wrap into text input)
                    self._mgmt_sel = max(0, self._mgmt_sel - 1) if self._mgmt_sel >= 0 \
                                     else len(disabled) - 1
            elif key == curses.KEY_DOWN:
                if disabled:
                    if self._mgmt_sel == -1:
                        self._mgmt_sel = 0
                    else:
                        self._mgmt_sel = min(len(disabled) - 1, self._mgmt_sel + 1)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self._mgmt_buf = self._mgmt_buf[:-1]
                self._mgmt_sel = -1   # typing refocuses text input
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
                if self._mgmt_buf.strip():
                    # Text input takes priority when it has content
                    username = self._mgmt_buf.strip().lower()
                    result = _modify_config_streamer(site.config_path,
                                                     self._mgmt_buf.strip(), "add")
                    self._clear_skip_disabled(site, username)
                    site.invalidate_config_cache()
                    self.config_editor.load_config(site.config_path)
                    self.config_editor.priority_editor.force_reload()
                    site.trigger_event.set()
                    self._mgmt_result = result
                    self._mgmt_buf    = ""
                elif self._mgmt_sel >= 0 and disabled:
                    # Re-enable the selected disabled streamer
                    username = disabled[self._mgmt_sel]
                    result   = _modify_config_streamer(site.config_path, username, "add")
                    self._clear_skip_disabled(site, username)
                    site.invalidate_config_cache()
                    self.config_editor.load_config(site.config_path)
                    self.config_editor.priority_editor.force_reload()
                    site.trigger_event.set()
                    self._mgmt_result = result
                    # Clamp selection to refreshed list
                    new_disabled = self._mgmt_disabled_streamers(site)
                    self._mgmt_sel = min(self._mgmt_sel, max(-1, len(new_disabled) - 1))
                else:
                    self._mgmt_mode   = None
                    self._mgmt_buf    = ""
                    self._mgmt_result = ""
            elif 32 <= key < 127:
                self._mgmt_buf += chr(key)
                self._mgmt_sel = -1   # typing refocuses text input
        return True

    # ── Live global-config apply (no restart needed) ──────────────────────────
    def apply_global_cfg(self, new_cfg: dict) -> None:
        """
        Called by GlobalConfigEditor immediately after global.conf is saved.
        Applies runtime-changeable settings to the live process so that changes
        like DEBUG_LOGS take effect without restarting the script.
        """
        from . import logger as _logger

        # ── DEBUG_LOGS / DEBUG_LOG_PATH ───────────────────────────────────────
        new_enabled = new_cfg.get("DEBUG_LOGS", "false").strip().lower() == "true"
        new_path    = new_cfg.get("DEBUG_LOG_PATH", "").strip().strip('"\'')

        _logger.dbg(
            f"[CONFIG] apply_global_cfg start: DEBUG_LOGS={new_enabled} DEBUG_LOG_PATH={new_path!r}"
        )

        if new_enabled:
            if new_path:
                # Explicit path provided — use it directly.
                _logger.configure_debug_log(True, new_path)
            else:
                # No explicit path — check if a path is already configured.
                _, current_path = _logger.get_debug_log_config()
                if current_path:
                    # Keep the existing path; just (re-)enable logging.
                    _logger.configure_debug_log(True, current_path)
                else:
                    # Fall back to the first site's default debug log path.
                    _logger.dbg("[CONFIG] apply_global_cfg fallback to first site debug log path")
                    try:
                        resolved_path = get_debug_log_path(load_config(self.sites[0].config_path))
                        _logger.configure_debug_log(True, resolved_path)
                        _logger.dbg(f"[CONFIG] apply_global_cfg resolved fallback DEBUG_LOG_PATH={resolved_path!r}")
                    except Exception as e:
                        _logger.dbg(f"[CONFIG] apply_global_cfg failed to resolve fallback debug log path: {e}")
                        _logger.configure_debug_log(False, "")
        else:
            _logger.configure_debug_log(False, "")

        final_enabled, final_path = _logger.get_debug_log_config()
        _logger.dbg(
            f"[CONFIG] apply_global_cfg completed: DEBUG_LOGS={final_enabled} "
            f"DEBUG_LOG_PATH={final_path!r}"
        )

        # ── FF_ERR_THRESH ─────────────────────────────────────────────────────
        # Apply the new ffmpeg error threshold immediately so in-flight drain
        # threads pick it up on their next error check.
        _new_thresh_raw = new_cfg.get("FF_ERR_THRESH", "200").strip()
        try:
            _new_thresh = int(_new_thresh_raw)
            if _new_thresh >= 0:
                self.app.ffmpeg_error_restart_threshold = _new_thresh
                _logger.dbg(
                    f"[CONFIG] apply_global_cfg: FFMPEG_ERROR_RESTART_THRESHOLD "
                    f"updated to {_new_thresh}"
                )
        except (ValueError, TypeError):
            _logger.dbg(
                f"[CONFIG] apply_global_cfg: invalid FF_ERR_THRESH value "
                f"{_new_thresh_raw!r} — keeping current threshold"
            )

        # ── GRAPH_SCALE ───────────────────────────────────────────────────────
        # Apply the new seconds-per-bar value immediately so the top graph's
        # cadence (and the disk-directory scan it drives) picks it up without
        # a restart.
        try:
            _new_scale = max(1, int(new_cfg.get("GRAPH_SCALE", "1").strip()))
            if _new_scale != self.graph_scale:
                self.graph_scale = _new_scale
                _logger.dbg(
                    f"[CONFIG] apply_global_cfg: GRAPH_SCALE updated to {_new_scale}"
                )
        except (ValueError, TypeError):
            _logger.dbg(
                f"[CONFIG] apply_global_cfg: invalid GRAPH_SCALE value "
                f"{new_cfg.get('GRAPH_SCALE', '1')!r} — keeping current scale"
            )

    # ── Changelog popup helpers ───────────────────────────────────────────────
    def _should_show_changelog(self) -> bool:
        """Return True when the changelog should be shown at startup.

        Show when:
          - update_available is False, AND
          - changelog_shown is False OR the key is missing entirely
            (missing = fresh install or manual update; treat it the same as False
             so the popup shows on the very first launch of any new version)

        Do NOT show when:
          - update_available is True  (update pending; changelog is for a version
            the user doesn't have yet)
          - changelog_shown is True   (already seen this version's changelog)
        """
        with self.app.update_available_lock:
            update_av = self.app.update_available
        if update_av:
            return False
        with self.app.global_json_lock:
            gd = self.app.load_global_json()
        # Key missing (fresh install / manual update / global.json reset) OR
        # explicitly False → show the changelog.
        return gd.get("changelog_shown") is not True

    def _mark_changelog_shown(self) -> None:
        """Persist changelog_shown=True immediately when the popup opens."""
        def _mutate(gdata):
            gdata["changelog_shown"] = True
        self.app.update_global_json(_mutate)
        dbg("[CHANGELOG] changelog_shown marked True")

    def _load_changelog_lines(self) -> List[str]:
        """Read jj-dlp/docs/changelog.txt and return its lines, or an error message."""
        changelog_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "docs", "changelog.txt"
        )
        try:
            with open(changelog_path, "r", encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f.readlines()]
            return lines if lines else ["(changelog is empty)"]
        except FileNotFoundError:
            return [f"Changelog not found at:", changelog_path]
        except Exception as e:
            dbg(f"_load_changelog_lines: {e}")
            return [f"Error reading changelog: {e}"]

    def open_changelog_popup(self) -> None:
        """Open the changelog popup, load content, and persist changelog_shown=True."""
        self._changelog_lines = self._load_changelog_lines()
        self._changelog_scroll = 0
        self._changelog_popup_open = True
        self._mark_changelog_shown()

    # ── Recording-failure alert (full-screen, does not auto-dismiss) ───────────
    def _update_write_failure_alert(self) -> None:
        """Called once per frame. Opens the full-screen alert whenever a
        streamer has been flagged by NOTIFY_NO_CONFIRM_FILE (see
        SiteState.flag_write_failure). Never auto-closes it — only an
        explicit keypress in _handle_write_failure_alert_key() does that."""
        names: List[str] = []
        for _site in self.sites:
            with _site.dash_lock:
                names.extend(_site.write_failure_streamers)
        if names:
            self._write_failure_names = sorted(set(names))
            self._write_failure_alert_open = True

    def _handle_write_failure_alert_key(self, key) -> bool:
        """Handle input while the recording-failure alert is open. Requires
        an explicit dismissal; does not time out or auto-close. Returns True
        to keep running (this alert never quits the app)."""
        if key in (27, ord('q'), ord('Q'), ord('x'), ord('X'),
                   ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            for _site in self.sites:
                with _site.dash_lock:
                    _site.write_failure_streamers.clear()
            self._write_failure_alert_open = False
        return True

    def draw_write_failure_alert(self) -> None:
        """Draw the centered 'recording has failed' alert popup."""
        if not self._write_failure_alert_open:
            return
        h, w = self.stdscr.getmaxyx()
        failing = self._write_failure_names
        if not failing:
            self._write_failure_alert_open = False
            return

        alert_attr = theme.attr(self, "main_jjdlpdashboard_draw_write_failure_a_delete")

        title = " ‼ RECORDING FAILURE ‼ "
        message = "The recording may have failed for the following streamer(s):"
        names_lines = [f"    {name}" for name in failing[: max(1, h - 12)]]
        legend = " Press Enter / X / Esc / Q to dismiss "

        box_w = min(w - 6, max(len(title), len(message), len(legend),
                                max((len(n) for n in names_lines), default=0)) + 6)
        box_h = min(h - 4, 7 + len(names_lines))
        by1 = max(1, (h - box_h) // 2)
        bx1 = max(1, (w - box_w) // 2)
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1), alert_attr)
        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_DELETE)

        self.safe_addstr(self.stdscr, by1, bx1 + max(0, (box_w - len(title)) // 2),
                    title, alert_attr)
        self.safe_addstr(self.stdscr, by1 + 2, bx1 + max(0, (box_w - len(message)) // 2),
                    message, alert_attr)
        for i, line in enumerate(names_lines):
            self.safe_addstr(self.stdscr, by1 + 4 + i, bx1 + 2, line[:box_w - 4], alert_attr)
        self.safe_addstr(self.stdscr, by2 - 1, bx1 + max(0, (box_w - len(legend)) // 2),
                    legend[:box_w - 2], theme.attr(self, "main_jjdlpdashboard_draw_write_failure_a_invhead"))

    def _open_exit_confirm(self) -> None:
        """Open the 'Are you sure you want to exit?' popup, 'Yes' selected by default."""
        self._exit_confirm_open = True
        self._exit_confirm_sel  = 0   # 0 = Yes, 1 = No

    def _handle_exit_confirm_key(self, key) -> bool:
        """Handle input while the exit-confirmation popup is open.

        Returns False to quit the app, True to keep running.
        """
        if key in (27, ord('q'), ord('Q')):  # Esc/Q again → same as selecting Yes + Enter
            return False
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord('h'), ord('l'),
                     curses.KEY_UP, curses.KEY_DOWN, ord('j'), ord('k'), ord('\t')):
            self._exit_confirm_sel = 1 - self._exit_confirm_sel
        elif key in (ord('y'), ord('Y')):
            return False
        elif key in (ord('n'), ord('N')):
            self._exit_confirm_open = False
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if self._exit_confirm_sel == 0:   # Yes
                return False
            self._exit_confirm_open = False   # No → close popup, keep running
        return True

    def draw_exit_confirm_popup(self) -> None:
        """Draw the small 'Are you sure you want to exit?' confirmation box."""
        if not self._exit_confirm_open:
            return
        h, w = self.stdscr.getmaxyx()

        message = "Are you sure you want to exit?"
        legend  = " \u2190/\u2192: Select  Enter: Confirm  Esc/Q: Exit "
        box_w = min(max(len(message) + 6, len(legend) + 4, 34), w - 4)
        box_h = 5
        by1 = max(0, (h - box_h) // 2)
        bx1 = max(0, (w - box_w) // 2)
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        # Fill background
        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1),
                        theme.attr(self, "main_jjdlpdashboard_draw_exit_confirm_po_normal_1"))

        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_WARN)
        title = " CONFIRM EXIT "
        self.safe_addstr(self.stdscr, by1, bx1 + 2, title,
                    theme.attr(self, "main_jjdlpdashboard_draw_exit_confirm_po_warn"))

        self.safe_addstr(self.stdscr, by1 + 2, bx1 + max(0, (box_w - len(message)) // 2),
                    message, theme.attr(self, "main_jjdlpdashboard_draw_exit_confirm_po_normal_2"))

        yes_label = " Yes "
        no_label  = " No "
        gap = 4
        buttons_w = len(yes_label) + len(no_label) + gap
        start_x = bx1 + max(0, (box_w - buttons_w) // 2)
        yes_attr = (theme.attr(self, "main_jjdlpdashboard_draw_exit_confirm_po_hilight_1")) if self._exit_confirm_sel == 0 \
                   else theme.attr(self, "main_jjdlpdashboard_draw_exit_confirm_po_normal_3")
        no_attr  = (theme.attr(self, "main_jjdlpdashboard_draw_exit_confirm_po_hilight_2")) if self._exit_confirm_sel == 1 \
                   else theme.attr(self, "main_jjdlpdashboard_draw_exit_confirm_po_normal_4")
        self.safe_addstr(self.stdscr, by1 + 3, start_x, yes_label, yes_attr)
        self.safe_addstr(self.stdscr, by1 + 3, start_x + len(yes_label) + gap, no_label, no_attr)

        self.safe_addstr(self.stdscr, by2, bx1 + max(0, (box_w - len(legend)) // 2),
                    legend[:max(0, box_w - 2)],
                    theme.attr(self, "main_jjdlpdashboard_draw_exit_confirm_po_invhead"))

    def draw_changelog_popup(self) -> None:
        """Draw the scrollable changelog popup centred on screen."""
        if not self._changelog_popup_open:
            return
        h, w = self.stdscr.getmaxyx()

        box_h = min(h - 4, 40)
        box_w = min(w - 4, 100)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        # Fill background
        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1),
                        theme.attr(self, "main_jjdlpdashboard_draw_changelog_popup_normal_1"))

        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_CHROME)
        title = " WHAT'S NEW "
        self.safe_addstr(self.stdscr, by1, bx1 + 2, title,
                    theme.attr(self, "main_jjdlpdashboard_draw_changelog_popup_hilight"))

        content_width = max(1, box_w - 4)
        wrapped = self._wrap_lines(self._changelog_lines, content_width)

        visible_rows = box_h - 3   # top border + title row + bottom legend row
        max_scroll   = max(0, len(wrapped) - visible_rows)
        self._changelog_scroll = min(self._changelog_scroll, max_scroll)

        start = self._changelog_scroll
        view  = wrapped[start : start + visible_rows]

        for i, line in enumerate(view):
            self.safe_addstr(self.stdscr, by1 + 1 + i, bx1 + 2, line,
                        theme.attr(self, "main_jjdlpdashboard_draw_changelog_popup_normal_2"))

        # Scroll indicator
        if max_scroll > 0:
            pct = int(100 * self._changelog_scroll / max_scroll)
            scroll_info = f" ↑↓/PgUp/PgDn  {pct}% "
        else:
            scroll_info = " (all) "
        legend = f" Q/Esc: close {scroll_info}"
        self.safe_addstr(self.stdscr, by2, bx1 + 2,
                    legend[:box_w - 4],
                    theme.attr(self, "main_jjdlpdashboard_draw_changelog_popup_invhead"))

    # ── Bake-to-source popup (dev feature, hidden 'W' hotkey) ────────────────
    def open_bake_popup(self) -> None:
        """Write the current theme customizations into theme.py (the only
        file this touches) and show a summary popup. NOTE: the next app
        update overwrites these edits."""
        result = theme.bake_to_source()
        lines = []
        if not result['ok']:
            lines.append("BAKE TO SOURCE — FAILED")
            lines.append(result.get('error') or "Unknown error.")
        else:
            lines.append("BAKE TO SOURCE — OK")
            if result['schemes']:
                schemes = ", ".join(str(i) for i in result['schemes'])
                lines.append(f"  COLOR_SCHEMES {schemes} updated")
            if result['role_sites'] or result['bold_sites']:
                parts = []
                if result['role_sites']:
                    parts.append(f"{result['role_sites']} role repoint(s)")
                if result['bold_sites']:
                    parts.append(f"{result['bold_sites']} bold toggle(s)")
                lines.append("  SITE_REGISTRY updated: " + ", ".join(parts))
            if result['files']:
                lines.append("  Files written: " + ", ".join(result['files']))
            lines.append("NOTE: app updates overwrite these edits.")
        self._bake_popup_lines = lines
        self._bake_popup_open = True

    def draw_bake_popup(self) -> None:
        """Draw the bake-to-source result popup centred on screen."""
        if not self._bake_popup_open:
            return
        h, w = self.stdscr.getmaxyx()
        content = self._bake_popup_lines
        box_h = min(h - 4, len(content) + 4)
        box_w = min(w - 4, 64)
        by1 = max(0, (h - box_h) // 2)
        bx1 = max(0, (w - box_w) // 2)
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1),
                        theme.attr(self, "main_jjdlpdashboard_draw_changelog_popup_normal_1"))

        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_CHROME)
        title = " THEME \u2192 SOURCE "
        self.safe_addstr(self.stdscr, by1, bx1 + 2, title,
                    theme.attr(self, "main_jjdlpdashboard_draw_changelog_popup_hilight"))

        for i, line in enumerate(content[:max(0, box_h - 3)]):
            self.safe_addstr(self.stdscr, by1 + 2 + i, bx1 + 2, line[:box_w - 4],
                        theme.attr(self, "main_jjdlpdashboard_draw_changelog_popup_normal_2"))

        legend = " Q/Esc: close "
        self.safe_addstr(self.stdscr, by2, bx1 + 2, legend[:box_w - 4],
                    theme.attr(self, "main_jjdlpdashboard_draw_changelog_popup_invhead"))

    # ── Graph module hot-reload (dev feature) ─────────────────────────────────
    def reload_graph_module(self) -> None:
        """Hot-reload jj_dlp/graph.py from disk (triggered by the "Reload
        graph.py" row of the 'p' knob popup).

        On success: re-executes the module, constructs a fresh ``Graph`` from
        it (carrying over the disk-rate history and the transient sub-sampler
        state; live-tuned _GRAPH_* knobs reset to the newly-loaded file
        defaults) and swaps it in for ``self.graph``. The popup closes because
        the fresh instance starts closed.

        On failure: the current ``Graph`` keeps running untouched and the
        error is recorded on it so the popup's legend can show it. A syntax
        error in graph.py therefore never takes the app down.
        """
        try:
            import importlib
            import traceback
            importlib.reload(_graph)
        except Exception as _e:
            _logger.dbg(f"[GRAPH] reload failed:\n{traceback.format_exc()}")
            self.graph.popup_status = f"reload failed: {_e}"
            return
        try:
            _old = self.graph
            _new = _graph.Graph(self)
        except Exception as _e:
            _logger.dbg(f"[GRAPH] reload failed (Graph construction):\n{traceback.format_exc()}")
            self.graph.popup_status = f"reload failed: {_e}"
            return
        # Carry over history + transient pipeline state (knobs reset to the
        # new defaults by the fresh instance).
        _new.disk_rate_history.extend(_old.disk_rate_history)
        _new._disk_graph_instant_rate = _old._disk_graph_instant_rate
        _new._disk_graph_window_bytes = _old._disk_graph_window_bytes
        _new._disk_graph_window_start = _old._disk_graph_window_start
        _new._disk_graph_window_peak = _old._disk_graph_window_peak
        _new._disk_graph_held_rate = _old._disk_graph_held_rate
        _new._disk_graph_last_tick = _old._disk_graph_last_tick
        _new._disk_graph_last_subsample = _old._disk_graph_last_subsample
        _new._disk_graph_bytes_ring.extend(_old._disk_graph_bytes_ring)
        self.graph = _new
        _logger.dbg("[GRAPH] graph.py reloaded")

    # ── Run loop ──────────────────────────────────────────────────────────────
    def _persist_graph_history(self) -> None:
        """Persist the current disk-rate graph bars to global.json.

        Called on shutdown so the graph comes back with its history on the
        next launch.  Best-effort — failures are swallowed.
        """
        try:
            _save_disk_rate_history(self.app, list(self.graph.disk_rate_history))
        except Exception as _e:
            dbg(f"[GRAPH] _persist_graph_history failed: {_e!r}")

    def _handle_possible_resize(self) -> None:
        """Detect a terminal resize and force curses to fully repaint.
        """
        try:
            curses.update_lines_cols()
        except Exception as e:
            dbg(f"_handle_possible_resize: {e}")
            pass
        size = self.stdscr.getmaxyx()
        if size != getattr(self, "_last_term_size", size):
            self._last_term_size = size
            try:
                curses.resizeterm(*size)
            except Exception as e:
                dbg(f"_handle_possible_resize: {e}")
                pass
            # touchwin() marks every cell as "changed" so the next
            # refresh() is forced to resend the whole screen instead of
            # diffing against curses' now-stale physical-screen cache.
            self.stdscr.touchwin()
        else:
            self._last_term_size = size

    def run(self):
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        self.setup_colors()

        # Track terminal size so we can detect resizes even on platforms
        # (notably windows-curses / cmd.exe) where KEY_RESIZE isn't
        # reliably delivered through getch(). See _handle_possible_resize().
        self._last_term_size = self.stdscr.getmaxyx()

        _perf_frame_count = 0
        _perf_next_report = time.time() + 10.0  # report every 10 seconds

        while True:
            _t_frame_start = time.time()
            self._handle_possible_resize()
            self._update_write_failure_alert()
            self.refresh_screen()
            _t_after_refresh = time.time()

            # After the first frame has been drawn, check whether we should show
            # the changelog popup.  We defer this by one frame so the dashboard
            # is fully visible before the overlay appears.
            if not self._changelog_popup_queued:
                self._changelog_popup_queued = True
                if self._should_show_changelog():
                    self.open_changelog_popup()

            # Drain ALL pending keypresses before sleeping.
            # This prevents the input buffer from accumulating a backlog
            # while napms() is sleeping, which would cause continued movement
            # after a key is released.
            should_quit = False
            while True:
                key = self.stdscr.getch()
                if key == -1:
                    break
                if key == curses.KEY_RESIZE:
                    # Some curses backends do deliver this. Handle it the
                    # same way as our own polling-based detection and skip
                    # passing it into handle_key() (it's not a real
                    # keypress the app's key handling should act on).
                    self._handle_possible_resize()
                    continue
                if not self.handle_key(key):
                    should_quit = True
                    break
            if should_quit:
                self._persist_graph_history()
                break
            self.tick += 1
            curses.napms(50)

            _t_frame_end = time.time()
            _perf_frame_count += 1

            if _t_frame_end >= _perf_next_report:
                _frame_ms   = (_t_after_refresh - _t_frame_start) * 1000
                _total_ms   = (_t_frame_end - _t_frame_start) * 1000
                _fps        = _perf_frame_count / 10.0
                dbg(
                    f"[PERF][run] 10s summary: frames={_perf_frame_count} "
                    f"effective_fps={_fps:.1f} "
                    f"last_refresh_ms={_frame_ms:.1f} "
                    f"last_total_frame_ms={_total_ms:.1f}"
                )
                _perf_frame_count = 0
                _perf_next_report = _t_frame_end + 10.0


# ══════════════════════════════════════════════════════════════════════════════
# Browser cookie helper (for --cookies-from-browser in [Downloader])
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# Multi-select startup chooser
# ══════════════════════════════════════════════════════════════════════════════


def _curses_choose_config(stdscr, app: "AppState", found: List[str]) -> List[str]:
    """
    MenuWorks-style config file chooser.
    Space = toggle [x],  Enter = confirm,  Q = quit.
    Returns list of selected config file paths (at least 1).
    """
    curses.start_color()
    curses.use_default_colors()
    theme.apply_palette(None)   # pairs 1-13 follow the active theme, like the dashboard

    curses.curs_set(0)
    stdscr.keypad(True)

    # ── Phase 1: config file selection ───────────────────────────────────────
    selected  = set(range(len(found)))   # start with all config files selected
    cursor    = 0
    n         = len(found)
    do_not_show_config = False

    # Display the SITE_LABEL of each config instead of its path.
    site_labels = {}
    for _name in found:
        try:
            site_labels[_name] = load_config(os.path.join(os.getcwd(), _name)).get(
                "site_label", os.path.basename(_name))
        except (SystemExit, Exception):
            site_labels[_name] = os.path.basename(_name)

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.bkgd(" ", theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum0"))

        # Logo
        for i, line in enumerate(ASCII_LOGO):
            JJDlpDashboard.safe_addstr(stdscr, 1 + i, 2, line, theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum6"))

        ts = time.strftime("%Y-%m-%d  %H:%M:%S")
        JJDlpDashboard.safe_addstr(stdscr, 1, w - len(ts) - 3, ts, theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum1_1"))
        JJDlpDashboard.safe_addstr(stdscr, 7, 2, "-" * (w - 4), theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum1_2"))

        # Title
        title = "SELECT SITES"
        JJDlpDashboard.safe_addstr(stdscr, 9, 2, title, theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum5_1"))

        # Instructions
        JJDlpDashboard.safe_addstr(stdscr, 10, 2,
                    "Space = toggle [x]   Enter = confirm   Q = quit",
                    theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum3_1"))

        # File list
        for i, name in enumerate(found):
            row     = 12 + i
            checked = "[x]" if i in selected else "[ ]"
            is_cur  = i == cursor
            if is_cur:
                attr = theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum2")
            elif i in selected:
                attr = theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum4")
            else:
                attr = theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum1_3")
            JJDlpDashboard.safe_addstr(stdscr, row, 4, f"  {checked}  {site_labels.get(name, name)}", attr)

        # "Do not show again" checkbox
        dna_row = 12 + n + 1
        dna_box = "[x]" if do_not_show_config else "[ ]"
        dna_attr = theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum3_2") if do_not_show_config else theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum3_3")
        JJDlpDashboard.safe_addstr(stdscr, dna_row, 4,
                    f"  {dna_box}  Do not show again (press D to toggle)",
                    dna_attr)

        # Footer
        sel_count = len(selected)
        footer = (f"  {sel_count} site(s) selected  "
                  f"↑/↓ navigate  Space toggle  Enter confirm  D do not show  ")
        JJDlpDashboard.safe_addstr(stdscr, h - 1, 0,
                    footer.ljust(w - 1)[:w - 1],
                    theme.attr(None, "main_jjdlpdashboard_curses_choose_config_pairnum5_2"))

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord('k')):
            cursor = (cursor - 1) % n
        elif key in (curses.KEY_DOWN, ord('j')):
            cursor = (cursor + 1) % n
        elif key == ord(' '):
            if cursor in selected:
                if len(selected) > 1:
                    selected.discard(cursor)
            else:
                selected.add(cursor)
        elif key in (ord('d'), ord('D')):
            do_not_show_config = not do_not_show_config
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if selected:
                chosen_files = [found[i] for i in sorted(selected)]

                # Save chosen config files to global.json
                def _mutate(gdata):
                    gdata["startup_configs"] = chosen_files
                app.update_global_json(_mutate)

                if do_not_show_config:
                    _write_global_conf_key("ASK_FOR_CONFIG", "false")
                
                return chosen_files
        elif key in (ord('q'), ord('Q'), 27):
            sys.exit(0)



def run_dashboard(app: "AppState", global_cfg: Optional[dict] = None):
    """Run the curses dashboard inside curses.wrapper; returns the JJDlpDashboard instance (or None)."""
    def _run(stdscr):
        h, w = stdscr.getmaxyx()
        min_h, min_w = 30, 90
        if h < min_h or w < min_w:
            stdscr.clear()
            stdscr.addstr(0, 0,
                f"Terminal too small — need at least {min_w}×{min_h} "
                f"(currently {w}×{h}). Resize and re-run.")
            stdscr.refresh()
            stdscr.getch()
            return None
        dash = JJDlpDashboard(stdscr, app, global_cfg=global_cfg)
        dash.run()
        return dash

    return curses.wrapper(_run)


def choose_config(app: "AppState", found: List[str]) -> List[str]:
    """Run the startup multi-select config chooser inside curses.wrapper."""
    return curses.wrapper(_curses_choose_config, app, found)
