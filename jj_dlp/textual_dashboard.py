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
from textual.reactive import reactive
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Static,
    Tabs,
    Tab,
    ContentSwitcher,
)

# Safe at module load time: main.py only imports this module lazily, inside
# a function, by which point main.py itself has already finished importing.
from .main import _modify_config_streamer

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
    dashed = ("─ " * filled)[:width]
    return dashed + "─" * (width - len(dashed))


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


# Per-tab footer hints, mirrors the curses draw_footer() switch statement.
TAB_HINTS = {
    "dashboard": (
        "  LEFT/RIGHT: switch tabs  [: prev site  ]: next site  "
        "A: add/enable streamer R: remove streamer D: disable streamer  "
        "S: Sort  C: Colors  Q: quit  "
    ),
    "log": (
        "  LEFT/RIGHT: switch tabs  [: prev site  ]: next site  "
        "UP: scroll up  DOWN: scroll down  C: Colors  Q: quit  "
    ),
    "stdout": (
        "  LEFT/RIGHT: switch tabs  [: prev site  ]: next site  "
        "Tab: switch panel  UP/DOWN: select streamer  A: Show All [OFF]  "
        "C: Colors  Q: quit  "
    ),
    "stderr": (
        "  LEFT/RIGHT: switch tabs  [: prev site  ]: next site  "
        "Tab: switch panel  UP/DOWN: select streamer  A: Show All [OFF]  "
        "C: Colors  Q: quit  "
    ),
    "config": (
        "  LEFT/RIGHT: switch tabs  [: prev site  ]: next site  "
        "Tab: Next Panel  G: Changelog  C: Colors  N: Theme Manager  Q: quit  "
    ),
    "filemanager": (
        "  ↑↓: select  Space: show folder  DEL: delete  S: sort  "
        "T: toggle trash  C: Colors  Q: quit  "
    ),
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


class FooterHints(Static):
    """Bottom hint bar; text changes per active tab."""

    hint_text = reactive("")

    def watch_hint_text(self, text: str) -> None:
        self.update(text)


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

    def compose(self) -> ComposeResult:
        yield DataTable(cursor_type="cell", id="streamer-table")
        with Horizontal(id="add-row"):
            yield Input(placeholder="Add streamer...", id="add-input")
            yield Button("Add", id="add-button", variant="success")
        yield Static("", id="countdown")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        keys = table.add_columns("Name", "Status", "Bar", "Duration", "Last Live")
        self._col = dict(zip(("name", "status", "bar", "duration", "last_live"), keys))
        self.refresh_data()

    def action_focus_add_input(self) -> None:
        self.query_one("#add-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "add-input":
            self._add_streamer(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-button":
            self._add_streamer(self.query_one("#add-input", Input).value)

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
            blocked = set(site.dash_blocked)
            self._next_check_in = site.dash_next_check_in

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
        self._update_badges(all_streamers, live_since, recording, blocked)

        table = self.query_one(DataTable)
        col = self._col
        new_keys = set(all_streamers)

        for s in new_keys - self._row_keys:
            table.add_row("", "", "", "", "", key=s)
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
            is_disabled = s in blocked
            seconds = now - live_since.get(s, now) if is_live else 0.0

            name_style = "dim" if is_disabled else ""
            table.update_cell(s, col["name"], Text(s, style=name_style), update_width=True)

            status_a, style_a = self._status_cell(is_live, is_rec, is_disabled, theme)
            if is_live and is_disabled:
                status_b, style_b = "[x  DIS]", "dim"
                self._blinking[s] = (status_a, style_a, status_b, style_b)
            elif is_live and is_rec:
                status_a, style_a = "[● Live]", theme.success
                status_b, style_b = "[►  REC]", theme.error
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
            for name in ("name", "status", "duration", "last_live")
        )
        return max(0, available - other - 2 * table.cell_padding)

    def on_resize(self) -> None:
        """Recompute the Bar column width immediately when the panel is resized."""
        self.refresh_data()

    def _status_cell(self, is_live: bool, is_rec: bool, is_disabled: bool, theme) -> tuple[str, str]:
        """Return (text, style) for a row's non-blinking/base status state."""
        if not is_live:
            return ("[○  off]", "dim")
        if is_disabled:
            return ("[● Live]", theme.success)
        if is_rec:
            return ("[►  REC]", theme.error)
        return ("[● Live]", theme.success)

    def _last_live_cell(self, s, is_live, recording_res, last_live, now, highlight_days, theme) -> Text:
        if is_live and s in recording_res:
            return Text(f"{recording_res[s]}p")
        if s not in last_live:
            return Text("never", style="dim")
        elapsed_days = (now - last_live[s]) / 86400
        recent = highlight_days and elapsed_days <= highlight_days
        return Text(_fmt_duration(now - last_live[s]) + " ago", style=theme.accent if recent else "dim")

    def _update_badges(self, all_streamers, live_since, recording, blocked) -> None:
        live_n = sum(1 for s in all_streamers if s in live_since)
        rec_n = len(recording)
        dis_n = len(blocked)
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

    FooterHints {
        height: 1;
        dock: bottom;
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
                    for tid in TAB_IDS:
                        if tid != "dashboard":
                            yield Static("", id=tid, classes="tab-pane")

                yield SystemPanel()

        yield FooterHints()

    def on_mount(self) -> None:
        # Gruvbox is the default theme.
        #
        # Textual exposes the built-in theme by this name in versions
        # which include it.
        self.theme = "gruvbox"

        self.query_one(FooterHints).hint_text = TAB_HINTS[TAB_IDS[0]]

        self.set_interval(DATA_POLL_INTERVAL, self._data_tick)
        self.set_interval(BLINK_INTERVAL, self._blink_tick)

    def _data_tick(self) -> None:
        """Poll SiteState and refresh every panel's table. See POLLING DECISION above."""
        for panel in self.query(SitePanel):
            panel.refresh_data()

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
        self.query_one(FooterHints).hint_text = TAB_HINTS.get(
            tab_id,
            "",
        )

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