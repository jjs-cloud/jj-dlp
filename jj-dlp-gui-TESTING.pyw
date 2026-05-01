#!/usr/bin/env python3
# jj-dlp-gui.pyw  —  GUI shell for jj-dlp
#
# Architecture notes (for future contributors):
# ─────────────────────────────────────────────
#  • The app is built on tkinter with ttk styling — no extra dependencies.
#  • The main window is a Notebook (tab container). Adding a new major feature
#    means adding a new Tab class (subclass of BaseTab) and registering it in
#    App.build_tabs().  See BaseTab below.
#  • A single AppState object holds shared runtime state.  Tabs communicate
#    through AppState, not directly with each other.  Use AppState.subscribe()
#    to react to state changes in a decoupled way.
#  • Background work goes in daemon threads; GUI updates must be posted back
#    with self.after() — tkinter is NOT thread-safe.
#  • Config loading is handled by ConfigManager (wraps jj-dlp's INI format).
#    Future tabs (e.g. config editor) should use ConfigManager too.
#  • The StatusBar at the bottom is always visible. Post status messages via
#    app.status_bar.set("message").
#
# Planned future tabs (stubs already registered, just un-comment):
#   • Terminal   — embedded terminal panels, one per streamer
#   • Config     — in-GUI editor for jj-dlp.conf / tiktok.conf / etc.
#   • Recordings — browse/play saved recordings
#   • TikTok     — your tiktok-post-checker script
#   • Settings   — app-level prefs, theme toggle, paths


import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import configparser
import os
import sys
import time
import threading
import subprocess
from datetime import datetime
from typing import Optional, Callable


# ══════════════════════════════════════════════════════════════════════════════
#  THEME  —  edit these constants to restyle the whole app
# ══════════════════════════════════════════════════════════════════════════════

THEME = {
    # Background layers
    "bg":           "#0d0f14",   # deepest background
    "bg_panel":     "#13161e",   # panel / card bg
    "bg_row":       "#191c26",   # alternating table row
    "bg_row_alt":   "#141720",
    "bg_input":     "#1e2130",

    # Accent & status
    "accent":       "#4f9eff",   # primary accent (blue)
    "accent_dim":   "#2a4a7a",
    "live":         "#39ff9e",   # neon green — streamer live
    "live_dim":     "#1a4a38",
    "offline":      "#5a5f70",   # muted grey — streamer offline
    "warn":         "#ffc14f",   # yellow warning
    "danger":       "#ff5f5f",   # red / error
    "recording":    "#ff6b3d",   # recording indicator

    # Text
    "fg":           "#dce1f0",
    "fg_dim":       "#7a8099",
    "fg_title":     "#ffffff",

    # Geometry
    "radius":       8,
    "pad":          12,
    "tab_height":   38,
    "row_height":   36,

    # Font  (monospace for status values, sans for labels)
    "font_ui":      ("Segoe UI", 10),
    "font_ui_bold": ("Segoe UI", 10, "bold"),
    "font_title":   ("Segoe UI", 13, "bold"),
    "font_mono":    ("Consolas", 10),
    "font_small":   ("Segoe UI", 8),
    "font_badge":   ("Segoe UI", 8, "bold"),
}

T = THEME   # shorthand


# ══════════════════════════════════════════════════════════════════════════════
#  APP STATE  —  shared runtime state; tabs subscribe to changes
# ══════════════════════════════════════════════════════════════════════════════

class AppState:
    """Central state object.  Tabs read from here; background threads write here."""

    def __init__(self):
        self.config_path: str = ""           # path to active jj-dlp.conf
        self.config: dict = {}               # parsed config dict
        self.streamers: list = []            # list of monitored streamer names
        self.live_since: dict = {}           # streamer -> epoch float | None
        self.recording: set = set()          # streamers currently recording
        self.daemon_running: bool = False    # True when jj-dlp backend is running
        self.daemon_proc = None              # subprocess.Popen | None
        self.last_check: float = 0          # epoch of last liveness check
        self.next_check_in: float = 0       # seconds until next check

        self._lock = threading.Lock()
        self._subscribers: list = []        # list of (event_name, callback)

    # ── pub/sub ────────────────────────────────────────────────────────────────
    def subscribe(self, event: str, callback: Callable):
        """Register a callback for a named event.  Called from GUI thread."""
        self._subscribers.append((event, callback))

    def publish(self, event: str, data=None):
        """Fire an event.  Safe to call from any thread — posts to Tk event loop."""
        for ev, cb in self._subscribers:
            if ev == event:
                try:
                    cb(data)
                except Exception as e:
                    print(f"[AppState] subscriber error ({event}): {e}")

    # ── convenience mutators ───────────────────────────────────────────────────
    def update_streamers(self, streamers: list, live_since: dict, recording: set):
        with self._lock:
            self.streamers = list(streamers)
            self.live_since = dict(live_since)
            self.recording = set(recording)

    def set_config(self, path: str, cfg: dict):
        with self._lock:
            self.config_path = path
            self.config = dict(cfg)
            self.streamers = list(cfg.get("streamers", []))
            self.live_since = {s: None for s in self.streamers}


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG MANAGER  —  reads jj-dlp INI format
# ══════════════════════════════════════════════════════════════════════════════

class ConfigManager:
    """Thin wrapper around configparser that understands jj-dlp's INI layout."""

    @staticmethod
    def load(path: str) -> dict:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Config not found: {path}")

        parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
        parser.read(path, encoding="utf-8")

        def _items(section):
            if not parser.has_section(section):
                return []
            return [k.strip().lower() for k, _ in parser.items(section) if k.strip()]

        def _get(section, key, default=""):
            if not parser.has_section(section):
                return default
            return parser[section].get(key, default).strip().strip("\"'")

        def _int(section, key, default):
            try:
                return int(_get(section, key, str(default)))
            except Exception:
                return default

        general = parser["General"] if parser.has_section("General") else {}

        return {
            "streamers":            _items("Streamers"),
            "blocked":              _items("Block"),
            "check_interval":       _int("General", "CHECK_INTERVAL", 60),
            "output_dir":           _get("General", "OUTPUT_DIR", "recordings"),
            "output_tmpl":          _get("General", "OUTPUT_TMPL", "%(title)s [%(id)s].%(ext)s"),
            "cooldown":             _int("General", "COOLDOWN_AFTER_RECORDING", 5),
            "stall_timeout":        _int("General", "STALL_TIMEOUT", 120),
            "yt_dlp_path":          _get("General", "YT_DLP_PATH", "yt-dlp") or "yt-dlp",
            "site_tmpl":            _get("General", "SITE_TMPL", ""),
            "verbosity":            _int("General", "VERBOSITY", 1),
            "logging_enabled":      _get("General", "LOGGING", "false").lower() == "true",
            "log_path":             _get("General", "LOG_PATH", ""),
            "popup_notifications":  _get("General", "POPUP_NOTIFICATIONS", "true").lower() == "true",
            "debug_logs":           _get("General", "DEBUG_LOGS", "false").lower() == "true",
            "twitch_enabled":       bool(_get("Twitch", "CLIENT_ID", "")),
            "config_path":          path,
        }

    @staticmethod
    def add_streamer(path: str, name: str) -> None:
        parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
        parser.read(path, encoding="utf-8")
        if not parser.has_section("Streamers"):
            parser.add_section("Streamers")
        parser.set("Streamers", name.lower(), None)
        with open(path, "w", encoding="utf-8") as f:
            parser.write(f)

    @staticmethod
    def remove_streamer(path: str, name: str) -> None:
        parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
        parser.read(path, encoding="utf-8")
        if parser.has_section("Streamers"):
            parser.remove_option("Streamers", name.lower())
        with open(path, "w", encoding="utf-8") as f:
            parser.write(f)


# ══════════════════════════════════════════════════════════════════════════════
#  BASE TAB  —  all feature tabs inherit from this
# ══════════════════════════════════════════════════════════════════════════════

class BaseTab(tk.Frame):
    """
    Base class for a feature tab.

    Subclasses implement:
        build()          — build all widgets inside self
        on_activate()    — called when this tab becomes visible (optional)
        on_deactivate()  — called when leaving this tab (optional)

    Access shared state via self.state (AppState).
    Post GUI updates via self.after(ms, callback).
    """

    def __init__(self, parent, app, state: AppState, label: str):
        super().__init__(parent, bg=T["bg"])
        self.app = app
        self.state = state
        self.label = label
        self.build()

    def build(self):
        raise NotImplementedError

    def on_activate(self):
        pass

    def on_deactivate(self):
        pass

    # ── helpers ────────────────────────────────────────────────────────────────
    def section_label(self, parent, text: str) -> tk.Label:
        lbl = tk.Label(parent, text=text.upper(), font=T["font_small"],
                       bg=T["bg_panel"], fg=T["fg_dim"],
                       anchor="w", padx=T["pad"])
        return lbl

    def card(self, parent, **kw) -> tk.Frame:
        defaults = dict(bg=T["bg_panel"], padx=T["pad"], pady=T["pad"])
        defaults.update(kw)
        return tk.Frame(parent, **defaults)


# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD TAB  —  live streamer status cards + daemon control
#  This is the one feature included in the v1 shell.
# ══════════════════════════════════════════════════════════════════════════════

class DashboardTab(BaseTab):
    """
    Shows live/offline status for every monitored streamer.
    Polls for status updates on its own timer; will connect to the
    jj-dlp backend process in a future update.
    """

    PULSE_INTERVAL_MS = 1000   # refresh the countdown + durations every second
    CHECK_INTERVAL_MS = 30_000  # simulate a liveness check every 30 s (demo)

    def build(self):
        self._streamer_rows: dict = {}   # name -> dict of tk widgets
        self._next_check_after = None

        # ── top toolbar ───────────────────────────────────────────────────────
        toolbar = tk.Frame(self, bg=T["bg"], pady=8, padx=T["pad"])
        toolbar.pack(fill="x")

        title_lbl = tk.Label(toolbar, text="Dashboard",
                             font=T["font_title"], bg=T["bg"], fg=T["fg_title"])
        title_lbl.pack(side="left")

        # Daemon start/stop button (right side)
        self._daemon_btn = tk.Button(
            toolbar, text="▶  Start Daemon",
            font=T["font_ui_bold"],
            bg=T["accent_dim"], fg=T["accent"],
            activebackground=T["accent"], activeforeground=T["bg"],
            relief="flat", padx=14, pady=5, cursor="hand2",
            command=self._toggle_daemon
        )
        self._daemon_btn.pack(side="right", padx=(0, 4))

        # Force-check button
        self._check_btn = tk.Button(
            toolbar, text="⟳  Check Now",
            font=T["font_ui"],
            bg=T["bg_input"], fg=T["fg"],
            activebackground=T["bg_row"], activeforeground=T["fg_title"],
            relief="flat", padx=10, pady=5, cursor="hand2",
            command=self._manual_check
        )
        self._check_btn.pack(side="right", padx=(0, 8))

        # ── countdown bar ─────────────────────────────────────────────────────
        bar_frame = tk.Frame(self, bg=T["bg_panel"], pady=6, padx=T["pad"])
        bar_frame.pack(fill="x")

        self._status_dot = tk.Label(bar_frame, text="●", font=("Segoe UI", 9),
                                    bg=T["bg_panel"], fg=T["offline"])
        self._status_dot.pack(side="left")

        self._status_lbl = tk.Label(bar_frame, text="  No config loaded",
                                    font=T["font_ui"], bg=T["bg_panel"], fg=T["fg_dim"])
        self._status_lbl.pack(side="left")

        self._countdown_lbl = tk.Label(bar_frame, text="",
                                       font=T["font_mono"], bg=T["bg_panel"], fg=T["fg_dim"])
        self._countdown_lbl.pack(side="right")

        # thin separator
        sep = tk.Frame(self, bg=T["accent_dim"], height=1)
        sep.pack(fill="x")

        # ── streamer list area ────────────────────────────────────────────────
        list_outer = tk.Frame(self, bg=T["bg"])
        list_outer.pack(fill="both", expand=True, padx=T["pad"], pady=T["pad"])

        # Column headers
        hdr = tk.Frame(list_outer, bg=T["bg"])
        hdr.pack(fill="x", pady=(0, 4))
        for text, anchor, width in [
            ("STREAMER",   "w", 180),
            ("STATUS",     "w", 90),
            ("LIVE FOR",   "w", 100),
            ("RECORDING",  "w", 100),
            ("PLATFORM",   "w", 120),
        ]:
            tk.Label(hdr, text=text, font=T["font_small"], bg=T["bg"],
                     fg=T["fg_dim"], anchor=anchor, width=width // 8,
                     padx=6).pack(side="left")

        sep2 = tk.Frame(list_outer, bg=T["bg_row"], height=1)
        sep2.pack(fill="x", pady=(0, 2))

        # Scrollable frame for streamer rows
        canvas = tk.Canvas(list_outer, bg=T["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        self._rows_frame = tk.Frame(canvas, bg=T["bg"])

        self._rows_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ── empty state ───────────────────────────────────────────────────────
        self._empty_lbl = tk.Label(
            self._rows_frame,
            text="No config loaded.\nUse File → Open Config to get started.",
            font=T["font_ui"], bg=T["bg"], fg=T["fg_dim"],
            justify="center", pady=40
        )
        self._empty_lbl.pack()

        # ── subscribe to state events ─────────────────────────────────────────
        self.state.subscribe("config_loaded", self._on_config_loaded)
        self.state.subscribe("streamers_updated", self._on_streamers_updated)

        # ── start the pulse timer ─────────────────────────────────────────────
        self._last_check_time: float = 0
        self._check_interval: int = 60
        self._pulse()

    # ── toolbar actions ────────────────────────────────────────────────────────

    def _toggle_daemon(self):
        if self.state.daemon_running:
            self._stop_daemon()
        else:
            self._start_daemon()

    def _start_daemon(self):
        if not self.state.config_path:
            messagebox.showwarning("No Config", "Please load a config file first.")
            return

        script_dir = os.path.dirname(os.path.abspath(__file__))
        jj_script = os.path.join(script_dir, "jj-dlp.py")

        if not os.path.isfile(jj_script):
            messagebox.showerror("Not Found",
                f"jj-dlp.py not found in:\n{script_dir}\n\n"
                "Place jj-dlp-gui.pyw in the same folder as jj-dlp.py.")
            return

        try:
            proc = subprocess.Popen(
                [sys.executable, jj_script, "--config", self.state.config_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            self.state.daemon_proc = proc
            self.state.daemon_running = True
            self._update_daemon_button()
            self.app.status_bar.set(f"Daemon started  (PID {proc.pid})")

            # Future: pipe stdout into the Terminal tab
            threading.Thread(target=self._drain_daemon_output,
                             args=(proc,), daemon=True).start()
        except Exception as e:
            messagebox.showerror("Launch Failed", str(e))

    def _stop_daemon(self):
        proc = self.state.daemon_proc
        if proc and proc.poll() is None:
            proc.terminate()
            self.app.status_bar.set("Daemon stopped.")
        self.state.daemon_running = False
        self.state.daemon_proc = None
        self._update_daemon_button()

    def _drain_daemon_output(self, proc):
        """Read daemon stdout — piped to Terminal tab in future."""
        for line in proc.stdout:
            pass   # TODO: post to Terminal tab log
        self.after(0, self._on_daemon_exited)

    def _on_daemon_exited(self):
        self.state.daemon_running = False
        self.state.daemon_proc = None
        self._update_daemon_button()
        self.app.status_bar.set("Daemon exited.")

    def _update_daemon_button(self):
        if self.state.daemon_running:
            self._daemon_btn.config(text="■  Stop Daemon",
                                    bg=T["live_dim"], fg=T["live"])
        else:
            self._daemon_btn.config(text="▶  Start Daemon",
                                    bg=T["accent_dim"], fg=T["accent"])

    def _manual_check(self):
        """Trigger an immediate simulated check cycle."""
        self._last_check_time = 0
        self.app.status_bar.set("Manual check triggered…")

    # ── state event handlers ───────────────────────────────────────────────────

    def _on_config_loaded(self, cfg: dict):
        """Called (on GUI thread) when a new config is loaded."""
        self._check_interval = cfg.get("check_interval", 60)
        self._rebuild_streamer_rows(cfg.get("streamers", []))
        self._status_dot.config(fg=T["accent"])
        self._status_lbl.config(
            text=f"  {os.path.basename(cfg['config_path'])}   "
                 f"·  {len(cfg['streamers'])} streamers",
            fg=T["fg"]
        )

    def _on_streamers_updated(self, data):
        """Called when live/recording status changes."""
        self._rebuild_streamer_rows(self.state.streamers)

    # ── streamer row management ────────────────────────────────────────────────

    def _rebuild_streamer_rows(self, streamers: list):
        """Rebuild the streamer card list from scratch."""
        # Clear existing rows
        for child in self._rows_frame.winfo_children():
            child.destroy()
        self._streamer_rows.clear()

        if not streamers:
            self._empty_lbl = tk.Label(
                self._rows_frame,
                text="No streamers configured.\nEdit your config to add some.",
                font=T["font_ui"], bg=T["bg"], fg=T["fg_dim"],
                justify="center", pady=40
            )
            self._empty_lbl.pack()
            return

        for i, name in enumerate(streamers):
            row_bg = T["bg_row"] if i % 2 == 0 else T["bg_row_alt"]
            self._make_streamer_row(name, row_bg)

    def _make_streamer_row(self, name: str, bg: str):
        """Create one streamer status row."""
        row = tk.Frame(self._rows_frame, bg=bg, pady=0)
        row.pack(fill="x", pady=1)

        # Live indicator dot
        dot = tk.Label(row, text="●", font=("Segoe UI", 11),
                       bg=bg, fg=T["offline"], width=2)
        dot.pack(side="left", padx=(8, 0))

        # Name
        name_lbl = tk.Label(row, text=name, font=T["font_ui_bold"],
                             bg=bg, fg=T["fg_title"], anchor="w", width=20)
        name_lbl.pack(side="left", padx=6)

        # Status badge
        status_lbl = tk.Label(row, text="OFFLINE", font=T["font_badge"],
                               bg=T["bg"], fg=T["offline"],
                               padx=8, pady=2, relief="flat")
        status_lbl.pack(side="left", padx=(0, 12))

        # Live duration
        duration_lbl = tk.Label(row, text="—", font=T["font_mono"],
                                 bg=bg, fg=T["fg_dim"], width=10, anchor="w")
        duration_lbl.pack(side="left")

        # Recording badge
        rec_lbl = tk.Label(row, text="", font=T["font_badge"],
                            bg=bg, fg=T["recording"], width=12, anchor="w")
        rec_lbl.pack(side="left", padx=(0, 12))

        # Platform (derived from SITE_TMPL in future; just a placeholder)
        site = self._guess_platform(name)
        platform_lbl = tk.Label(row, text=site, font=T["font_small"],
                                 bg=bg, fg=T["fg_dim"], anchor="w")
        platform_lbl.pack(side="left")

        self._streamer_rows[name] = {
            "row": row, "dot": dot, "status": status_lbl,
            "duration": duration_lbl, "rec": rec_lbl, "bg": bg,
        }

    def _guess_platform(self, name: str) -> str:
        site = self.state.config.get("site_tmpl", "")
        if "twitch" in site.lower():
            return "Twitch"
        if "youtube" in site.lower():
            return "YouTube"
        if "kick" in site.lower():
            return "Kick"
        return ""

    # ── pulse timer — updates countdown + durations every second ──────────────

    def _pulse(self):
        now = time.time()
        since_check = now - self._last_check_time if self._last_check_time else 0
        remaining = max(0, self._check_interval - since_check)

        if remaining == 0 and self._last_check_time > 0:
            self._countdown_lbl.config(text="checking…", fg=T["warn"])
        else:
            mins, secs = divmod(int(remaining), 60)
            label = f"next check  {mins:02d}:{secs:02d}" if self._last_check_time else ""
            self._countdown_lbl.config(text=label, fg=T["fg_dim"])

        # Update per-row live durations
        live_since = self.state.live_since
        recording = self.state.recording

        for name, widgets in self._streamer_rows.items():
            since = live_since.get(name)
            is_recording = name in recording
            bg = widgets["bg"]

            if since is not None:
                elapsed = now - since
                dur_text = _fmt_duration(elapsed)
                widgets["dot"].config(fg=T["live"])
                widgets["status"].config(text="LIVE", bg=T["live_dim"], fg=T["live"])
                widgets["duration"].config(text=dur_text, fg=T["live"])
                widgets["rec"].config(
                    text="⏺ REC" if is_recording else "",
                    fg=T["recording"]
                )
            else:
                widgets["dot"].config(fg=T["offline"])
                widgets["status"].config(text="OFFLINE", bg=T["bg"], fg=T["offline"])
                widgets["duration"].config(text="—", fg=T["fg_dim"])
                widgets["rec"].config(text="")

        self.after(self.PULSE_INTERVAL_MS, self._pulse)


# ══════════════════════════════════════════════════════════════════════════════
#  PLACEHOLDER TABS  —  stubs for planned features
# ══════════════════════════════════════════════════════════════════════════════

class PlaceholderTab(BaseTab):
    """Generic stub for a feature tab not yet implemented."""

    def __init__(self, parent, app, state, label, description="", roadmap=None):
        self._description = description
        self._roadmap = roadmap or []
        super().__init__(parent, app, state, label)

    def build(self):
        outer = tk.Frame(self, bg=T["bg"])
        outer.pack(expand=True, fill="both")

        inner = tk.Frame(outer, bg=T["bg"])
        inner.place(relx=0.5, rely=0.4, anchor="center")

        tk.Label(inner, text=self.label, font=T["font_title"],
                 bg=T["bg"], fg=T["fg_title"]).pack()

        if self._description:
            tk.Label(inner, text=self._description, font=T["font_ui"],
                     bg=T["bg"], fg=T["fg_dim"], pady=6).pack()

        if self._roadmap:
            tk.Label(inner, text="Planned features:", font=T["font_small"],
                     bg=T["bg"], fg=T["fg_dim"], pady=(10, 2)).pack()
            for item in self._roadmap:
                tk.Label(inner, text=f"  •  {item}", font=T["font_small"],
                         bg=T["bg"], fg=T["fg_dim"]).pack(anchor="w")

        tk.Label(inner, text="Coming soon", font=T["font_badge"],
                 bg=T["accent_dim"], fg=T["accent"],
                 padx=12, pady=4).pack(pady=(20, 0))


# ══════════════════════════════════════════════════════════════════════════════
#  STATUS BAR
# ══════════════════════════════════════════════════════════════════════════════

class StatusBar(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=T["bg_panel"], height=24)
        self._var = tk.StringVar(value="Ready.")
        lbl = tk.Label(self, textvariable=self._var, font=T["font_small"],
                       bg=T["bg_panel"], fg=T["fg_dim"], anchor="w", padx=T["pad"])
        lbl.pack(side="left", fill="x")

        # Version tag on right
        tk.Label(self, text="jj-dlp-gui  v0.1-shell", font=T["font_small"],
                 bg=T["bg_panel"], fg=T["fg_dim"], padx=T["pad"]).pack(side="right")

    def set(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._var.set(f"[{ts}]  {message}")


# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOM NOTEBOOK  —  dark-themed tab strip
# ══════════════════════════════════════════════════════════════════════════════

class StyledNotebook(tk.Frame):
    """
    Hand-rolled tab strip (avoids ttk styling headaches on Windows/Mac).
    Add tabs with .add_tab(tab_instance, label).
    """

    def __init__(self, parent, app):
        super().__init__(parent, bg=T["bg"])
        self.app = app
        self._tabs: list = []          # list of BaseTab instances
        self._buttons: list = []
        self._active: int = -1

        # Tab strip
        self._strip = tk.Frame(self, bg=T["bg_panel"], height=T["tab_height"])
        self._strip.pack(fill="x")
        self._strip.pack_propagate(False)

        # Content area
        self._content = tk.Frame(self, bg=T["bg"])
        self._content.pack(fill="both", expand=True)

    def add_tab(self, tab: BaseTab):
        idx = len(self._tabs)
        self._tabs.append(tab)

        btn = tk.Button(
            self._strip, text=tab.label,
            font=T["font_ui"],
            relief="flat", padx=16, pady=0,
            bg=T["bg_panel"], fg=T["fg_dim"],
            activebackground=T["bg"], activeforeground=T["fg_title"],
            cursor="hand2",
            command=lambda i=idx: self.show(i)
        )
        btn.pack(side="left", fill="y")
        self._buttons.append(btn)

        tab.place(in_=self._content, x=0, y=0, relwidth=1, relheight=1)

        if idx == 0:
            self.show(0)

    def show(self, idx: int):
        if idx == self._active:
            return

        # Deactivate old
        if 0 <= self._active < len(self._tabs):
            old = self._tabs[self._active]
            old.lower()
            old.on_deactivate()
            self._buttons[self._active].config(
                bg=T["bg_panel"], fg=T["fg_dim"],
                relief="flat"
            )

        # Activate new
        self._active = idx
        tab = self._tabs[idx]
        tab.lift()
        tab.on_activate()
        self._buttons[idx].config(
            bg=T["bg"], fg=T["fg_title"],
            relief="flat"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  MENU BAR
# ══════════════════════════════════════════════════════════════════════════════

def build_menu(app) -> tk.Menu:
    menubar = tk.Menu(app.root, bg=T["bg_panel"], fg=T["fg"],
                      activebackground=T["accent_dim"],
                      activeforeground=T["fg_title"],
                      relief="flat")

    # File
    file_menu = tk.Menu(menubar, tearoff=0, bg=T["bg_panel"], fg=T["fg"],
                        activebackground=T["accent_dim"],
                        activeforeground=T["fg_title"])
    file_menu.add_command(label="Open Config…", accelerator="Ctrl+O",
                          command=app.open_config)
    file_menu.add_separator()
    file_menu.add_command(label="Quit", accelerator="Ctrl+Q",
                          command=app.quit)
    menubar.add_cascade(label="File", menu=file_menu)

    # View
    view_menu = tk.Menu(menubar, tearoff=0, bg=T["bg_panel"], fg=T["fg"],
                        activebackground=T["accent_dim"],
                        activeforeground=T["fg_title"])
    view_menu.add_command(label="Dashboard", command=lambda: app.notebook.show(0))
    menubar.add_cascade(label="View", menu=view_menu)

    # Help
    help_menu = tk.Menu(menubar, tearoff=0, bg=T["bg_panel"], fg=T["fg"],
                        activebackground=T["accent_dim"],
                        activeforeground=T["fg_title"])
    help_menu.add_command(label="About jj-dlp-gui",
                          command=lambda: messagebox.showinfo(
                              "About", "jj-dlp-gui  v0.1-shell\n\nGUI front-end for jj-dlp.\n"))
    menubar.add_cascade(label="Help", menu=help_menu)

    return menubar


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION
# ══════════════════════════════════════════════════════════════════════════════

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("jj-dlp")
        self.root.geometry("900x600")
        self.root.minsize(700, 400)
        self.root.configure(bg=T["bg"])

        # Try to set a nice icon (silently skip if unavailable)
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        self.state = AppState()
        self._build_ui()
        self._bind_keys()

        # Auto-load config if found next to this script
        self._try_auto_load_config()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        # Status bar at bottom
        self.status_bar = StatusBar(self.root)
        self.status_bar.pack(side="bottom", fill="x")

        # Notebook
        self.notebook = StyledNotebook(self.root, self)
        self.notebook.pack(fill="both", expand=True)

        self._build_tabs()

        # Menu
        menubar = build_menu(self)
        self.root.configure(menu=menubar)

    def _build_tabs(self):
        """Register all feature tabs.  To add a new tab, add a line here."""
        self.dashboard_tab = DashboardTab(self.notebook._content, self, self.state, "Dashboard")
        self.notebook.add_tab(self.dashboard_tab)

        # ── Planned tabs (stubs) ──────────────────────────────────────────────
        terminal_tab = PlaceholderTab(
            self.notebook._content, self, self.state, "Terminal",
            description="Embedded terminal panels — one per active streamer.",
            roadmap=[
                "Live yt-dlp stdout / stderr per streamer",
                "Color-coded log output",
                "Scrollback buffer with search",
                "Per-streamer start / stop controls",
            ]
        )
        self.notebook.add_tab(terminal_tab)

        recordings_tab = PlaceholderTab(
            self.notebook._content, self, self.state, "Recordings",
            description="Browse, play and manage saved recordings.",
            roadmap=[
                "File browser scoped to OUTPUT_DIR",
                "Inline video preview",
                "Delete / rename / move files",
                "Recording stats (size, duration)",
            ]
        )
        self.notebook.add_tab(recordings_tab)

        config_tab = PlaceholderTab(
            self.notebook._content, self, self.state, "Config Editor",
            description="Edit jj-dlp.conf and other config files directly in the GUI.",
            roadmap=[
                "Syntax-highlighted INI editor",
                "Add / remove streamers with a dialog",
                "Validate config before saving",
                "Hot-reload running daemon after save",
            ]
        )
        self.notebook.add_tab(config_tab)

        tiktok_tab = PlaceholderTab(
            self.notebook._content, self, self.state, "TikTok",
            description="Integration for the TikTok post-checker script.",
            roadmap=[
                "Run tiktok-checker.py from the GUI",
                "Display new posts in a feed view",
                "Notification on new post detected",
                "Configurable check interval",
            ]
        )
        self.notebook.add_tab(tiktok_tab)

    # ── Key bindings ───────────────────────────────────────────────────────────

    def _bind_keys(self):
        self.root.bind("<Control-o>", lambda e: self.open_config())
        self.root.bind("<Control-q>", lambda e: self.quit())

    # ── Config loading ─────────────────────────────────────────────────────────

    def open_config(self):
        path = filedialog.askopenfilename(
            title="Open jj-dlp Config",
            filetypes=[("Config files", "*.conf *.ini *.cfg"), ("All files", "*.*")]
        )
        if path:
            self._load_config(path)

    def _load_config(self, path: str):
        try:
            cfg = ConfigManager.load(path)
            self.state.set_config(path, cfg)
            self.state.publish("config_loaded", cfg)
            self.status_bar.set(f"Loaded: {path}")
            self.root.title(f"jj-dlp  —  {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Config Error", str(e))
            self.status_bar.set(f"Failed to load config: {e}")

    def _try_auto_load_config(self):
        """Look for jj-dlp.conf next to this script and load it automatically."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        for candidate in ["jj-dlp.conf", "config.conf", "jj-dlp.ini"]:
            path = os.path.join(script_dir, candidate)
            if os.path.isfile(path):
                self._load_config(path)
                return

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def quit(self):
        if self.state.daemon_running and self.state.daemon_proc:
            if messagebox.askyesno("Quit", "The daemon is still running. Stop it and quit?"):
                self.state.daemon_proc.terminate()
            else:
                return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = App()
    app.run()
