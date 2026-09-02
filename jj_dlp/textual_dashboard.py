#!/usr/bin/env python3
"""jj-dlp — Textual port, framework-only demo.

Uses Textual's built-in Gruvbox theme as the default theme and relies on
Textual theme variables rather than hard-coded colors.

The dashboard can switch between the themes available in the installed
version of Textual.
"""

from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Static,
    Tabs,
    Tab,
    ContentSwitcher,
)


__version__ = "1.28.11"


# Matches ASCII_LOGO in main.py (6 lines).
ASCII_LOGO = r"""     __     __              .___.__          
     |__|   |__|           __| _/|  | ______  
     |  |   |  |  ______  / __ | |  | \____ \ 
     |  |   |  | /_____/ / /_/ | |  |_|  |_> >
 /\__|  /\__|  |         \____ | |____/   __/ 
 \______\______|              \/      |__|    """


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

    Tabs Underline {
        display: none;
    }

    Tab {
        background: $background;
        color: $text-secondary;
        text-style: bold;
        padding: 0 2;
        margin-right: 1;
    }

    Tab:hover {
        background: $surface;
        color: $text;
    }

    Tab.-active {
        background: $primary;
        color: $background;
        text-style: bold;
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
        border: solid $secondary;
        background: $panel;
        color: $text-secondary;
        text-style: bold;
        padding: 1;
    }

    FooterHints {
        height: 1;
        background: $primary;
        color: $background;
        dock: bottom;
        text-style: bold;
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
                    for tid in TAB_IDS:
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


if __name__ == "__main__":
    JJDlpApp().run()