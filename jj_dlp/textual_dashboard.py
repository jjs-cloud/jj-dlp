#!/usr/bin/env python3
"""jj-dlp — Textual port, framework-only demo.

Uses Textual's built-in Gruvbox theme as the default theme and relies on
Textual theme variables rather than hard-coded colors.

The dashboard can switch between the themes available in the installed
version of Textual.

POLLING DECISION: site panels refresh by reading SiteState directly inside
a set_interval callback on the main Textual event loop (see JJDlpApp._data_tick
and SitePanel.refresh_data), the same way curses_dashboard's draw loop reads
SiteState synchronously once per frame. This was chosen over a background
worker thread for simplicity, since curses already proves this synchronous,
lock-protected read pattern works fine at this polling rate. The tradeoff:
Textual's UI is single-threaded, so if a SiteState lock is ever held for a
noticeable stretch by a checker/recorder thread in main.py, this callback
blocks waiting for it and the *entire* UI (all panels, animations, keypresses)
freezes until the lock is released. If that turns out to cause visible stalls
in practice, switch to a background worker thread (self.run_worker(..., thread=True))
that polls SiteState off the main thread and hands results back via
self.call_from_thread(...) or a posted Message, so lock contention can no
longer block rendering or input handling.
"""

import os
import time
from datetime import datetime
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    Switch,
    Tabs,
    Tab,
    TextArea,
    ContentSwitcher,
)

# Safe at module load time: main.py only imports this module lazily, inside
# a function, by which point main.py itself has already finished importing.
from .main import _modify_config_streamer, _CHECKER_STDOUT_PREFIX, _CHECKER_STDERR_PREFIX

if TYPE_CHECKING:
    # Avoids a circular import; main.py imports this module.
    from .main import AppState, SiteState


__version__ = "1.28.11"


# Matches ASCII_LOGO in main.py (6 lines).
ASCII_LOGO = r"""     __     __              .___.__          
     |__|   |__|           __| _/|  | ______  
     |  |   |  |  ______  / __ | |  | \____ \ 
     |  |   |  | /_____/ / /_/ | |  |_|  |_> >
 /\__|  /\__|  |         \____ | |____/   __/ 
 \______\______|              \/      |__|    """


# Ported from curses_dashboard.py so both dashboards agree on timing/format.
_QUALITY_DISPLAY_GRACE_SECS: float = 60.0
DATA_POLL_INTERVAL = 1.0
BLINK_INTERVAL = 0.25


def _fmt_duration(seconds: float) -> str:
    """Format a duration as a single truncated unit, e.g. '35m', '56d'."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    if days:
        return f"{days}d"
    hours, rem = divmod(rem, 3600)
    if hours:
        return f"{hours}h"
    minutes, s = divmod(rem, 60)
    if minutes:
        return f"{minutes}m"
    return f"{s}s"


def _live_bar(seconds: float, width: int = 14, max_secs: int = 6 * 3600) -> str:
    """Render a filled/unfilled block bar showing progress toward max_secs."""
    filled = min(int(width * seconds / max(1, max_secs)), width)
    return "█" * filled + "░" * (width - filled)


def _live_bar_dashed(seconds: float, width: int = 14, max_secs: int = 6 * 3600) -> str:
    """Like _live_bar, but dashed for disabled streamers."""
    filled = min(int(width * seconds / max(1, max_secs)), width)
    return "─" * filled + ("─ " * (width - filled))[:width - filled]


def _site_label(site: "SiteState") -> str:
    """Return a site's configured label, or its config filename if unset."""
    return site.get_cached_config().get("site_label", os.path.basename(site.config_path))


def _log_line_style(line: str, theme) -> str:
    """Pick a style for one activity/debug log line, mirroring curses_dashboard."""
    if "Live now" in line or "Recording started" in line:
        return theme.success
    if "ERROR" in line or "Stall" in line or "STOPPED" in line or "Warning" in line:
        return theme.error
    if "Info" in line:
        return theme.warning
    return "dim"


def _collect_log_lines(sites_app: "AppState", site_idx: "int | None", show_debug: bool) -> list[str]:
    """Merge activity (and optionally debug) lines for one site, or all sites, in time order."""
    def ts(ln: str) -> str:
        return ln[:20] if ln[:1] == "[" else ""

    if site_idx is None:
        raw_lines: list[str] = []
        raw_debug: list[str] = []
        with sites_app.global_log_lock:
            raw_lines.extend(sites_app.global_log_lines)
        for site in sites_app.sites:
            lbl = _site_label(site)
            with site.dash_lock:
                site_lines = list(site.dash_log_lines)
                site_debug = list(site.dash_debug_lines) if show_debug else []
            raw_lines.extend(
                f"{ln[:21]} [{lbl}]{ln[21:]}" if ln[:1] == "[" else ln for ln in site_lines
            )
            raw_debug.extend(
                f"{ln[:21]} [{lbl}]{ln[21:]}" if ln[:1] == "[" else ln for ln in site_debug
            )
        raw_lines.sort(key=ts)
        raw_debug.sort(key=ts)
    else:
        site = sites_app.sites[site_idx]
        with site.dash_lock:
            raw_lines = list(site.dash_log_lines)
        raw_debug = []

    merged: list[str] = []
    i = j = 0
    while i < len(raw_lines) and j < len(raw_debug):
        if ts(raw_lines[i]) <= ts(raw_debug[j]):
            merged.append(raw_lines[i]); i += 1
        else:
            merged.append(raw_debug[j]); j += 1
    merged.extend(raw_lines[i:])
    merged.extend(raw_debug[j:])
    return merged


def _redraw_log(widget: RichLog, lines: list[str], styles: "list[str] | None" = None) -> None:
    """Repaint a RichLog with `lines`, preserving scroll position unless at the bottom."""
    at_bottom = widget.scroll_y >= max(0.0, widget.max_scroll_y - 0.5)
    old_y = widget.scroll_y
    widget.clear()
    for i, line in enumerate(lines):
        style = styles[i] if styles else ""
        widget.write(Text(line, style=style) if style else line)
    if at_bottom:
        widget.scroll_end(animate=False)
    else:
        widget.scroll_to(y=old_y, animate=False)


TAB_IDS = [
    "dashboard",
    "log",
    "stdout",
    "stderr",
    "config",
    "filemanager",
]

TAB_LABELS = {
    "dashboard": "Dashboard",
    "log": "Log",
    "stdout": "Stdout",
    "stderr": "Stderr",
    "config": "Config",
    "filemanager": "File Manager",
}


class Separator(Static):
    """The plain rule drawn between the tab bar and content."""

    def render(self) -> str:
        return "─" * self.size.width


class Header(Horizontal):
    """Logo on the left; clock + version stacked and right-aligned."""

    def compose(self) -> ComposeResult:
        yield Static(ASCII_LOGO, id="logo")

        with Vertical(id="header-info"):
            yield Static(id="header-time")
            yield Static(id="header-version")

    def on_mount(self) -> None:
        self.query_one("#header-version", Static).update(f"v{__version__}")
        self.update_clock()
        self.set_interval(1.0, self.update_clock)

    def update_clock(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        self.query_one("#header-time", Static).update(now)


class SystemPanel(Static):
    """Bordered sidebar shown alongside the tab content."""

    def on_mount(self) -> None:
        self.border_title = " SYSTEM "


# Column positions added in SitePanel.on_mount.
TOGGLE_COLUMN_INDEX = 0
NAME_COLUMN_INDEX = 1


class StreamerTable(DataTable):
    """DataTable that never shows the cursor highlight on the toggle column."""

    def _should_highlight(self, cursor, target_cell, type_of_cursor) -> bool:
        if type_of_cursor == "cell" and target_cell.column == TOGGLE_COLUMN_INDEX:
            return False
        return super()._should_highlight(cursor, target_cell, type_of_cursor)


class SitePanel(Container):
    """One site's streamer table, with badge/countdown border decorations."""

    DEFAULT_CSS = """
    SitePanel {
        padding: 0 1;
    }

    SitePanel DataTable {
        height: 1fr;
    }

    SitePanel #add-row {
        height: 1;
    }

    SitePanel #add-input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0 1;
    }

    SitePanel #add-button {
        width: auto;
        min-width: 10;
        height: 1;
        border: none;
        padding: 0 1;
    }

    SitePanel #rem-button {
        width: auto;
        min-width: 10;
        height: 1;
        border: none;
        padding: 0 1;
    }

    SitePanel #countdown {
        height: 1;
        color: $warning;
    }
    """

    # 'a' bubbles up from the DataTable (which doesn't bind it) to focus
    # this panel's Add input, even while the table has focus.
    BINDINGS = [
        Binding("a", "focus_add_input", "Add streamer", show=False),
    ]

    def __init__(self, site: "SiteState", **kwargs) -> None:
        super().__init__(**kwargs)
        self.site = site
        self._row_keys: set[str] = set()
        # streamer -> (text_a, style_a, text_b, style_b) for blinking rows.
        self._blinking: dict[str, tuple[str, str, str, str]] = {}
        self._next_check_in: float = 0.0
        self._countdown_message_until: float = 0.0
        self._disabled: set[str] = set()
        # streamer -> (want_disabled, expiry_time) for optimistic toggle clicks
        # not yet confirmed by SiteState's next checker cycle.
        self._pending_toggle: dict[str, tuple[bool, float]] = {}
        self._selected_streamer: str | None = None

    def compose(self) -> ComposeResult:
        yield StreamerTable(cursor_type="cell", id="streamer-table")
        with Horizontal(id="add-row"):
            yield Input(placeholder="Add streamer...", id="add-input")
            yield Button("Add", id="add-button", variant="success")
            yield Button("Rem", id="rem-button", variant="error")
        yield Static("", id="countdown")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        keys = table.add_columns("", "Name", "Stat", "Prog", "Dur", "Last")
        self._col = dict(zip(("toggle", "name", "status", "bar", "duration", "last_live"), keys))
        self.refresh_data()

    def action_focus_add_input(self) -> None:
        self.query_one("#add-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "add-input":
            self._add_streamer(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-button":
            self._add_streamer(self.query_one("#add-input", Input).value)
        elif event.button.id == "rem-button":
            self._remove_selected_streamer()

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        """Track the Name cell under the cursor so Rem knows what to remove."""
        if event.coordinate.column == NAME_COLUMN_INDEX:
            self._selected_streamer = event.cell_key.row_key.value

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Toggle enable/disable on a selected toggle-column cell (click-again or Enter)."""
        if event.coordinate.column != TOGGLE_COLUMN_INDEX:
            return
        streamer = event.cell_key.row_key.value
        if streamer is not None:
            self._toggle_streamer(streamer)

    def _toggle_streamer(self, streamer: str) -> None:
        """Flip a streamer's disabled state instantly, then persist the change."""
        want_disabled = streamer not in self._disabled
        if want_disabled:
            self._disabled.add(streamer)
        else:
            self._disabled.discard(streamer)
        self._pending_toggle[streamer] = (want_disabled, time.time() + 45.0)
        self._paint_toggle_cell(streamer, want_disabled)
        action = "disable" if want_disabled else "add"
        message = _modify_config_streamer(self.site.config_path, streamer, action)
        self._flash_countdown_message(message)

    def _paint_toggle_cell(self, streamer: str, is_disabled: bool) -> None:
        """Redraw one row's toggle glyph without waiting for the next poll."""
        theme = self.app.current_theme
        glyph, style = ("○", "dim") if is_disabled else ("●", theme.success)
        self.query_one(DataTable).update_cell(
            streamer, self._col["toggle"], Text(glyph, style=style), update_width=True
        )

    def _remove_selected_streamer(self) -> None:
        """Remove whichever streamer's Name cell is currently selected."""
        streamer = self._selected_streamer
        if streamer is None:
            self._flash_countdown_message("No streamer selected")
            return
        message = _modify_config_streamer(self.site.config_path, streamer, "remove")
        self._pending_toggle.pop(streamer, None)
        self._selected_streamer = None
        self._flash_countdown_message(message)

    def _add_streamer(self, username: str) -> None:
        """Add a streamer to this site's config and flash the result."""
        message = _modify_config_streamer(self.site.config_path, username, "add")
        self.query_one("#add-input", Input).value = ""
        self._flash_countdown_message(message)

    def _flash_countdown_message(self, message: str, seconds: float = 3.0) -> None:
        """Briefly show `message` on the countdown line, then let it revert."""
        self._countdown_message_until = time.time() + seconds
        self.query_one("#countdown", Static).update(message)
        self.set_timer(seconds, self._update_countdown)

    def refresh_data(self) -> None:
        """Diff streamer rows against SiteState and update changed cells."""
        site = self.site
        now = time.time()
        cfg = site.get_cached_config()

        with site.dash_lock:
            site_label = cfg.get("site_label", os.path.basename(site.config_path))
            all_streamers = list(site.dash_all_streamers)
            last_live = dict(site.dash_last_live)
            disabled = set(site.dash_blocked)
            self._next_check_in = site.dash_next_check_in

        # Apply optimistic toggle clicks the backend hasn't confirmed yet;
        # drop each override once dash_blocked agrees with it or it expires.
        for s, (want_disabled, expires) in list(self._pending_toggle.items()):
            if (s in disabled) == want_disabled or now >= expires:
                del self._pending_toggle[s]
            elif want_disabled:
                disabled.add(s)
            else:
                disabled.discard(s)
        self._disabled = disabled

        live_since = site.snapshot_live_since()

        with site.lock:
            recording = set(site.currently_recording)
            recording_res = {
                s: h
                for s, h in site.recording_resolution.items()
                if (now - site.recording_attempt_started.get(s, 0))
                >= _QUALITY_DISPLAY_GRACE_SECS
            }
            recording_res.update(site.display_resolution)
            ad_alert_streamers = set(getattr(site, "ad_alerts", {}).keys())
            ffmpeg_error_streamers = {
                s
                for s, c in getattr(site, "ffmpeg_error_counts", {}).items()
                if c > 0
            }

        bar_max_secs = cfg.get("progress_bar_max_hours", 6) * 3600
        highlight_days = cfg.get("last_live_highlight", 0)

        self.border_title = f" {site_label} "
        self._update_badges(all_streamers, live_since, recording, disabled)

        table = self.query_one(DataTable)
        col = self._col
        new_keys = set(all_streamers)

        for s in new_keys - self._row_keys:
            table.add_row("", "", "", "", "", "", key=s)
            self._row_keys.add(s)
        for s in self._row_keys - new_keys:
            table.remove_row(s)
            self._row_keys.discard(s)
            self._blinking.pop(s, None)

        theme = self.app.current_theme
        self._blinking.clear()

        # Bar rendering is deferred until the other columns' widths are final.
        bar_rows: list[tuple[str, bool, bool, float]] = []

        for s in all_streamers:
            is_live = s in live_since
            is_rec = s in recording
            is_disabled = s in disabled
            seconds = now - live_since.get(s, now) if is_live else 0.0

            self._paint_toggle_cell(s, is_disabled)

            name_style = "dim" if is_disabled else ""
            table.update_cell(s, col["name"], Text(s, style=name_style), update_width=True)

            status_a, style_a = self._status_cell(is_live, is_rec, is_disabled, theme)
            if is_live and is_disabled:
                status_b, style_b = "DIS ", "dim"
                self._blinking[s] = (status_a, style_a, status_b, style_b)
            elif is_live and is_rec:
                status_a, style_a = "Live", theme.success
                status_b, style_b = "REC ", theme.error
                self._blinking[s] = (status_a, style_a, status_b, style_b)
            else:
                table.update_cell(s, col["status"], Text(status_a, style=style_a), update_width=True)

            duration_text = _fmt_duration(seconds) if is_live else "-"
            table.update_cell(s, col["duration"], duration_text, update_width=True)

            table.update_cell(
                s, col["last_live"],
                self._last_live_cell(s, is_live, recording_res, last_live, now, highlight_days, theme),
                update_width=True,
            )

            bar_rows.append((s, is_live, is_disabled, seconds))

        bar_width = self._fit_bar_width(table, col)
        for s, is_live, is_disabled, seconds in bar_rows:
            bar_fn = _live_bar_dashed if is_disabled else _live_bar
            bar_text = bar_fn(seconds, width=bar_width, max_secs=bar_max_secs) if is_live else " " * bar_width
            bar_style = theme.warning if (s in ad_alert_streamers or s in ffmpeg_error_streamers) else ""
            table.update_cell(s, col["bar"], Text(bar_text, style=bar_style), update_width=True)

        self._update_countdown()

    def _fit_bar_width(self, table: DataTable, col: dict) -> int:
        """Compute the widest the Bar column can be without forcing horizontal scroll."""
        available = table.container_size.width - table.scrollbar_size_vertical
        other = sum(
            table.columns[col[name]].get_render_width(table)
            for name in ("toggle", "name", "status", "duration", "last_live")
        )
        return max(0, available - other - 2 * table.cell_padding)

    def on_resize(self) -> None:
        """Recompute the Bar column width immediately when the panel is resized."""
        self.refresh_data()

    def _status_cell(self, is_live: bool, is_rec: bool, is_disabled: bool, theme) -> tuple[str, str]:
        """Return (text, style) for a row's non-blinking/base status state."""
        if not is_live:
            return ("off ", "dim")
        if is_disabled:
            return ("Live", "dim")
        if is_rec:
            return ("REC ", theme.error)
        return ("Live", theme.success)

    def _last_live_cell(self, s, is_live, recording_res, last_live, now, highlight_days, theme) -> Text:
        if is_live and s in recording_res:
            return Text(f"{recording_res[s]}p")
        if s not in last_live:
            return Text("-")
        elapsed_days = (now - last_live[s]) / 86400
        recent = highlight_days and elapsed_days <= highlight_days
        return Text(_fmt_duration(now - last_live[s]), style=theme.accent if recent else "dim")

    def _update_badges(self, all_streamers, live_since, recording, disabled) -> None:
        live_n = sum(1 for s in all_streamers if s in live_since)
        rec_n = len(recording)
        dis_n = len(disabled)
        off_n = len(all_streamers) - live_n
        theme = self.app.current_theme
        self.border_subtitle = (
            f" [{theme.success}]LIVE:{live_n}[/] "
            f"[{theme.error}]REC:{rec_n}[/] "
            f"[dim]OFF:{off_n}[/] "
            f"[dim italic]DIS:{dis_n}[/] "
        )

    def _update_countdown(self) -> None:
        if time.time() < self._countdown_message_until:
            return
        nxt = max(0.0, self._next_check_in)
        if nxt <= 0:
            frame = self.app.blink_frame % 3
            text = (".    ", "  .  ", "    .")[frame]
        else:
            text = f"{nxt:>4.0f}s"
        self.query_one("#countdown", Static).update(f"Next check: {text}")

    def apply_blink(self) -> None:
        """Flip blinking status cells and the waiting-countdown ellipsis frame."""
        if not self._blinking:
            if self._next_check_in <= 0:
                self._update_countdown()
            return
        table = self.query_one(DataTable)
        blink_on = self.app.blink_on
        for s, (text_a, style_a, text_b, style_b) in self._blinking.items():
            text, style = (text_a, style_a) if blink_on else (text_b, style_b)
            table.update_cell(s, self._col["status"], Text(text, style=style), update_width=True)
        if self._next_check_in <= 0:
            self._update_countdown()


class SitePanelGrid(Grid):
    """Auto-fit grid of SitePanels; column count follows available width."""

    MIN_PANEL_WIDTH = 59

    def on_mount(self) -> None:
        self._recompute_columns()

    def on_resize(self) -> None:
        self._recompute_columns()

    def _recompute_columns(self) -> None:
        cols = max(1, self.size.width // self.MIN_PANEL_WIDTH)
        cols = min(cols, len(self.children)) or 1
        self.styles.grid_size_columns = cols


class LogPane(Vertical):
    """Log tab: merged or per-site activity log, with a debug-line toggle."""

    DEFAULT_CSS = """
    LogPane #log-toolbar {
        height: 1;
    }
    LogPane #log-debug-switch {
        width: auto;
        margin: 0 1;
    }
    LogPane #log-debug-label {
        width: auto;
        color: $text-muted;
    }
    LogPane RichLog {
        height: 1fr;
    }
    """

    def __init__(self, sites_app: "AppState", **kwargs) -> None:
        super().__init__(**kwargs)
        self._sites_app = sites_app
        self._site_idx: "int | None" = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="log-toolbar"):
            yield Tabs(
                Tab("All", id="all"),
                *(Tab(_site_label(s), id=f"site-{i}") for i, s in enumerate(self._sites_app.sites)),
                id="log-site-tabs",
            )
            yield Switch(value=False, id="log-debug-switch")
            yield Static("Show debug", id="log-debug-label")
        yield RichLog(id="log-view", wrap=True, auto_scroll=False, max_lines=2000)

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Switch which site's log lines are shown."""
        if event.tabs.id != "log-site-tabs":
            return
        event.stop()
        self._site_idx = None if event.tab.id == "all" else int(event.tab.id.split("-")[1])
        is_all = self._site_idx is None
        self.query_one("#log-debug-switch", Switch).display = is_all
        self.query_one("#log-debug-label", Static).display = is_all
        self.refresh_data()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Toggle whether debug lines are merged into the 'All' site log."""
        if event.switch.id == "log-debug-switch":
            event.stop()
            self.refresh_data()

    def refresh_data(self) -> None:
        """Re-render the log view from the latest SiteState/AppState buffers."""
        show_debug = self.query_one("#log-debug-switch", Switch).value
        lines = _collect_log_lines(self._sites_app, self._site_idx, show_debug)
        theme = self.app.current_theme
        styles = [_log_line_style(line, theme) for line in lines]
        _redraw_log(self.query_one(RichLog), lines, styles)


class PipePane(Vertical):
    """Stdout/Stderr tab: per-site streamer list plus that streamer's raw output."""

    DEFAULT_CSS = """
    PipePane #pipe-toolbar {
        height: 1;
    }
    PipePane #pipe-show-all {
        width: auto;
        margin: 0 1;
    }
    PipePane #pipe-show-all-label {
        width: auto;
        color: $text-muted;
    }
    PipePane #pipe-body {
        height: 1fr;
    }
    PipePane #pipe-streamers {
        width: 26;
        border: round $panel;
    }
    PipePane #pipe-content {
        width: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    """

    def __init__(self, sites_app: "AppState", kind: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sites_app = sites_app
        self._kind = kind  # "stdout" or "stderr"
        self._site_idx = 0
        self._streamer_names: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="pipe-toolbar"):
            yield Tabs(
                *(Tab(_site_label(s), id=f"site-{i}") for i, s in enumerate(self._sites_app.sites)),
                id=f"pipe-site-tabs-{self._kind}",
            )
            yield Switch(value=False, id="pipe-show-all")
            yield Static("Show all", id="pipe-show-all-label")
        with Horizontal(id="pipe-body"):
            yield ListView(ListItem(Label("All Streamers")), id="pipe-streamers")
            yield RichLog(id="pipe-content", wrap=True, auto_scroll=False, max_lines=2000)

    def on_mount(self) -> None:
        self.query_one("#pipe-streamers", ListView).border_title = " STREAMERS "

    def _site(self) -> "SiteState | None":
        sites = self._sites_app.sites
        return sites[self._site_idx] if sites else None

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Switch which site's streamers/output are shown."""
        if event.tabs.id != f"pipe-site-tabs-{self._kind}":
            return
        event.stop()
        self._site_idx = int(event.tab.id.split("-")[1])
        self.query_one(ListView).index = 0
        site = self._site()
        if site is not None:
            attr = "show_checker_stdout" if self._kind == "stdout" else "show_checker_stderr"
            self.query_one("#pipe-show-all", Switch).value = getattr(site, attr)
        self.refresh_data(force_rebuild=True)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Toggle whether checker-only lines are mixed into 'All Streamers'."""
        if event.switch.id != "pipe-show-all":
            return
        event.stop()
        site = self._site()
        if site is not None:
            attr = "show_checker_stdout" if self._kind == "stdout" else "show_checker_stderr"
            setattr(site, attr, event.value)
        self.refresh_data()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Follow the highlighted streamer, like curses' arrow-key navigation."""
        if event.list_view.id == "pipe-streamers":
            self.refresh_data()

    def refresh_data(self, force_rebuild: bool = False) -> None:
        """Rebuild the streamer list (if changed) and re-render the content pane."""
        site = self._site()
        streamers = list(site.dash_all_streamers) if site is not None else []
        list_view = self.query_one(ListView)
        if force_rebuild or streamers != self._streamer_names:
            selected = list_view.index or 0
            self._streamer_names = streamers
            list_view.clear()
            list_view.append(ListItem(Label("All Streamers")))
            for name in streamers:
                list_view.append(ListItem(Label(name)))
            list_view.index = min(selected, len(streamers))

        sel_idx = list_view.index or 0
        show_all_switch = self.query_one("#pipe-show-all", Switch)
        show_all_switch.display = sel_idx == 0
        self.query_one("#pipe-show-all-label", Static).display = sel_idx == 0

        content = self.query_one("#pipe-content", RichLog)
        lines: list[str] = []
        title = f" {self._kind.upper()} "
        if site is not None:
            lines_attr = f"dash_{self._kind}_lines"
            by_streamer_attr = f"dash_{self._kind}_lines_by_streamer"
            prefix = _CHECKER_STDOUT_PREFIX if self._kind == "stdout" else _CHECKER_STDERR_PREFIX
            if sel_idx == 0:
                show_all = show_all_switch.value
                with site.dash_lock:
                    raw = list(getattr(site, lines_attr))
                if show_all:
                    lines = [ln[len(prefix):] if ln.startswith(prefix) else ln for ln in raw]
                else:
                    lines = [ln for ln in raw if not ln.startswith(prefix)]
                title = f" {self._kind.upper()} — Show All: {'ON' if show_all else 'OFF'} "
            else:
                streamer = streamers[sel_idx - 1] if sel_idx - 1 < len(streamers) else ""
                with site.dash_lock:
                    lines = list(getattr(site, by_streamer_attr).get(streamer, ()))
                title = f" {self._kind.upper()} — {streamer} "

        content.border_title = title
        _redraw_log(content, lines)


class ConfigPane(Vertical):
    """Edits each site's .conf file directly, with a Save button per site."""

    DEFAULT_CSS = """
    ConfigPane #config-site-tabs {
        height: 1;
    }
    ConfigPane TextArea {
        height: 1fr;
    }
    ConfigPane #config-toolbar {
        height: 3;
        align-horizontal: right;
    }
    """

    def __init__(self, sites_app: "AppState", **kwargs) -> None:
        super().__init__(**kwargs)
        self._sites_app = sites_app
        self._site_idx = 0

    def compose(self) -> ComposeResult:
        yield Tabs(
            *(Tab(_site_label(s), id=f"site-{i}") for i, s in enumerate(self._sites_app.sites)),
            id="config-site-tabs",
        )
        yield TextArea(id="config-editor", show_line_numbers=False)
        with Horizontal(id="config-toolbar"):
            yield Button("Save", id="config-save")

    def on_mount(self) -> None:
        self._load_file()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Switch which site's .conf file is loaded into the editor."""
        if event.tabs.id != "config-site-tabs":
            return
        event.stop()
        self._site_idx = int(event.tab.id.split("-")[1])
        self._load_file()

    def _load_file(self) -> None:
        """Read the selected site's .conf file into the editor."""
        sites = self._sites_app.sites
        if not sites:
            return
        try:
            with open(sites[self._site_idx].config_path, "r") as f:
                text = f.read()
        except OSError:
            text = ""
        self.query_one(TextArea).text = text

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Write the editor's contents back to the selected site's .conf file."""
        if event.button.id != "config-save":
            return
        event.stop()
        sites = self._sites_app.sites
        if not sites:
            return
        path = sites[self._site_idx].config_path
        with open(path, "w") as f:
            f.write(self.query_one(TextArea).text)
        self.notify(f"Saved {os.path.basename(path)}")


class FileManagerPane(Vertical):
    """Browses each site's output directory.

    file_manager.FileManagerTab's move/fixup/trim/split actions live in a
    module that wasn't available to port, so this is a read-only browser
    built from Textual's default DirectoryTree.
    """

    DEFAULT_CSS = """
    FileManagerPane #filemanager-site-tabs {
        height: 1;
    }
    FileManagerPane DirectoryTree {
        height: 1fr;
    }
    """

    RELOAD_INTERVAL = 5.0

    def __init__(self, sites_app: "AppState", **kwargs) -> None:
        super().__init__(**kwargs)
        self._sites_app = sites_app

    def compose(self) -> ComposeResult:
        yield Tabs(
            *(Tab(_site_label(s), id=f"site-{i}") for i, s in enumerate(self._sites_app.sites)),
            id="filemanager-site-tabs",
        )
        yield DirectoryTree(self._output_dir(0), id="filemanager-tree")

    def on_mount(self) -> None:
        self.set_interval(self.RELOAD_INTERVAL, self._reload)

    def _output_dir(self, site_idx: int) -> str:
        sites = self._sites_app.sites
        if not sites:
            return os.getcwd()
        path = sites[site_idx].get_cached_config().get("output_dir")
        return path if path and os.path.isdir(path) else os.getcwd()

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Point the tree at the newly selected site's output directory."""
        if event.tabs.id != "filemanager-site-tabs":
            return
        event.stop()
        site_idx = int(event.tab.id.split("-")[1])
        self.query_one(DirectoryTree).path = self._output_dir(site_idx)

    def _reload(self) -> None:
        self.query_one(DirectoryTree).reload()


class JJDlpApp(App):
    """Main dashboard application."""

    # Use Textual theme variables throughout.
    #
    # This means the dashboard automatically follows whichever Textual
    # theme is active, with Gruvbox as the default.
    CSS = """
    Screen {
        background: $background;
        color: $text;
    }

    #main-area {
        padding: 0 1;
        height: 1fr;
    }

    Header {
        height: 7;
        padding: 1 0 0 1;
        background: $background;
        color: $text;
    }

    #logo {
        color: $accent;
        text-style: bold;
        width: auto;
        height: auto;
    }

    #header-info {
        width: 1fr;
        height: auto;
        align-horizontal: right;
    }

    #header-time {
        width: auto;
        color: $text-primary;
    }

    #header-version {
        width: auto;
        color: $text-muted;
    }

    #tab-spacer {
        height: 1;
        background: $background;
    }

    Tabs {
        height: 1;
        padding: 0 0 0 1;
        background: $background;
        color: $text;
    }

    Separator {
        height: 1;
        color: $border;
        background: $background;
    }

    #body {
        height: 1fr;
    }

    ContentSwitcher {
        width: 1fr;
        height: 100%;
        background: $background;
    }

    .tab-pane {
        width: 100%;
        height: 100%;
        background: $background;
        color: $text;
    }

    SystemPanel {
        width: 28;
        height: 100%;
        padding: 1;
    }

    """

    # Keep theme switching available.
    #
    # C cycles through the themes available in the installed Textual
    # version.  Gruvbox is the starting theme.
    BINDINGS = [
        Binding(
            "c",
            "cycle_theme",
            "Theme",
            tooltip="Cycle through available Textual themes",
        ),
        Binding(
            "q",
            "quit",
            "Quit",
            tooltip="Quit the application",
        ),
    ]

    def __init__(self, app: "AppState", global_cfg: dict | None = None) -> None:
        super().__init__()
        # Named to mirror curses_dashboard.run_dashboard(app, global_cfg=...);
        # stored as self._sites_app to avoid clashing with Textual's own
        # Widget.app property.
        self._sites_app = app
        self._global_cfg = global_cfg or {}
        # Shared blink state so every SitePanel flashes in lockstep, matching
        # curses_dashboard's single shared self.tick counter.
        self.blink_on = True
        self.blink_frame = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="main-area"):
            yield Header()
            yield Static(id="tab-spacer")
            yield Tabs(
                *(Tab(TAB_LABELS[tid], id=tid) for tid in TAB_IDS)
            )
            yield Separator()

            with Horizontal(id="body"):
                with ContentSwitcher(initial=TAB_IDS[0]):
                    with SitePanelGrid(id="dashboard"):
                        for site in self._sites_app.sites:
                            yield SitePanel(site)
                    yield LogPane(self._sites_app, id="log", classes="tab-pane")
                    yield PipePane(self._sites_app, "stdout", id="stdout", classes="tab-pane")
                    yield PipePane(self._sites_app, "stderr", id="stderr", classes="tab-pane")
                    yield ConfigPane(self._sites_app, id="config", classes="tab-pane")
                    yield FileManagerPane(self._sites_app, id="filemanager", classes="tab-pane")

                yield SystemPanel()

    def on_mount(self) -> None:
        # Gruvbox is the default theme.
        #
        # Textual exposes the built-in theme by this name in versions
        # which include it.
        self.theme = "atom-one-dark"

        self.set_interval(DATA_POLL_INTERVAL, self._data_tick)
        self.set_interval(BLINK_INTERVAL, self._blink_tick)

    def _data_tick(self) -> None:
        """Poll SiteState and refresh every panel's table. See POLLING DECISION above."""
        for panel in self.query(SitePanel):
            panel.refresh_data()
        for pane in self.query(LogPane):
            pane.refresh_data()
        for pane in self.query(PipePane):
            pane.refresh_data()

    def _blink_tick(self) -> None:
        """Flip the shared blink flag/frame and let panels re-render blinking cells."""
        self.blink_on = not self.blink_on
        self.blink_frame = (self.blink_frame + 1) % 3
        for panel in self.query(SitePanel):
            panel.apply_blink()

    def action_cycle_theme(self) -> None:
        """Cycle through the themes provided by the installed Textual version."""

        themes = list(self.available_themes.keys())

        if not themes:
            return

        current = self.theme

        try:
            index = themes.index(current)
        except ValueError:
            index = -1

        self.theme = themes[(index + 1) % len(themes)]

    def on_tabs_tab_activated(
        self,
        event: Tabs.TabActivated,
    ) -> None:
        tab_id = event.tab.id

        if tab_id is None:
            return

        self.query_one(ContentSwitcher).current = tab_id

        # The Log tab uses the full width so its text stays easy to select;
        # every other tab keeps the system sidebar.
        self.query_one(SystemPanel).display = tab_id != "log"


def run_dashboard(app: "AppState", global_cfg: dict | None = None) -> None:
    """Entry point mirroring curses_dashboard.run_dashboard's signature."""
    JJDlpApp(app, global_cfg=global_cfg).run()


if __name__ == "__main__":
    raise SystemExit(
        "textual_dashboard.py now requires a real AppState; run it via "
        "main.py's dashboard launch path (run_dashboard(app, global_cfg=...)), "
        "not standalone."
    )