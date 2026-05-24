#!/usr/bin/env python3
"""
jj-dlp  —  multi-site stream recorder with MenuWorks-style curses dashboard
"""
__version__ = "1.1.4"

import subprocess
import time
import sys
import os
import json
import threading
from datetime import datetime
from typing import List, Set, Tuple, Dict, Optional
import configparser
import argparse
import shlex
from urllib.parse import urlparse
import shutil

from .deps import ensure_curses, plain_ffmpeg_check
from .logger import (
    startup_dbg, startup_dbg_flush,
    dbg,
    log_crash,
    get_debug_log_path, get_log_path, get_log_file_paths,
    DEBUG_LOGS_ENABLED, DEBUG_LOG_PATH, debug_log_lock,
    ENABLE_CRASH_LOG,
    configure as _configure_logger,
    configure_filters as _configure_dbg_filters,
)

from .browser_config import (
    _SUPPORTED_BROWSERS,
    _read_browser_from_config,
    _write_browser_to_config,
    _write_ask_for_browser_to_config,
)

import curses  # noqa: E402


# ── Script start time (for uptime display) ───────────────────────────────────
_SCRIPT_START_TIME: float = time.time()


# ══════════════════════════════════════════════════════════════════════════════
# Config loading
# ══════════════════════════════════════════════════════════════════════════════

def load_config(config_path: str) -> dict:
    startup_dbg(f"[CONFIG] load_config called with: {config_path!r}")
    if not os.path.isfile(config_path):
        print(f"ERROR: Config file not found at: {config_path}", file=sys.stderr)
        sys.exit(1)

    parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8")
    except Exception as _e:
        startup_dbg(f"[CONFIG] load_config: configparser FAILED — {type(_e).__name__}: {_e}")
        raise

    streamers = []
    if parser.has_section("Streamers"):
        for key, _ in parser.items("Streamers"):
            if key.strip():
                streamers.append(key.strip().lower())

    blocked = []
    if parser.has_section("Block"):
        for key, _ in parser.items("Block"):
            if key.strip():
                blocked.append(key.strip().lower())

    general = parser["General"] if parser.has_section("General") else {}

    def safe_int(value, default):
        try:
            return int(value)
        except Exception:
            return default

    check_interval        = safe_int(general.get("CHECK_INTERVAL", 60), 60)
    output_dir            = general.get("OUTPUT_DIR", "recordings").strip().strip('\"\'')
    output_tmpl           = general.get("OUTPUT_TMPL", "%(title)s [%(id)s].%(ext)s").strip().strip('\"\'')
    cooldown              = safe_int(general.get("COOLDOWN_AFTER_RECORDING", 5), 5)
    split_after          = safe_int(general.get("SPLIT_AFTER", 0), 0)
    stall_check_interval  = safe_int(general.get("STALL_CHECK_INTERVAL", 30), 30)
    stall_timeout         = safe_int(general.get("STALL_TIMEOUT", 120), 120)
    config_check_interval = safe_int(general.get("CONFIG_CHECK_INTERVAL", 3), 3)
    site_tmpl             = general.get("SITE_TMPL", "").strip().strip('"\'')
    tmpl_parts = urlparse(site_tmpl).path.rstrip("/").split("/") if site_tmpl else []
    username_idx = None
    for i, p in enumerate(tmpl_parts):
        if "{username}" in p:
            username_idx = i - len(tmpl_parts)
            break
    panel_resize          = general.get("PANEL_RESIZE", "true").strip().lower() == "true"
    logging_enabled       = general.get("LOGGING", "false").strip().lower() == "true"
    log_path              = general.get("LOG_PATH", "").strip().strip('\"\'')
    split_logs            = general.get("SPLIT_LOGS", "false").strip().lower() == "true"
    popup_notifications   = general.get("POPUP_NOTIFICATIONS", "true").strip().lower() == "true"
    popup_timeout         = safe_int(general.get("POPUP_TIMEOUT", 15), 15)
    debug_logs            = general.get("DEBUG_LOGS", "false").strip().lower() == "true"
    debug_log_path_raw    = general.get("DEBUG_LOG_PATH", "").strip().strip('\"\'')
    debug_log_path        = debug_log_path_raw if debug_log_path_raw else ""
    # Select the platform-specific yt-dlp path key
    if sys.platform == "win32":
        yt_dlp_path_raw = general.get("YT_DLP_PATH_WINDOWS", "").strip().strip('"\'')
    elif sys.platform == "darwin":
        yt_dlp_path_raw = general.get("YT_DLP_PATH_MAC", "").strip().strip('"\'')
    else:
        yt_dlp_path_raw = general.get("YT_DLP_PATH_LINUX", "").strip().strip('"\'')
    startup_dbg(f"[YT_DLP] platform={sys.platform!r} → yt_dlp_path_raw={yt_dlp_path_raw!r}")
    # Auto-detect bundled yt-dlp module in the project root
    bundled_yt_dlp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "yt-dlp")
    bundled_yt_dlp_module = os.path.join(bundled_yt_dlp_dir, "yt_dlp")

    startup_dbg(f"[YT_DLP] bundled_yt_dlp_dir={bundled_yt_dlp_dir!r}")
    startup_dbg(f"[YT_DLP] bundled_yt_dlp_module={bundled_yt_dlp_module!r} exists={os.path.isdir(bundled_yt_dlp_module)}")
    startup_dbg(f"[YT_DLP] sys.executable={sys.executable!r} platform={sys.platform!r}")
    startup_dbg(f"[YT_DLP] yt_dlp_path_raw={yt_dlp_path_raw!r}")

    if os.path.isdir(bundled_yt_dlp_module):
        # Inject the bundled directory into PYTHONPATH so subprocesses can find it
        current_pp = os.environ.get("PYTHONPATH", "")
        if bundled_yt_dlp_dir not in current_pp:
            os.environ["PYTHONPATH"] = f"{bundled_yt_dlp_dir}{os.pathsep}{current_pp}" if current_pp else bundled_yt_dlp_dir

        # On Windows, sys.executable may be pythonw.exe (the windowless variant).
        # Subprocesses spawned from pythonw.exe inherit broken pipe handles and
        # produce no output at all — yt-dlp goes completely silent.
        # Always force python.exe so the child process has a proper stdio environment.
        _py_exe = sys.executable
        if sys.platform == "win32":
            _py_exe_lower = _py_exe.lower()
            if _py_exe_lower.endswith("pythonw.exe"):
                _py_exe = _py_exe[:-len("pythonw.exe")] + "python.exe"
                startup_dbg(f"[YT_DLP] pythonw.exe detected — rewriting to python.exe: {_py_exe!r}")
            else:
                startup_dbg(f"[YT_DLP] python executable OK (not pythonw): {_py_exe!r}")

        startup_dbg(f"[YT_DLP] PYTHONPATH set to: {os.environ['PYTHONPATH']!r}")
        default_yt_dlp = f"{_py_exe} -m yt_dlp"
        startup_dbg(f"[YT_DLP] bundled module found → default_yt_dlp={default_yt_dlp!r}")
    else:
        default_yt_dlp = "yt-dlp"
        startup_dbg(f"[YT_DLP] bundled module NOT found → falling back to system yt-dlp")

    # Resolve relative yt-dlp executable paths against the project root
    # (the directory containing jj_dlp/), anchored to __file__ rather than
    # CWD.  This mirrors how output_dir is handled and prevents
    # FileNotFoundError on Linux when the working directory shifts between
    # startup and subprocess launch.  Paths that already contain a space
    # (e.g. "python -m yt_dlp") or are absolute are left unchanged.
    if yt_dlp_path_raw and " " not in yt_dlp_path_raw and not os.path.isabs(yt_dlp_path_raw):
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        yt_dlp_path_raw = os.path.join(_project_root, yt_dlp_path_raw)
        startup_dbg(f"[YT_DLP] relative path resolved to absolute: {yt_dlp_path_raw!r}")
    yt_dlp_path           = yt_dlp_path_raw if yt_dlp_path_raw else default_yt_dlp
    site_label            = general.get("SITE_LABEL", os.path.basename(config_path)).strip().strip('\"\'')
    progress_bar_max_hours = safe_int(general.get("PROGRESS_BAR_MAX_HOURS", 6), 6)
    _raw_pbw = general.get("PROGRESS_BAR_WIDTH", None)
    progress_bar_width     = safe_int(general.get("PROGRESS_BAR_WIDTH", 14), 14)
    startup_dbg(
        f"[BAR_WIDTH] load_config: raw PROGRESS_BAR_WIDTH from file={_raw_pbw!r}  "
        f"-> parsed progress_bar_width={progress_bar_width}"
    )
    popup_cooldown         = safe_int(general.get("POPUP_COOLDOWN", 30), 30)
    downloader_cookies     = general.get("DOWNLOADER_COOKIES", "true").strip().lower() not in ("false", "0", "no")
    checker_cookies        = general.get("CHECKER_COOKIES", "false").strip().lower() not in ("false", "0", "no")
    ask_for_browser        = general.get("ASK_FOR_BROWSER", "true").strip().lower() not in ("false", "0", "no")
    site_order             = safe_int(general.get("SITE_ORDER", 999), 999)
    last_live_highlight    = safe_int(general.get("LAST_LIVE_HIGHLIGHT", 0), 0)
    check_for_updates      = general.get("CHECK_FOR_UPDATES", "true").strip().lower() not in ("false", "0", "no")

    # Disk drives to monitor (comma-separated paths/letters, e.g. "C:\,D:\,/home")
    disk_drives_raw = general.get("DISK_DRIVES", "").strip().strip('\"\'')
    if disk_drives_raw:
        disk_drives = [d.strip() for d in disk_drives_raw.split(",") if d.strip()]
    else:
        disk_drives = []

    if not os.path.isabs(output_dir):
        output_dir = os.path.abspath(output_dir)

    twitch_cfg = parser["Twitch"] if parser.has_section("Twitch") else {}
    twitch_client_id     = twitch_cfg.get("CLIENT_ID", "").strip().strip('"\'')
    twitch_client_secret = twitch_cfg.get("CLIENT_SECRET", "").strip().strip('"\'')
    twitch_webhook_secret= twitch_cfg.get("WEBHOOK_SECRET", "jj-dlp-secret").strip().strip('"\'')
    twitch_callback_url  = twitch_cfg.get("CALLBACK_URL", "").strip().strip('"\'')
    twitch_webhook_port  = safe_int(twitch_cfg.get("WEBHOOK_PORT", 8888), 8888)
    twitch_enabled       = bool(twitch_client_id and twitch_client_secret and twitch_callback_url)

    checker_cmd = []
    if parser.has_section("Checker"):
        for key, val in parser.items("Checker"):
            item = (val or key).strip()
            if item:
                checker_cmd.append(item)

    downloader_cmd = []
    if parser.has_section("Downloader"):
        for key, val in parser.items("Downloader"):
            item = (val or key).strip()
            if item:
                downloader_cmd.extend(shlex.split(item, posix=(sys.platform != "win32")))

    return {
        "streamers": streamers,
        "blocked": blocked,
        "check_interval": check_interval,
        "output_dir": output_dir,
        "output_tmpl": output_tmpl,
        "cooldown": cooldown,
        "split_after": split_after,
        "stall_check_interval": stall_check_interval,
        "stall_timeout": stall_timeout,
        "yt_dlp_path": yt_dlp_path,
        "checker_cmd": checker_cmd,
        "downloader_cmd": downloader_cmd,
        "config_check_interval": config_check_interval,
        "logging_enabled": logging_enabled,
        "log_path": log_path,
        "split_logs": split_logs,
        "popup_notifications": popup_notifications,
        "popup_timeout": popup_timeout,
        "debug_logs": debug_logs,
        "debug_log_path": debug_log_path,
        "panel_resize": panel_resize,
        "site_tmpl": site_tmpl,
        "username_idx": username_idx,
        "config_path": config_path,
        "site_label": site_label,
        "progress_bar_max_hours": progress_bar_max_hours,
        "progress_bar_width": progress_bar_width,
        "popup_cooldown": popup_cooldown,
        "disk_drives": disk_drives,
        "downloader_cookies": downloader_cookies,
        "checker_cookies": checker_cookies,
        "ask_for_browser": ask_for_browser,
        "site_order": site_order,
        "last_live_highlight": last_live_highlight,
        "check_for_updates": check_for_updates,
        "twitch_enabled": twitch_enabled,
        "twitch_client_id": twitch_client_id,
        "twitch_client_secret": twitch_client_secret,
        "twitch_webhook_secret": twitch_webhook_secret,
        "twitch_callback_url": twitch_callback_url,
        "twitch_webhook_port": twitch_webhook_port,
    }


# ── Global config filename (always silently loaded; never shown in chooser) ───
_GLOBAL_CONF_NAME: str = "global.conf"


def get_global_conf_path() -> str:
    """Return the absolute path to global.conf.

    Prefer configs/global.conf and fall back to global.conf in the current
    working directory for backwards compatibility.
    """
    config_dir = os.path.abspath("configs")
    global_conf_in_configs = os.path.join(config_dir, _GLOBAL_CONF_NAME)
    if os.path.exists(global_conf_in_configs):
        return global_conf_in_configs
    return os.path.abspath(_GLOBAL_CONF_NAME)


def load_global_config() -> dict:
    """Load global.conf and return the keys that are truly global.

    Returns a dict with the following keys (with safe defaults if the file does
    not exist or a key is absent):
        disk_drives       – list[str]
        debug_logs        – bool
        debug_log_path    – str
        check_for_updates – bool
        update_interval   – int
        update_branch     – str   ("main", "testing", or "experimental")
        ask_for_browser   – bool
    """
    path = get_global_conf_path()
    parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except Exception:
        pass

    general = parser["General"] if parser.has_section("General") else {}

    def _bool(key: str, default: bool) -> bool:
        raw = general.get(key, "").strip().lower()
        if raw in ("true", "1", "yes"):
            return True
        if raw in ("false", "0", "no"):
            return False
        return default

    disk_drives_raw = general.get("DISK_DRIVES", "").strip().strip('"\'')
    disk_drives = [d.strip() for d in disk_drives_raw.split(",") if d.strip()] if disk_drives_raw else []

    def _int(key: str, default: int) -> int:
        raw = general.get(key, "").strip()
        try:
            value = int(raw)
            return value if value > 0 else default
        except Exception:
            return default

    debug_log_path_raw = general.get("DEBUG_LOG_PATH", "").strip().strip('"\'')
    update_interval = _int("UPDATE_INTERVAL", 30)

    _valid_branches = {"main", "testing", "experimental"}
    _raw_branch = general.get("UPDATE_BRANCH", "main").strip().lower()
    update_branch = _raw_branch if _raw_branch in _valid_branches else "main"

    return {
        "disk_drives":        disk_drives,
        "debug_logs":         _bool("DEBUG_LOGS", False),
        "debug_log_path":     debug_log_path_raw,
        "check_for_updates":  _bool("CHECK_FOR_UPDATES", True),
        "update_interval":    update_interval,
        "update_branch":      update_branch,
        "ask_for_browser":    _bool("ASK_FOR_BROWSER", True),
        "ask_for_config":     _bool("ASK_FOR_CONFIG", True),
    }

def _write_global_conf_key(key: str, value: str) -> None:
    path = get_global_conf_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        lines = ["[General]\n"]

    section_found = False
    in_general = False
    replaced = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if s[1:-1] == "General":
                section_found = True
                in_general = True
            else:
                in_general = False
        elif in_general and "=" in s:
            k, _ = s.split("=", 1)
            if k.strip().upper() == key.upper():
                lines[i] = f"{key.upper()} = {value}\n"
                replaced = True
                break

    if not replaced:
        if not section_found:
            lines.insert(0, "[General]\n")
            lines.insert(1, f"{key.upper()} = {value}\n")
        else:
            in_general = False
            insert_idx = len(lines)
            for i, line in enumerate(lines):
                s = line.strip()
                if s.startswith("[") and s.endswith("]"):
                    if s[1:-1] == "General":
                        in_general = True
                    elif in_general:
                        insert_idx = i
                        break
            lines.insert(insert_idx, f"{key.upper()} = {value}\n")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Per-site state
# ══════════════════════════════════════════════════════════════════════════════

_GLOBAL_JSON_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "global.json") 
_global_json_lock: threading.Lock = threading.Lock()


def _load_global_json() -> dict:
    """Load the global.json file from the script's directory.  Returns an empty
    dict if the file does not exist or cannot be parsed."""
    try:
        with open(_GLOBAL_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_global_json(data: dict) -> None:
    """Write *data* to global.json atomically.  Silently ignores errors."""
    try:
        with open(_GLOBAL_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _load_last_live_cache(config_path: str) -> Dict[str, float]:
    """Return the last-live timestamps for the given site from global.json.

    The site is identified by its config filename (without path).  Each entry
    in the returned dict maps a streamer name to the Unix epoch at which their
    most recent recording ended.
    """
    site_key = os.path.basename(config_path)
    with _global_json_lock:
        global_data = _load_global_json()
    site_data = global_data.get("sites", {}).get(site_key, {})
    raw = site_data.get("last_live", {})
    if isinstance(raw, dict):
        return {k: float(v) for k, v in raw.items()}
    return {}


def _save_last_live_cache(config_path: str, last_live: Dict[str, float]) -> None:
    """Persist last-live timestamps for the given site into global.json.

    Merges with any existing data so other sites' entries are preserved.
    """
    site_key = os.path.basename(config_path)
    with _global_json_lock:
        global_data = _load_global_json()
        if "sites" not in global_data or not isinstance(global_data["sites"], dict):
            global_data["sites"] = {}
        if site_key not in global_data["sites"] or not isinstance(global_data["sites"][site_key], dict):
            global_data["sites"][site_key] = {}
        global_data["sites"][site_key]["last_live"] = {
            streamer: timestamp for streamer, timestamp in last_live.items()
        }
        _save_global_json(global_data)


class SiteState:
    """All mutable runtime state for a single monitored site/config."""

    def __init__(self, config_path: str):
        self.config_path          = config_path
        self.label                = os.path.basename(config_path)
        
        # Load the configuration once during init to retrieve things like site_order
        cfg = load_config(config_path)
        self.site_order           = cfg.get("site_order", 999)
        
        self.lock                 = threading.Lock()
        self.currently_recording: Set[str] = set()
        self.recording_threads:   List[threading.Thread] = []
        self.known_streamers:     Set[str] = set()
        self.trigger_event        = threading.Event()

        # Dashboard display state (written by monitor thread, read by renderer)
        self.dash_lock            = threading.Lock()
        self.dash_live_since:     Dict[str, float] = {}   # streamer -> epoch
        self.dash_last_live:      Dict[str, float] = _load_last_live_cache(config_path)   # streamer -> epoch when recording stopped
        self.dash_next_check_in:  float = 0.0
        self.dash_all_streamers:  List[str] = []
        self.dash_blocked:        Set[str] = set()
        self.dash_log_lines:      List[str] = []          # recent activity log
        self.dash_stdout_lines:   List[str] = []          # recent stdout lines
        self.dash_stderr_lines:   List[str] = []          # recent stderr lines

        # Twitch EventSub
        self.eventsub             = None
        self.eventsub_state       = None   # EventSubState set during main()

        # Config watcher
        self.watcher_thread:      Optional[threading.Thread] = None
        self.monitor_thread:      Optional[threading.Thread] = None

        self._stop_event          = threading.Event()

        # Stdout/Stderr tabs: whether to show checker command output (off by default — can flood with JSON)
        self.show_checker_stdout: bool = False
        self.show_checker_stderr: bool = False

        # Popup cooldown: streamer -> epoch of last popup shown
        self.popup_last_shown:    Dict[str, float] = {}

        # Active yt-dlp subprocesses: streamer -> proc
        # Written by record_stream threads; read by stop() for clean kill.
        self._procs_lock          = threading.Lock()
        self._active_procs:       Dict[str, object] = {}

        # Cached config for the dashboard renderer — refreshed at most every 2s
        # so we avoid 7+ file reads per frame in draw_system_panel.
        self._cfg_cache:          Optional[dict] = None
        self._cfg_cache_time:     float = 0.0
        self._cfg_cache_lock:     threading.Lock = threading.Lock()

    def register_proc(self, streamer: str, proc) -> None:
        """Register an active yt-dlp subprocess so stop() can kill it."""
        with self._procs_lock:
            self._active_procs[streamer] = proc

    def unregister_proc(self, streamer: str) -> None:
        """Remove a subprocess from the registry (after it exits)."""
        with self._procs_lock:
            removed = self._active_procs.pop(streamer, None)

    def kill_all_procs(self) -> None:
        """Kill every registered yt-dlp process. Called on quit."""
        with self._procs_lock:
            procs = dict(self._active_procs)
        for streamer, proc in procs.items():
            try:
                kill_proc(proc)
            except Exception:
                pass

    _CFG_CACHE_TTL: float = 2.0  # seconds between re-reads for the dashboard

    def get_cached_config(self) -> dict:
        """Return a recently-loaded config dict, re-reading the file at most every
        _CFG_CACHE_TTL seconds.  Use this in all rendering paths; use load_config()
        directly only where you need guaranteed-fresh data (monitor/watcher threads)."""
        now = time.time()
        with self._cfg_cache_lock:
            if self._cfg_cache is None or (now - self._cfg_cache_time) >= self._CFG_CACHE_TTL:
                self._cfg_cache      = load_config(self.config_path)
                self._cfg_cache_time = now
            return self._cfg_cache

    def invalidate_config_cache(self) -> None:
        """Force the next get_cached_config() call to re-read the file.
        Call this after writing changes to the config (e.g. from ConfigEditor)."""
        with self._cfg_cache_lock:
            self._cfg_cache_time = 0.0

    def log_line(self, msg: str) -> None:
        """Append a timestamped line to the site's activity log (capped at 200 lines)."""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self.dash_lock:
            self.dash_log_lines.append(line)
            if len(self.dash_log_lines) > 200:
                self.dash_log_lines = self.dash_log_lines[-200:]

    def add_stdout_line(self, line: str) -> None:
        with self.dash_lock:
            self.dash_stdout_lines.append(line)
            if len(self.dash_stdout_lines) > 200:
                self.dash_stdout_lines = self.dash_stdout_lines[-200:]

    def add_stderr_line(self, line: str) -> None:
        with self.dash_lock:
            self.dash_stderr_lines.append(line)
            if len(self.dash_stderr_lines) > 200:
                self.dash_stderr_lines = self.dash_stderr_lines[-200:]

    def stop(self) -> None:
        self._stop_event.set()
        self.trigger_event.set()
        self.kill_all_procs()


# ══════════════════════════════════════════════════════════════════════════════
# Global singletons (output mode)
# ══════════════════════════════════════════════════════════════════════════════

# Output mode: 1=curses dashboard  2=terminal
OUTPUT_MODE = 1
output_mode_lock = threading.Lock()

# Update availability flag (set during startup, read by dashboard)
UPDATE_AVAILABLE = False
update_available_lock = threading.Lock()

FFMPEG_ERROR_PATTERNS: List[str] = [
    "timestamp discontinuity",
    "Packet corrupt",
]

# Lines from the checker command are stored with these prefixes so draw_stdout_tab
# and draw_stderr_tab can filter them in/out without separate buffers.
_CHECKER_STDOUT_PREFIX: str = "\x00checker\x00"
_CHECKER_STDERR_PREFIX: str = "\x00checker_err\x00"
FFMPEG_ERROR_RESTART_THRESHOLD: int = 500

# Wire logger.dbg to this module's OUTPUT_MODE
def _get_output_mode() -> int:
    with output_mode_lock:
        return OUTPUT_MODE

_configure_logger(_get_output_mode)

# ── Per-tag debug filter ───────────────────────────────────────────────────────
# Set any tag to False to silence that group in the debug log.
# Changes here take effect immediately on the next dbg() call.
#
#   DRAIN    — yt-dlp stdout/stderr pipe drain threads
#   CHECKER  — liveness-check subprocess calls
#   SPLIT    — split-recording file-tracking logic
#   POPEN    — yt-dlp process launch details
#   PERF     — performance timing summaries (high-frequency; off by default)
#   UPDATER  — update checker and periodic updater thread
#
MAIN_DBG_FILTERS: dict[str, bool] = {
    "DRAIN":   True,
    "CHECKER": True,
    "SPLIT":   True,
    "POPEN":   True,
    "PERF":    False,   # high-frequency; disable unless actively profiling
    "UPDATER": True,
}
_configure_dbg_filters(MAIN_DBG_FILTERS)

# DEBUG_LOGS_ENABLED / DEBUG_LOG_PATH / debug_log_lock are imported from logger.

# ── Keybinds ──
KEYBIND_OUTPUT    = "o"
KEYBIND_ADD       = "a"
KEYBIND_REMOVE    = "r"
KEYBIND_DISABLE   = "d"
KEYBIND_LABELS = {
    KEYBIND_OUTPUT:    "O",
    KEYBIND_ADD:       "A",
    KEYBIND_REMOVE:    "R",
    KEYBIND_DISABLE:   "D",
}


# ══════════════════════════════════════════════════════════════════════════════
# Process helpers
# ══════════════════════════════════════════════════════════════════════════════

def kill_proc(proc) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        # PyInstaller yt-dlp binaries spawn two processes: a bootloader and the
        # real Python worker.  proc.kill() only kills the bootloader; the worker
        # becomes an orphan and keeps recording.  Kill the entire process group
        # instead so both processes are terminated together.
        import signal as _signal
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, _signal.SIGKILL)
        except (ProcessLookupError, OSError):
            # Process already gone or pgid unavailable — fall back to direct kill
            try:
                proc.kill()
            except Exception:
                pass


def build_yt_dlp_command(yt_dlp_path: str, base_cmd: List[str], extra: List[str]) -> List[str]:
    # Support "python -m yt_dlp" or other commands with arguments
    if " " in yt_dlp_path and not os.path.isfile(yt_dlp_path):
        exec_parts = shlex.split(yt_dlp_path, posix=(sys.platform != "win32"))
    else:
        exec_parts = [yt_dlp_path]
    return [*exec_parts, *base_cmd, *extra]


def cmd_display_str(cmd: List[str]) -> str:
    """Return a shell-pasteable string for the given command list.
    Uses subprocess.list2cmdline on Windows (cmd.exe quoting, backslashes intact)
    and shlex.join on POSIX systems."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


# ══════════════════════════════════════════════════════════════════════════════
# Popup notification
# ══════════════════════════════════════════════════════════════════════════════

def _show_live_popup(streamer: str, source: str = "poll", popup_timeout: int = 15) -> None:
    def _run():
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            win = tk.Toplevel(root)
            win.title("jj-dlp — Stream Live")
            win.resizable(False, False)
            win.attributes("-topmost", True)
            tk.Label(win, text=f"🔴  {streamer}  is now LIVE",
                     font=("Segoe UI", 16, "bold"), padx=20, pady=10).pack()
            tk.Label(win, text=f"via {'EventSub' if source == 'eventsub' else 'poll check'}",
                     font=("Segoe UI", 10), fg="gray", padx=20).pack()
            tk.Button(win, text="Dismiss", command=win.destroy, padx=12, pady=4).pack(pady=(4, 12))
            win.after(popup_timeout * 1000, win.destroy)
            root.mainloop()
        except ImportError:
            pass
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True, name=f"popup-{streamer}").start()


# ══════════════════════════════════════════════════════════════════════════════
# Config file editor
# ══════════════════════════════════════════════════════════════════════════════

def _modify_config_streamer(config_path: str, username: str, action: str) -> str:
    username = username.strip().lower()
    if not username:
        return "No username provided."

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"ERROR reading config: {e}"

    section_starts: dict = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section_starts[stripped[1:-1]] = i

    def _remove_from_section(sec: str, name: str) -> bool:
        if sec not in section_starts:
            return False
        removed = False
        sec_line = section_starts[sec]
        next_sec_line = len(lines)
        for other_sec, other_line in section_starts.items():
            if other_line > sec_line:
                next_sec_line = min(next_sec_line, other_line)
        to_delete = []
        for i in range(sec_line + 1, next_sec_line):
            key = lines[i].strip().split("=")[0].strip().lower()
            if key == name:
                to_delete.append(i)
                removed = True
        for i in reversed(to_delete):
            del lines[i]
            for sec_name in list(section_starts.keys()):
                if section_starts[sec_name] > i:
                    section_starts[sec_name] -= 1
        return removed

    def _add_to_section(sec: str, name: str) -> None:
        if sec not in section_starts:
            lines.append(f"\n[{sec}]\n")
            section_starts[sec] = len(lines) - 1
        sec_line = section_starts[sec]
        next_sec_line = len(lines)
        for other_sec, other_line in section_starts.items():
            if other_line > sec_line:
                next_sec_line = min(next_sec_line, other_line)
        for i in range(sec_line + 1, next_sec_line):
            key = lines[i].strip().split("=")[0].strip().lower()
            if key == name:
                return
        insert_at = next_sec_line
        while insert_at > sec_line + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines.insert(insert_at, f"{name}\n")
        for sec_name in list(section_starts.keys()):
            if section_starts[sec_name] >= insert_at:
                section_starts[sec_name] += 1

    messages = []
    if action == "add":
        removed_from_block = _remove_from_section("Block", username)
        if removed_from_block:
            messages.append(f"Unblocked '{username}'.")
        _add_to_section("Streamers", username)
        messages.append(f"Added '{username}' to [Streamers].")
    elif action == "remove":
        removed = _remove_from_section("Streamers", username)
        messages.append(f"Removed '{username}' from [Streamers]." if removed else f"'{username}' not found.")
        _add_to_section("Block", username)
    elif action == "disable":
        in_streamers = False
        if "Streamers" in section_starts:
            sec_line = section_starts["Streamers"]
            next_sec_line = len(lines)
            for other_sec, other_line in section_starts.items():
                if other_line > sec_line:
                    next_sec_line = min(next_sec_line, other_line)
            for i in range(sec_line + 1, next_sec_line):
                key = lines[i].strip().split("=")[0].strip().lower()
                if key == username:
                    in_streamers = True
                    break
        if in_streamers:
            _add_to_section("Block", username)
            messages.append(f"Disabled '{username}'.")
        else:
            messages.append(f"'{username}' not found in [Streamers].")

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        return f"ERROR writing config: {e}"

    return "  ".join(messages)


# ══════════════════════════════════════════════════════════════════════════════
# yt-dlp subprocess helpers
# ══════════════════════════════════════════════════════════════════════════════

def open_log_streams(cfg: dict):
    log_out_fp = log_err_fp = None
    if cfg.get("logging_enabled"):
        out_path, err_path = get_log_file_paths(cfg)
        try:
            log_out_fp = open(out_path, "a", encoding="utf-8")
        except Exception:
            pass
        try:
            log_err_fp = log_out_fp if err_path == out_path else open(err_path, "a", encoding="utf-8")
        except Exception:
            pass

    def _close():
        for fp in {log_out_fp, log_err_fp}:
            try:
                if fp is not None:
                    fp.close()
            except Exception:
                pass

    return subprocess.PIPE, subprocess.PIPE, _close, log_out_fp, log_err_fp


def _drain_pipe(pipe, log_fp, pipe_type: str,
                ffmpeg_error_counter=None, ffmpeg_error_event=None,
                streamer: str = "", site: Optional[SiteState] = None) -> None:
    dbg(f"[DRAIN] thread started pipe_type={pipe_type!r} streamer={streamer!r} pipe={pipe!r}")
    line_count = 0
    try:
        for raw in pipe:
            line = raw.decode(errors="replace").rstrip("\n")
            line_count += 1
            if line_count <= 3:
                dbg(f"[DRAIN] pipe_type={pipe_type!r} streamer={streamer!r} line#{line_count}: {line[:200]!r}")
            if log_fp is not None:
                try:
                    log_fp.write(line + "\n")
                    log_fp.flush()
                except Exception:
                    pass
            if site is not None:
                if pipe_type == "stdout":
                    site.add_stdout_line(line)
                elif pipe_type == "stderr":
                    site.add_stderr_line(line)
            with output_mode_lock:
                mode = OUTPUT_MODE
            if mode == 2:
                # In terminal mode, print stdout and stderr
                print(line, flush=True)
            if (ffmpeg_error_counter is not None and ffmpeg_error_event is not None
                    and FFMPEG_ERROR_RESTART_THRESHOLD > 0 and not ffmpeg_error_event.is_set()):
                line_lower = line.lower()
                for pattern in FFMPEG_ERROR_PATTERNS:
                    if pattern.lower() in line_lower:
                        ffmpeg_error_counter[0] += 1
                        if ffmpeg_error_counter[0] >= FFMPEG_ERROR_RESTART_THRESHOLD:
                            ffmpeg_error_event.set()
                        break
    except Exception as _drain_exc:
        dbg(f"[DRAIN] pipe_type={pipe_type!r} streamer={streamer!r} EXCEPTION: {_drain_exc!r}")
    dbg(f"[DRAIN] thread exiting pipe_type={pipe_type!r} streamer={streamer!r} total_lines={line_count}")


def get_live_streamers(streamers: List[str], cfg: dict,
                       site: Optional["SiteState"] = None) -> List[str]:
    if not streamers:
        return []
    # NOTE: Do NOT filter out blocked streamers here. We still need to know
    # if a blocked/disabled streamer is live so the dashboard can flash
    # [●Live] ↔ [DIS]. Recording is suppressed downstream in
    # start_recording_if_needed(), not here.
    if not streamers:
        return []
    urls = [cfg["site_tmpl"].format(username=s) for s in streamers]
    cmd = build_yt_dlp_command(cfg["yt_dlp_path"], cfg["checker_cmd"], urls)
    dbg(f"[CHECKER] yt_dlp_path={cfg['yt_dlp_path']!r}")
    dbg(f"[CHECKER] cmd={cmd!r}")
    dbg(f"[CHECKER] PYTHONPATH={os.environ.get('PYTHONPATH', '<not set>')!r}")
    _run_kwargs: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if sys.platform == "win32":
        _run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        dbg("[CHECKER] Windows: added CREATE_NO_WINDOW to subprocess.run")
    result = subprocess.run(cmd, **_run_kwargs)
    dbg(f"[CHECKER] returncode={result.returncode} stdout_len={len(result.stdout)} stderr_len={len(result.stderr)}")
    if result.stderr:
        dbg(f"[CHECKER] stderr (first 500 chars): {result.stderr[:500]!r}")
    if cfg["logging_enabled"]:
        out_path, err_path = get_log_file_paths(cfg)
        try:
            if result.stdout:
                open(out_path, "a", encoding="utf-8").write(result.stdout)
        except Exception:
            pass
    # Feed checker stdout/stderr into the site's pipe buffers (tagged so the
    # tabs can filter them based on the "Show All" toggle).
    if site is not None:
        for _chk_line in result.stdout.splitlines():
            site.add_stdout_line(_CHECKER_STDOUT_PREFIX + _chk_line)
        for _chk_line in result.stderr.splitlines():
            site.add_stderr_line(_CHECKER_STDERR_PREFIX + _chk_line)
    live = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            info = json.loads(line)
            if info.get("is_live") is True or info.get("live_status") in ("is_live", "is_upcoming"):
                url = info.get("webpage_url") or info.get("url") or ""
                ui = cfg.get("username_idx")
                try:
                    streamer = url.rstrip("/").split("/")[ui if ui is not None else -1].lstrip("@").lower().strip()
                except Exception:
                    streamer = url.rstrip("/").split("/")[-1].lstrip("@").lower().strip()
                if streamer:
                    live.append(streamer)
        except Exception:
            pass
    return live


def wait_for_streamer_file(output_dir, streamer, proc_start_time, timeout=15.0, interval=0.5):
    start = time.time()
    dbg(f"[SPLIT][wait_for_streamer_file] START streamer={streamer!r} output_dir={output_dir!r} "
        f"proc_start_time={proc_start_time:.3f} timeout={timeout}")
    while time.time() - start < timeout:
        if os.path.isdir(output_dir):
            all_files = os.listdir(output_dir)
            candidate_files = []
            for f in all_files:
                fpath = os.path.join(output_dir, f)
                if not os.path.isfile(fpath):
                    continue
                name_match = streamer.lower() in f.lower()
                mtime = os.path.getmtime(fpath)
                time_match = proc_start_time is None or mtime >= proc_start_time
                if name_match and time_match:
                    candidate_files.append(fpath)
                elif name_match and not time_match:
                    dbg(f"[SPLIT][wait_for_streamer_file] SKIPPED (too old) file={f!r} "
                        f"mtime={mtime:.3f} proc_start_time={proc_start_time:.3f} "
                        f"age_delta={proc_start_time - mtime:.3f}s")
            if candidate_files:
                chosen = max(candidate_files, key=os.path.getmtime)
                dbg(f"[SPLIT][wait_for_streamer_file] FOUND file={chosen!r} "
                    f"elapsed={time.time()-start:.2f}s candidates={len(candidate_files)}")
                return chosen
            else:
                dbg(f"[SPLIT][wait_for_streamer_file] no match yet "
                    f"elapsed={time.time()-start:.2f}s total_files={len(all_files)}")
        else:
            dbg(f"[SPLIT][wait_for_streamer_file] output_dir does not exist: {output_dir!r}")
        time.sleep(interval)
    dbg(f"[SPLIT][wait_for_streamer_file] TIMEOUT after {timeout}s — returning None for streamer={streamer!r}")
    return None


def get_streamer_file_size(output_dir, streamer, cfg=None,
                           last_growth_time=None, stall_timeout=None,
                           stall_check_interval=None, proc_start_time=None):
    try:
        filename = wait_for_streamer_file(output_dir, streamer, proc_start_time) if os.path.isdir(output_dir) else None
        size = os.path.getsize(filename) if filename else 0
        stall_detected = False
        if last_growth_time is not None and stall_timeout is not None:
            stalled = max(0.0, time.time() - last_growth_time - stall_check_interval)
            if stalled >= stall_timeout:
                stall_detected = True
        return size, stall_detected, filename or ""
    except Exception:
        return 0, False, ""

def add_segment_suffix_to_tmpl(output_tmpl: str, segment_num: int) -> str:
    """
    Convert:
        "%(title)s [%(id)s].%(ext)s"
    into:
        "%(title)s [%(id)s]_part1.%(ext)s"
    """
    root, ext = os.path.splitext(output_tmpl)
    return f"{root}_part{segment_num:01d}{ext}"


def wait_for_new_file_growth(filepath: str, timeout: float = 15.0,
                             stable_checks: int = 2,
                             interval: float = 1.0) -> bool:
    """
    Confirm a newly-started recording is actually writing data.
    Returns True once the file size grows across multiple checks.
    """
    start = time.time()
    last_size = -1
    growth_hits = 0

    dbg(f"[SPLIT][wait_for_new_file_growth] START filepath={filepath!r} "
        f"timeout={timeout} stable_checks={stable_checks} interval={interval}")

    while time.time() - start < timeout:
        try:
            if os.path.isfile(filepath):
                size = os.path.getsize(filepath)
                dbg(f"[SPLIT][wait_for_new_file_growth] poll size={size} last_size={last_size} "
                    f"growth_hits={growth_hits} elapsed={time.time()-start:.2f}s")
                if size > 0 and size > last_size:
                    growth_hits += 1
                    dbg(f"[SPLIT][wait_for_new_file_growth] growth detected "
                        f"({growth_hits}/{stable_checks}) size={size} last_size={last_size}")
                    if growth_hits >= stable_checks:
                        dbg(f"[SPLIT][wait_for_new_file_growth] CONFIRMED growth "
                            f"after {time.time()-start:.2f}s filepath={filepath!r}")
                        return True
                last_size = size
            else:
                dbg(f"[SPLIT][wait_for_new_file_growth] file not found yet: {filepath!r} "
                    f"elapsed={time.time()-start:.2f}s")
        except Exception as e:
            dbg(f"[SPLIT][wait_for_new_file_growth] exception: {e}")

        time.sleep(interval)

    dbg(f"[SPLIT][wait_for_new_file_growth] TIMEOUT after {timeout}s — "
        f"last_size={last_size} growth_hits={growth_hits} filepath={filepath!r}")
    return False




def record_stream(streamer: str, cfg: dict, site: "SiteState") -> None:
    channel_url = cfg["site_tmpl"].format(username=streamer)
    output_dir  = cfg["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    split_after_minutes = max(0, cfg.get("split_after", 0))
    split_after_seconds = split_after_minutes * 60

    dbg(f"[SPLIT][record_stream] ENTER streamer={streamer!r} "
        f"split_after_minutes={split_after_minutes} split_after_seconds={split_after_seconds} "
        f"output_dir={output_dir!r}")

    site.log_line(f"Recording started: {streamer}")

    proc = None
    close_logs = lambda: None
    segment_num = 1

    try:
        while True:
            current_output_tmpl = cfg["output_tmpl"]
            if split_after_seconds > 0:
                current_output_tmpl = add_segment_suffix_to_tmpl(
                    current_output_tmpl,
                    segment_num
                )

            output_path = os.path.join(output_dir, current_output_tmpl)

            cmd = build_yt_dlp_command(
                cfg["yt_dlp_path"],
                cfg["downloader_cmd"],
                ["-o", output_path, channel_url]
            )

            out_target, err_target, close_logs, log_out_fp, log_err_fp = open_log_streams(cfg)

            site.log_line(f"cmd: {cmd_display_str(cmd)}")

            try:
                _popen_kwargs: dict = dict(stdout=out_target, stderr=err_target)
                if sys.platform == "win32":
                    _popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                else:
                    # Put the child in its own process group so we can kill both
                    # the PyInstaller bootloader and the real yt-dlp process at once.
                    _popen_kwargs["start_new_session"] = True
                dbg(f"[POPEN] streamer={streamer!r} cmd={cmd!r}")
                dbg(f"[POPEN] Windows CREATE_NO_WINDOW={'yes' if sys.platform == 'win32' else 'n/a'}")
                dbg(f"[POPEN] PYTHONPATH={os.environ.get('PYTHONPATH', '<not set>')!r}")
                proc = subprocess.Popen(cmd, **_popen_kwargs)
                proc_start_time = time.time()
                dbg(f"[POPEN] launched pid={proc.pid}")

                site.register_proc(streamer, proc)

                ffmpeg_error_counter = [0]
                ffmpeg_error_event   = threading.Event()

                threading.Thread(
                    target=_drain_pipe,
                    args=(proc.stdout, log_out_fp, "stdout"),
                    kwargs={
                        "ffmpeg_error_counter": ffmpeg_error_counter,
                        "ffmpeg_error_event": ffmpeg_error_event,
                        "streamer": streamer,
                        "site": site
                    },
                    daemon=True
                ).start()

                threading.Thread(
                    target=_drain_pipe,
                    args=(proc.stderr, log_err_fp, "stderr"),
                    kwargs={
                        "ffmpeg_error_counter": ffmpeg_error_counter,
                        "ffmpeg_error_event": ffmpeg_error_event,
                        "streamer": streamer,
                        "site": site
                    },
                    daemon=True
                ).start()

            except Exception as e:
                site.log_line(f"Failed to start yt-dlp for {streamer}: {e}")
                try:
                    close_logs()
                except Exception:
                    pass
                break

            active_file = wait_for_streamer_file(
                output_dir,
                streamer,
                proc_start_time
            )

            last_size, _, _ = get_streamer_file_size(
                output_dir,
                streamer,
                cfg=cfg,
                proc_start_time=proc_start_time
            )

            last_growth_time     = time.time()
            recording_start_time = time.time()
            stall_check_interval = cfg["stall_check_interval"]
            stall_timeout        = cfg["stall_timeout"]
            seconds_since_check  = 0
            _split_log_counter   = 0  # throttle periodic split-timer dbg lines

            dbg(f"[SPLIT][record_stream] inner loop starting: streamer={streamer!r} "
                f"segment_num={segment_num} pid={proc.pid} "
                f"split_after_seconds={split_after_seconds} "
                f"stall_check_interval={stall_check_interval} stall_timeout={stall_timeout}")

            while proc.poll() is None:

                if site._stop_event.is_set():
                    kill_proc(proc)
                    proc.wait()
                    site.unregister_proc(streamer)
                    try:
                        close_logs()
                    except Exception:
                        pass
                    return

                _t0 = time.time()
                current_cfg = load_config(cfg["config_path"])
                _load_cfg_ms = (time.time() - _t0) * 1000
                if _split_log_counter % 30 == 0:
                    dbg(f"[PERF][record_stream/inner] load_config took {_load_cfg_ms:.2f}ms streamer={streamer!r}")

                if streamer in current_cfg["blocked"]:
                    kill_proc(proc)
                    site.log_line(f"Recording STOPPED (blocked) -> {streamer}")
                    site.unregister_proc(streamer)

                    try:
                        close_logs()
                    except Exception:
                        pass

                    with site.lock:
                        site.currently_recording.discard(streamer)

                    time.sleep(cfg["cooldown"])
                    return

                if ffmpeg_error_event.is_set():
                    site.log_line(f"ffmpeg error threshold reached for {streamer} — restarting")
                    kill_proc(proc)
                    site.unregister_proc(streamer)

                    try:
                        close_logs()
                    except Exception:
                        pass

                    time.sleep(5)
                    break

                if split_after_seconds > 0:
                    elapsed = time.time() - recording_start_time
                    _split_log_counter += 1
                    if _split_log_counter % 30 == 0:  # log roughly every 30s
                        dbg(f"[SPLIT][record_stream] split timer: streamer={streamer!r} "
                            f"segment={segment_num} elapsed={elapsed:.1f}s / "
                            f"split_after_seconds={split_after_seconds}s "
                            f"remaining={max(0, split_after_seconds - elapsed):.1f}s")

                    if elapsed >= split_after_seconds:
                        next_segment_num = segment_num + 1

                        next_output_tmpl = add_segment_suffix_to_tmpl(
                            cfg["output_tmpl"],
                            next_segment_num
                        )

                        next_output_path = os.path.join(output_dir, next_output_tmpl)

                        dbg(f"[SPLIT][record_stream] SPLIT_AFTER={split_after_seconds}s triggered for "
                            f"streamer={streamer!r} elapsed={elapsed:.1f}s "
                            f"segment_num={segment_num} -> next_segment_num={next_segment_num} "
                            f"next_output_path={next_output_path!r}")

                        site.log_line(
                            f"SPLIT_AFTER reached for {streamer} — starting part {next_segment_num}"
                        )

                        next_cmd = build_yt_dlp_command(
                            cfg["yt_dlp_path"],
                            cfg["downloader_cmd"],
                            ["-o", next_output_path, channel_url]
                        )

                        next_out_target, next_err_target, next_close_logs, next_log_out_fp, next_log_err_fp = open_log_streams(cfg)

                        site.log_line(f"yt-dlp cmd: {cmd_display_str(next_cmd)}")

                        try:
                            _next_popen_kwargs: dict = dict(
                                stdout=next_out_target,
                                stderr=next_err_target,
                            )
                            if sys.platform != "win32":
                                # Same process-group isolation as the primary Popen above.
                                _next_popen_kwargs["start_new_session"] = True
                            next_proc = subprocess.Popen(next_cmd, **_next_popen_kwargs)

                            next_proc_start_time = time.time()
                            dbg(f"[SPLIT][record_stream] next_proc started pid={next_proc.pid} "
                                f"next_proc_start_time={next_proc_start_time:.3f}")

                            threading.Thread(
                                target=_drain_pipe,
                                args=(next_proc.stdout, next_log_out_fp, "stdout"),
                                kwargs={"streamer": streamer, "site": site},
                                daemon=True
                            ).start()

                            threading.Thread(
                                target=_drain_pipe,
                                args=(next_proc.stderr, next_log_err_fp, "stderr"),
                                kwargs={"streamer": streamer, "site": site},
                                daemon=True
                            ).start()

                            # Wait for the exact new segment file.
                            # Do NOT use wait_for_streamer_file here — it does a
                            # fuzzy mtime search and can return the *previous*
                            # segment's file if the old proc is still writing to it
                            # and bumps its mtime past next_proc_start_time.
                            # Instead, search by the exact _partN suffix so we only
                            # accept the file that belongs to this new segment.
                            part_suffix = f"_part{next_segment_num}"
                            next_file = None
                            _nf_deadline = time.time() + 30.0
                            dbg(f"[SPLIT][record_stream] waiting for exact segment file "
                                f"part_suffix={part_suffix!r} pid={next_proc.pid} "
                                f"next_proc_start_time={next_proc_start_time:.3f} timeout=30s")
                            while time.time() < _nf_deadline:
                                if os.path.isdir(output_dir):
                                    for _f in os.listdir(output_dir):
                                        _fp = os.path.join(output_dir, _f)
                                        if (os.path.isfile(_fp)
                                                and streamer.lower() in _f.lower()
                                                and part_suffix.lower() in _f.lower()
                                                and os.path.getmtime(_fp) >= next_proc_start_time):
                                            next_file = _fp
                                            break
                                if next_file:
                                    dbg(f"[SPLIT][record_stream] exact segment file found: "
                                        f"{next_file!r} elapsed={30.0-(_nf_deadline-time.time()):.1f}s")
                                    break
                                dbg(f"[SPLIT][record_stream] still waiting for {part_suffix!r} file "
                                    f"remaining={_nf_deadline-time.time():.1f}s")
                                time.sleep(0.5)

                            if next_file is None:
                                dbg(f"[SPLIT][record_stream] TIMEOUT — exact segment file not found "
                                    f"part_suffix={part_suffix!r} pid={next_proc.pid}")
                            dbg(f"[SPLIT][record_stream] segment file search result: {next_file!r}")

                            split_success = (
                                next_file is not None and
                                wait_for_new_file_growth(next_file, timeout=15.0)
                            )
                            dbg(f"[SPLIT][record_stream] split_success={split_success} "
                                f"next_file={next_file!r}")

                            if split_success:
                                site.log_line(
                                    f"Split confirmed for {streamer} — switching to part {next_segment_num}"
                                )

                                dbg(f"[SPLIT][record_stream] killing old proc pid={proc.pid} "
                                    f"(was part {segment_num})")
                                kill_proc(proc)
                                try:
                                    proc.wait(timeout=15)
                                    dbg(f"[SPLIT][record_stream] old proc pid={proc.pid} exited cleanly")
                                except Exception as wait_err:
                                    dbg(f"[SPLIT][record_stream] old proc pid={proc.pid} wait() error: {wait_err}")

                                site.unregister_proc(streamer)
                                try:
                                    close_logs()
                                except Exception:
                                    pass

                                proc = next_proc
                                close_logs = next_close_logs
                                proc_start_time = next_proc_start_time
                                active_file = next_file
                                # Use next_proc_start_time (not time.time()) so the
                                # split timer accounts for time already spent verifying
                                # the new file. time.time() here would let each segment
                                # silently overrun SPLIT_AFTER by the verification delay.
                                recording_start_time = next_proc_start_time
                                segment_num = next_segment_num

                                site.register_proc(streamer, proc)

                                ffmpeg_error_counter = [0]
                                ffmpeg_error_event   = threading.Event()

                                last_size = 0
                                last_growth_time = time.time()

                                dbg(f"[SPLIT][record_stream] switched to part {segment_num} "
                                    f"pid={proc.pid} active_file={active_file!r} "
                                    f"recording_start_time reset")

                                continue

                            dbg(f"[SPLIT][record_stream] SPLIT FAILED — "
                                f"next_file={next_file!r} split_success={split_success} — "
                                f"killing next_proc pid={next_proc.pid} and continuing current segment")
                            site.log_line(
                                f"Split verification FAILED for {streamer} — keeping current recording"
                            )

                            kill_proc(next_proc)

                            try:
                                next_close_logs()
                            except Exception:
                                pass

                        except Exception as e:
                            dbg(f"[SPLIT][record_stream] EXCEPTION launching next proc: "
                                f"{type(e).__name__}: {e}")
                            site.log_line(
                                f"Failed to start split recording for {streamer}: {e}"
                            )

                time.sleep(1)
                seconds_since_check += 1

                if seconds_since_check >= stall_check_interval:
                    seconds_since_check = 0

                    current_size, stall_detected, _ = get_streamer_file_size(
                        output_dir,
                        streamer,
                        cfg=cfg,
                        proc_start_time=proc_start_time,
                        last_growth_time=last_growth_time,
                        stall_timeout=stall_timeout,
                        stall_check_interval=stall_check_interval
                    )

                    if stall_detected:
                        site.log_line(f"Stall detected for {streamer} — restarting")

                        kill_proc(proc)
                        site.unregister_proc(streamer)

                        try:
                            close_logs()
                        except Exception:
                            pass

                        time.sleep(5)
                        break

                    if current_size > last_size:
                        last_size = current_size
                        last_growth_time = time.time()

            else:
                site.unregister_proc(streamer)

                try:
                    close_logs()
                except Exception:
                    pass

                with site.dash_lock:
                    site.dash_last_live[streamer] = time.time()
                    _save_last_live_cache(site.config_path, site.dash_last_live)

                site.log_line(f"Recording finished: {streamer}")
                break

    except KeyboardInterrupt:
        if proc is not None:
            try:
                kill_proc(proc)
            except Exception:
                pass

        site.unregister_proc(streamer)

        try:
            close_logs()
        except Exception:
            pass

    finally:
        with site.lock:
            site.currently_recording.discard(streamer)

        time.sleep(cfg["cooldown"])


def start_recording_if_needed(live_now: List[str], cfg: dict, site: "SiteState",
                               show_popup: bool = True) -> None:
    with site.lock:
        to_start = [s for s in live_now
                    if s not in site.currently_recording and s not in cfg["blocked"]]
        if not to_start:
            site.recording_threads[:] = [t for t in site.recording_threads if t.is_alive()]
            return
        for streamer in to_start:
            site.currently_recording.add(streamer)
            with site.dash_lock:
                if streamer not in site.dash_live_since:
                    site.dash_live_since[streamer] = time.time()
            if show_popup and cfg.get("popup_notifications", True):
                cooldown_secs = cfg.get("popup_cooldown", 30) * 60
                last_shown    = site.popup_last_shown.get(streamer, 0)
                if time.time() - last_shown >= cooldown_secs:
                    _show_live_popup(streamer, source="poll", popup_timeout=cfg.get("popup_timeout", 15))
                    site.popup_last_shown[streamer] = time.time()
            t = threading.Thread(target=record_stream, args=(streamer, cfg, site), daemon=True)
            t.start()
            site.recording_threads.append(t)
        site.recording_threads[:] = [t for t in site.recording_threads if t.is_alive()]


def config_watcher(site: "SiteState", poll_interval: int = 3) -> None:
    prev_streamers: Set[str] = set()
    first_run = True
    while not site._stop_event.is_set():
        try:
            cfg = load_config(site.config_path)
            curr_streamers = set(cfg.get("streamers", []))
            blocked        = set(cfg.get("blocked", []))
            if first_run:
                prev_streamers = curr_streamers
                first_run      = False
            else:
                added = [s for s in (curr_streamers - prev_streamers) if s not in blocked]
                if added:
                    site.log_line(f"New streamer(s): {', '.join(added)} — immediate check")
                    with site.lock:
                        site.known_streamers.update(curr_streamers)
                    site.trigger_event.set()
                prev_streamers = curr_streamers
        except Exception:
            pass
        site._stop_event.wait(timeout=poll_interval)


def monitor_site(site: "SiteState") -> None:
    """Main polling loop for a single site — runs in its own thread."""
    try:
        from .twitch_eventsub import TwitchEventSub, EventSubState
        site.eventsub_state = EventSubState()
    except ImportError:
        site.eventsub_state = None

    initial_cfg = load_config(site.config_path)

    if site.eventsub_state is not None and initial_cfg.get("twitch_enabled"):
        def _on_stream_online(broadcaster_login: str, cfg: dict) -> None:
            with site.dash_lock:
                if broadcaster_login not in site.dash_live_since:
                    site.dash_live_since[broadcaster_login] = time.time()
            current_cfg = load_config(cfg["config_path"])
            if broadcaster_login in current_cfg.get("streamers", []) and \
               broadcaster_login not in current_cfg.get("blocked", []):
                if current_cfg.get("popup_notifications", True):
                    cooldown_secs = current_cfg.get("popup_cooldown", 30) * 60
                    last_shown    = site.popup_last_shown.get(broadcaster_login, 0)
                    if time.time() - last_shown >= cooldown_secs:
                        _show_live_popup(broadcaster_login, source="eventsub",
                                         popup_timeout=current_cfg.get("popup_timeout", 15))
                        site.popup_last_shown[broadcaster_login] = time.time()
                start_recording_if_needed([broadcaster_login], current_cfg, site, show_popup=False)

        try:
            from .twitch_eventsub import TwitchEventSub
            site.eventsub = TwitchEventSub(
                cfg=initial_cfg,
                state=site.eventsub_state,
                on_stream_online=_on_stream_online,
                load_config_fn=load_config,
                dbg_fn=dbg,
                log_fn=site.log_line,
            )
            site.eventsub.start()
        except Exception as e:
            site.log_line(f"EventSub init failed: {e}")

    while not site._stop_event.is_set():
        cfg       = load_config(site.config_path)
        streamers = cfg["streamers"]

        with site.lock:
            site.known_streamers.clear()
            site.known_streamers.update(streamers)

        with site.dash_lock:
            site.dash_next_check_in = 0.0

        if not streamers:
            site.log_line("ERROR: No streamers configured.")
        else:
            site.log_line(f"Checking {len(streamers)} streamer(s)...")
            live_now = get_live_streamers(streamers, cfg, site=site)
            cfg = load_config(site.config_path)

            with site.dash_lock:
                site.dash_all_streamers.clear()
                site.dash_all_streamers.extend(streamers)
                site.dash_blocked.clear()
                site.dash_blocked.update(cfg["blocked"])
                live_set = set(live_now)
                for s in streamers:
                    if s not in live_set:
                        site.dash_live_since.pop(s, None)
                    elif s not in site.dash_live_since:
                        site.dash_live_since[s] = time.time()

            if live_now:
                site.log_line(f"Live now: {', '.join(live_now)}")
                start_recording_if_needed(live_now, cfg, site)
            else:
                site.log_line("All streamers offline.")

        wait_secs = cfg.get("check_interval", 60)
        site.log_line(f"Next check in {wait_secs}s")
        deadline = time.time() + wait_secs

        while not site._stop_event.is_set():
            remaining = deadline - time.time()
            with site.dash_lock:
                site.dash_next_check_in = max(0.0, remaining)
            if remaining <= 0:
                with site.dash_lock:
                    site.dash_next_check_in = 0.0
                break
            fired = site.trigger_event.wait(timeout=min(1.0, remaining))
            if fired:
                site.trigger_event.clear()
                with site.dash_lock:
                    site.dash_next_check_in = 0.0
                break


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

def draw_box(stdscr, y1, x1, y2, x2, pair):
    h, w = stdscr.getmaxyx()
    def safe_ch(y, x, ch):
        if 0 <= y < h and 0 <= x < w - 1:
            try:
                stdscr.addch(y, x, ch, curses.color_pair(pair))
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


class JJDlpDashboard:
    """
    MenuWorks-style curses TUI.

    PANEL LAYOUT (easy to rearrange):
    The dashboard tab shows one panel per site. With 1 site: full width.
    With 2+ sites: 2 columns, stacked rows.

    To change panel order, just reorder the sites list passed to __init__.
    Panel grid: sites[0]=top-left, sites[1]=top-right, sites[2]=bot-left, etc.
    """

    FLASH_CYCLE = 8

    # ── Tab definitions — add/remove tabs here ──────────────────────────────
    TABS = ["Dashboard", "Log", "EventSub", "Config"]

    def __init__(self, stdscr, sites: List["SiteState"], global_cfg: dict = None):
        self.stdscr       = stdscr
        self.sites        = sites
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

        self.TABS.append("Config")  # Config tab is always last
        # --------------------------

        self.selected_tab = 0
        self.selected_site_idx = 0   # for log/config/eventsub tabs
        self.tick         = 0
        # Streamer management mode: None, or ("add"/"remove"/"disable", site_idx)
        self._mgmt_mode   = None
        self._mgmt_buf    = ""
        self._mgmt_result = ""
        # Color scheme index for randomization
        self._color_scheme_idx = 0
        # Scroll offsets for log/stdout/stderr tabs (lines from bottom; 0 = newest at bottom)
        self._log_scroll    = 0
        self._stdout_scroll = 0
        self._stderr_scroll = 0

        # Disk usage cache — refreshed at most once every 10 seconds
        self._disk_cache_time: float = 0.0
        self._disk_cache_drives: list = []
        self._disk_cache_results: list = []  # list of (drive, usage) or (drive, None) on error

        from .config_editor import ConfigEditor
        self.config_editor = ConfigEditor(self)

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

    # Color schemes: list of (chrome_fg, hilight_fg, hilight_bg, warn_fg, live_fg,
    #                          invhead_fg, invhead_bg, logo_fg, rec_fg, dim_fg,
    #                          livebadge_fg, livebadge_bg, normal_fg, disabled_fg, system_fg)
    COLOR_SCHEMES = [
        # 0: Default (cyan/blue/green/magenta)
        (curses.COLOR_CYAN,    curses.COLOR_WHITE,   curses.COLOR_BLUE,
         curses.COLOR_YELLOW,  curses.COLOR_GREEN,   curses.COLOR_BLACK,
         curses.COLOR_CYAN,    curses.COLOR_MAGENTA, curses.COLOR_RED,
         curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_GREEN,
         curses.COLOR_WHITE,   curses.COLOR_YELLOW,  curses.COLOR_YELLOW),
        # 1: Amber terminal
        (curses.COLOR_YELLOW,  curses.COLOR_WHITE,   curses.COLOR_YELLOW,
         curses.COLOR_WHITE,   curses.COLOR_GREEN,   curses.COLOR_BLACK,
         curses.COLOR_YELLOW,  curses.COLOR_YELLOW,  curses.COLOR_RED,
         curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_GREEN,
         curses.COLOR_WHITE,   curses.COLOR_WHITE,   curses.COLOR_CYAN),
        # 2: Green phosphor
        (curses.COLOR_GREEN,   curses.COLOR_WHITE,   curses.COLOR_GREEN,
         curses.COLOR_CYAN,    curses.COLOR_WHITE,   curses.COLOR_BLACK,
         curses.COLOR_GREEN,   curses.COLOR_GREEN,   curses.COLOR_RED,
         curses.COLOR_GREEN,   curses.COLOR_BLACK,   curses.COLOR_WHITE,
         curses.COLOR_WHITE,   curses.COLOR_CYAN,    curses.COLOR_YELLOW),
        # 3: Red alert
        (curses.COLOR_RED,     curses.COLOR_WHITE,   curses.COLOR_RED,
         curses.COLOR_YELLOW,  curses.COLOR_GREEN,   curses.COLOR_BLACK,
         curses.COLOR_RED,     curses.COLOR_RED,     curses.COLOR_MAGENTA,
         curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_GREEN,
         curses.COLOR_WHITE,   curses.COLOR_YELLOW,  curses.COLOR_CYAN),
        # 4: Magenta/purple
        (curses.COLOR_MAGENTA, curses.COLOR_WHITE,   curses.COLOR_MAGENTA,
         curses.COLOR_CYAN,    curses.COLOR_GREEN,   curses.COLOR_BLACK,
         curses.COLOR_MAGENTA, curses.COLOR_CYAN,    curses.COLOR_RED,
         curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_GREEN,
         curses.COLOR_WHITE,   curses.COLOR_CYAN,    curses.COLOR_YELLOW),
        # 5: Ice blue
        (curses.COLOR_CYAN,    curses.COLOR_WHITE,   curses.COLOR_CYAN,
         curses.COLOR_WHITE,   curses.COLOR_GREEN,   curses.COLOR_BLACK,
         curses.COLOR_WHITE,   curses.COLOR_BLUE,    curses.COLOR_RED,
         curses.COLOR_CYAN,    curses.COLOR_BLACK,   curses.COLOR_GREEN,
         curses.COLOR_WHITE,   curses.COLOR_YELLOW,  curses.COLOR_MAGENTA),
    ]

    def randomize_colors(self):
        """Cycle to the next color scheme."""
        self._color_scheme_idx = (self._color_scheme_idx + 1) % len(self.COLOR_SCHEMES)
        self._apply_color_scheme()

    def _apply_color_scheme(self):
        s = self.COLOR_SCHEMES[self._color_scheme_idx]
        (chrome_fg, hilight_fg, hilight_bg, warn_fg, live_fg,
         invhead_fg, invhead_bg, logo_fg, rec_fg, dim_fg,
         livebadge_fg, livebadge_bg, normal_fg, disabled_fg, system_fg) = s
        curses.init_pair(self.C_CHROME,    chrome_fg,    curses.COLOR_BLACK)
        curses.init_pair(self.C_HILIGHT,   hilight_fg,   hilight_bg)
        curses.init_pair(self.C_WARN,      warn_fg,      curses.COLOR_BLACK)
        curses.init_pair(self.C_LIVE,      live_fg,      curses.COLOR_BLACK)
        curses.init_pair(self.C_INVHEAD,   invhead_fg,   invhead_bg)
        curses.init_pair(self.C_LOGO,      logo_fg,      curses.COLOR_BLACK)
        curses.init_pair(self.C_REC,       rec_fg,       curses.COLOR_BLACK)
        curses.init_pair(self.C_DIM,       dim_fg,       curses.COLOR_BLACK)
        curses.init_pair(self.C_LIVEBADGE, livebadge_fg, livebadge_bg)
        curses.init_pair(self.C_NORMAL,    normal_fg,    curses.COLOR_BLACK)
        curses.init_pair(self.C_DISABLED,  disabled_fg,  curses.COLOR_BLACK)
        curses.init_pair(self.C_SYSTEM,    system_fg,    curses.COLOR_BLACK)

    def setup_colors(self):
        curses.start_color()
        curses.use_default_colors()
        self._apply_color_scheme()


    # ── Logo ─────────────────────────────────────────────────────────────────
    def draw_logo(self, y, x):
        for i, line in enumerate(ASCII_LOGO):
            safe_addstr(self.stdscr, y + i, x, line,
                        curses.color_pair(self.C_LOGO) | curses.A_BOLD)

    # ── Tab bar ──────────────────────────────────────────────────────────────
    def draw_tabs(self, y, x):
        for i, tab in enumerate(self.TABS):
            label = f"  {tab}  "
            if i == self.selected_tab:
                safe_addstr(self.stdscr, y, x, label,
                            curses.color_pair(self.C_HILIGHT) | curses.A_BOLD)
            else:
                safe_addstr(self.stdscr, y, x, label, curses.color_pair(self.C_INVHEAD))
            x += len(label) + 1

    # ── System status sidebar ────────────────────────────────────────────────
    def draw_system_panel(self, y1, x1, y2, x2):
        """Draws the SYSTEM info panel (from demo). Placed in the sidebar."""
        draw_box(self.stdscr, y1, x1, y2, x2, self.C_SYSTEM)
        safe_addstr(self.stdscr, y1, x1 + 2, " SYSTEM ",
                    curses.color_pair(self.C_SYSTEM) | curses.A_BOLD)

        # Aggregate counts across all sites
        total_streamers = 0
        live_cnt = 0
        rec_cnt  = 0
        off_cnt  = 0
        dis_cnt  = 0
        check_interval = 60

        for site in self.sites:
            with site.dash_lock:
                all_s      = list(site.dash_all_streamers)
                live_since = dict(site.dash_live_since)
                blocked    = set(site.dash_blocked)
                recording  = set(site.currently_recording)
            try:
                cfg = site.get_cached_config()
                check_interval = cfg.get("check_interval", 60)
            except Exception:
                pass
            total_streamers += len(all_s)
            live_cnt += sum(1 for s in all_s if s in live_since)
            rec_cnt  += sum(1 for s in recording)
            off_cnt  += sum(1 for s in all_s if s not in live_since and s not in blocked)
            dis_cnt  += sum(1 for s in all_s if s in blocked)

        # Uptime
        uptime_secs = int(time.time() - _SCRIPT_START_TIME)
        uptime_str  = _fmt_duration(uptime_secs)

        rows = [
            ("Streamers", str(total_streamers), self.C_CHROME),
            ("Live",      str(live_cnt),        self.C_LIVE),
            ("Recording", str(rec_cnt),         self.C_REC),
            ("Offline",   str(off_cnt),         self.C_DIM),
            ("Disabled",  str(dis_cnt),         self.C_DISABLED),
            ("",          "",                   0),
            ("Interval",  f"{check_interval}s", self.C_CHROME),
            ("Logging",   "", self.C_LIVE),   # filled below
            ("Popups",    "", self.C_LIVE),   # filled below
        ]

        # Fill logging/popups from first site's config
        try:
            cfg0 = self.sites[0].get_cached_config() if self.sites else {}
            rows[7] = ("Logging", "ON" if cfg0.get("logging_enabled") else "OFF",
                       self.C_LIVE if cfg0.get("logging_enabled") else self.C_DIM)
            rows[8] = ("Popups",  "ON" if cfg0.get("popup_notifications") else "OFF",
                       self.C_LIVE if cfg0.get("popup_notifications") else self.C_DIM)
        except Exception:
            pass

        # Add Update Available row if applicable
        with update_available_lock:
            if UPDATE_AVAILABLE:
                rows.append(("",               "",                 0))
                rows.append(("Update",         "Available!",       self.C_WARN))

        inner_w = x2 - x1 - 2
        label_w = min(10, inner_w // 2)

        for i, (label, val, cpair) in enumerate(rows):
            row_y = y1 + 2 + i
            if row_y >= y2 - 1:
                break
            if label:
                safe_addstr(self.stdscr, row_y, x1 + 2,
                            label[:label_w].ljust(label_w),
                            curses.color_pair(self.C_DIM))
                safe_addstr(self.stdscr, row_y, x1 + 2 + label_w + 1,
                            str(val)[:inner_w - label_w - 1],
                            curses.color_pair(cpair) | curses.A_BOLD)

        # Disk space rows — drives from global.conf take precedence; fall back to per-site
        disk_row_y = y1 + 2 + len(rows) + 1
        try:
            now = time.monotonic()
            if now - self._disk_cache_time >= 10.0:
                # Rebuild the drives list
                seen_drives: list = []
                seen_drives_set: set = set()
                fallback_dir = None

                # 1. Global drives (from global.conf) — shown first if configured
                global_drives = self.global_cfg.get("disk_drives", [])
                for d in global_drives:
                    key = os.path.normcase(d)
                    if key not in seen_drives_set:
                        seen_drives_set.add(key)
                        seen_drives.append(d)

                # 2. Per-site drives (merged in, deduped)
                for _site in self.sites:
                    try:
                        _cfg = _site.get_cached_config()
                        drives_for_site = _cfg.get("disk_drives", [])
                        if drives_for_site:
                            for d in drives_for_site:
                                key = os.path.normcase(d)
                                if key not in seen_drives_set:
                                    seen_drives_set.add(key)
                                    seen_drives.append(d)
                        elif fallback_dir is None:
                            fallback_dir = _cfg.get("output_dir", "/")
                    except Exception as _disk_site_exc:
                        dbg(f"[DISK] exception reading site config: {_disk_site_exc!r}")

                drives = seen_drives if seen_drives else ([fallback_dir] if fallback_dir else ["/"])
                dbg(f"[DISK] refreshing cache — drives={drives!r}")

                # Query disk usage for each drive and cache the results
                results = []
                for drive in drives:
                    try:
                        usage = shutil.disk_usage(drive)
                        results.append((drive, usage))
                        dbg(f"[DISK] {drive!r} → free={usage.free/(1024**3):.1f}G")
                    except Exception as _disk_exc:
                        results.append((drive, None))
                        dbg(f"[DISK] shutil.disk_usage({drive!r}) FAILED: {type(_disk_exc).__name__}: {_disk_exc}")

                self._disk_cache_drives  = drives
                self._disk_cache_results = results
                self._disk_cache_time    = now

            if disk_row_y < y2 - 1:
                safe_addstr(self.stdscr, disk_row_y, x1 + 2, "── Disk ──",
                            curses.color_pair(self.C_SYSTEM))
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
                drv_label = drv_label[:6]
                disk_str  = f"{drv_label:<6} {free_gb:>4.1f}G {pct:>3.0f}%"
                color = self.C_LIVE if pct < 80 else (self.C_WARN if pct < 95 else self.C_REC)
                safe_addstr(self.stdscr, disk_row_y, x1 + 2,
                            disk_str[:inner_w],
                            curses.color_pair(color))
                disk_row_y += 1
        except Exception as _disk_outer_exc:
            dbg(f"[DISK] outer exception in disk section: {type(_disk_outer_exc).__name__}: {_disk_outer_exc}")

        # Uptime at bottom
        safe_addstr(self.stdscr, y2 - 1, x1 + 2,
                    f"Up: {uptime_str}"[:inner_w],
                    curses.color_pair(self.C_CHROME))

    # ── Site panel (one per config) ──────────────────────────────────────────
    def draw_site_panel(self, site: "SiteState", y1, x1, y2, x2, is_selected: bool = False):
        """
        Draws one site's streamer list inside the given bounding box.
        This is the main reusable panel — rearrange by changing caller geometry.
        """
        now = time.time()
        #Pick border color based on selection
        border_pair = self.C_HILIGHT if is_selected else self.C_CHROME
        draw_box(self.stdscr, y1, x1, y2, x2, border_pair)

        # ── Panel header ──
        _panel_cfg = site.get_cached_config()
        with site.dash_lock:
            cfg_label    = _panel_cfg.get("site_label",
                                       os.path.basename(site.config_path))
            all_s        = list(site.dash_all_streamers)
            live_since   = dict(site.dash_live_since)
            last_live    = dict(site.dash_last_live)
            blocked      = set(site.dash_blocked)
            next_in      = site.dash_next_check_in
            recording    = set(site.currently_recording)

        try:
            _bar_max_secs = _panel_cfg.get("progress_bar_max_hours", 6) * 3600
            _bar_cfg_w    = max(4, _panel_cfg.get("progress_bar_width", 14))
            _last_live_highlight_days = _panel_cfg.get("last_live_highlight", 0)
        except Exception:
            _bar_max_secs = 6 * 3600
            _bar_cfg_w    = 14
            _last_live_highlight_days = 0

        # Counts for header badges
        live_cnt = sum(1 for s in all_s if s in live_since)
        rec_cnt  = sum(1 for s in recording)
        off_cnt  = sum(1 for s in all_s if s not in live_since and s not in blocked)
        dis_cnt  = sum(1 for s in all_s if s in blocked)

        header_y = y1
        # Site label on top border
        label_text = f"  {cfg_label}  "
        safe_addstr(self.stdscr, header_y, x1 + 2, label_text,
                    curses.color_pair(self.C_CHROME) | curses.A_BOLD)

        # Status badge row
        badge_y = y1 + 1
        bx = x1 + 2
        safe_addstr(self.stdscr, badge_y, bx,
                    f"LIVE:{live_cnt}",  curses.color_pair(self.C_LIVE) | curses.A_BOLD)
        bx += 7
        safe_addstr(self.stdscr, badge_y, bx,
                    f"REC:{rec_cnt}",    curses.color_pair(self.C_REC) | curses.A_BOLD)
        bx += 6
        safe_addstr(self.stdscr, badge_y, bx,
                    f"OFF:{off_cnt}",    curses.color_pair(self.C_DIM))
        bx += 6
        if dis_cnt:
            safe_addstr(self.stdscr, badge_y, bx,
                        f"DIS:{dis_cnt}", curses.color_pair(self.C_DISABLED))

        # ── Streamer rows ──
        panel_width  = x2 - x1 - 2   # usable inner width
        row_start    = y1 + 3
        max_rows     = y2 - row_start - 2   # leave 2 rows at bottom for countdown

        # Column widths — bar_w honours PROGRESS_BAR_WIDTH but won't overflow the row.
        # Row layout: [name_w] 1 [status=7] 1 [bar_w] 1 [dur=9] 1 [last_live_w]
        # So the actual space available for the bar is what's left after the fixed columns.
        name_w      = max(10, min(18, panel_width // 4))
        last_live_w = 12   # "Last Live" column
        _fixed_cols = name_w + 1 + 7 + 1 + 1 + 9 + 1 + last_live_w  # everything except bar
        bar_w       = max(4, min(_bar_cfg_w, panel_width - _fixed_cols))

        for i, s in enumerate(all_s):
            if i >= max_rows:
                break
            row_y    = row_start + i
            is_dis   = s in blocked
            since    = live_since.get(s)
            is_rec   = s in recording

            # "Last Live" value for this streamer
            ll_ts = last_live.get(s)
            if ll_ts is not None:
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
                name_attr   = curses.color_pair(self.C_DISABLED)
                bar_str     = "─" * bar_w
                bar_attr    = curses.color_pair(self.C_DISABLED)
                dur_str     = ""
                if since is not None:
                    # Disabled but currently live — flash [●Live] ↔ [x DIS]
                    if (self.tick % self.FLASH_CYCLE) < (self.FLASH_CYCLE // 2):
                        status_str  = "[●Live]"
                        status_attr = curses.color_pair(self.C_DISABLED) | curses.A_BOLD
                    else:
                        status_str  = "[x DIS]"
                        status_attr = curses.color_pair(self.C_DISABLED)
                else:
                    # Disabled and offline — steady [x DIS]
                    status_str  = "[x DIS]"
                    status_attr = curses.color_pair(self.C_DISABLED)
            elif since is not None:
                elapsed     = now - since
                name_attr   = curses.color_pair(self.C_LIVE) | curses.A_BOLD
                # Flash between "Live" and "REC" for recording streamers
                if is_rec:
                    if (self.tick % self.FLASH_CYCLE) < (self.FLASH_CYCLE // 2):
                        status_str  = "[●Live]"
                        status_attr = curses.color_pair(self.C_LIVE) | curses.A_BOLD
                    else:
                        status_str  = "[▶ REC] "
                        status_attr = curses.color_pair(self.C_REC) | curses.A_BOLD
                else:
                    status_str  = "[●Live]"
                    status_attr = curses.color_pair(self.C_LIVE) | curses.A_BOLD
                bar_str     = _live_bar(elapsed, bar_w, _bar_max_secs)
                bar_attr    = curses.color_pair(self.C_LIVE)
                dur_str     = _fmt_duration(elapsed)
                last_live_str = ""  # currently live, no "last live"
            else:
                name_attr   = curses.color_pair(self.C_DIM)
                status_str  = "[○ off]"
                status_attr = curses.color_pair(self.C_DIM)
                bar_str     = "─" * bar_w
                bar_attr    = curses.color_pair(self.C_DIM)
                dur_str     = ""

            col = x1 + 2
            safe_addstr(self.stdscr, row_y, col,
                        s[:name_w].ljust(name_w), name_attr)
            col += name_w + 1
            safe_addstr(self.stdscr, row_y, col,
                        status_str[:7].ljust(7), status_attr)
            col += 8
            safe_addstr(self.stdscr, row_y, col, bar_str, bar_attr)
            col += bar_w + 1
            if dur_str:
                safe_addstr(self.stdscr, row_y, col,
                            dur_str[:9].ljust(9), curses.color_pair(self.C_CHROME))
            else:
                safe_addstr(self.stdscr, row_y, col, " " * 9, 0)
            col += 10
            if last_live_str:
                # Highlight in C_LIVE if streamer was live within LAST_LIVE_HIGHLIGHT days
                if (ll_ts is not None
                        and _last_live_highlight_days > 0
                        and (now - ll_ts) <= _last_live_highlight_days * 86400):
                    ll_attr = curses.color_pair(self.C_LIVE) | curses.A_BOLD
                else:
                    ll_attr = curses.color_pair(self.C_DIM)
                safe_addstr(self.stdscr, row_y, col,
                            last_live_str[:last_live_w],
                            ll_attr)

        # ── Countdown ──
        nxt = max(0.0, next_in)
        safe_addstr(self.stdscr, y2 - 1, x1 + 2,
                    f"Next check: {nxt:>4.0f}s",
                    curses.color_pair(self.C_WARN) | curses.A_BOLD)

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
            
            if panel_resize and num_streamers > base_max_streamers:
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
    @staticmethod
    def _wrap_lines(lines: List[str], max_width: int) -> List[str]:
        """Wrap each line to max_width characters, preserving order."""
        if max_width <= 0:
            return lines
        wrapped = []
        for line in lines:
            if not line:
                wrapped.append("")
                continue
            while len(line) > max_width:
                wrapped.append(line[:max_width])
                line = line[max_width:]
            wrapped.append(line)
        return wrapped

    # ── Log tab ──────────────────────────────────────────────────────────────
    def draw_log_tab(self, y1, x1, y2, x2):
        # Site selector across the top
        sel_site = self.sites[self.selected_site_idx] if self.sites else None
        tab_x    = x1 + 1
        safe_addstr(self.stdscr, y1, x1, "  Site: ",
                    curses.color_pair(self.C_DIM))
        tab_x += 8
        for i, site in enumerate(self.sites):
            lbl = site.get_cached_config().get("site_label",
                              os.path.basename(site.config_path))
            label = f" {lbl} "
            attr  = (curses.color_pair(self.C_HILIGHT) | curses.A_BOLD
                     if i == self.selected_site_idx
                     else curses.color_pair(self.C_CHROME))
            safe_addstr(self.stdscr, y1, tab_x, label, attr)
            tab_x += len(label) + 1
        safe_addstr(self.stdscr, y1 + 1, x1 + 2, " ACTIVITY LOG ",
                    curses.color_pair(self.C_DIM) | curses.A_BOLD)

        if sel_site is None:
            return

        visible_rows = (y2 - y1) - 3
        line_width   = max(1, (x2 - x1) - 4)   # 2 chars padding each side

        with sel_site.dash_lock:
            raw_lines = list(sel_site.dash_log_lines)

        wrapped = self._wrap_lines(raw_lines, line_width)

        # Clamp scroll so it never exceeds available history
        max_scroll = max(0, len(wrapped) - visible_rows)
        self._log_scroll = min(self._log_scroll, max_scroll)

        # 0 = tail (newest); positive = scrolled up
        start = max(0, len(wrapped) - visible_rows - self._log_scroll)
        view  = wrapped[start : start + visible_rows]

        for i, line in enumerate(view):
            attr = curses.color_pair(self.C_DIM)
            if "Live now" in line or "Recording started" in line:
                attr = curses.color_pair(self.C_LIVE)
            elif "ERROR" in line or "Stall" in line or "STOPPED" in line:
                attr = curses.color_pair(self.C_REC)
            elif "Next check" in line:
                attr = curses.color_pair(self.C_WARN)
            safe_addstr(self.stdscr, y1 + 2 + i, x1 + 2, line, attr)

        # Scroll indicator
        if max_scroll > 0:
            scroll_info = f" ↑{self._log_scroll}/{max_scroll} " if self._log_scroll else " (end) "
            safe_addstr(self.stdscr, y1 + 1, x2 - len(scroll_info) - 1,
                        scroll_info, curses.color_pair(self.C_WARN))

    def _draw_pipe_tab(self, y1, x1, y2, x2, title: str, lines: List[str],
                       scroll: int = 0) -> int:
        """Draw a pipe-output tab. Returns the clamped scroll value."""
        sel_site = self.sites[self.selected_site_idx] if self.sites else None
        tab_x    = x1 + 1
        safe_addstr(self.stdscr, y1, x1, "  Site: ",
                    curses.color_pair(self.C_DIM))
        tab_x += 8
        for i, site in enumerate(self.sites):
            lbl = site.get_cached_config().get("site_label",
                              os.path.basename(site.config_path))
            label = f" {lbl} "
            attr  = (curses.color_pair(self.C_HILIGHT) | curses.A_BOLD
                     if i == self.selected_site_idx
                     else curses.color_pair(self.C_CHROME))
            safe_addstr(self.stdscr, y1, tab_x, label, attr)
            tab_x += len(label) + 1

        draw_box(self.stdscr, y1 + 1, x1, y2, x2, self.C_DIM)
        safe_addstr(self.stdscr, y1 + 1, x1 + 2, f" {title} ",
                    curses.color_pair(self.C_DIM) | curses.A_BOLD)

        if sel_site is None:
            return 0

        visible_rows = (y2 - y1) - 3
        line_width   = max(1, (x2 - x1) - 4)

        wrapped   = self._wrap_lines(lines, line_width)
        max_scroll = max(0, len(wrapped) - visible_rows)
        scroll    = min(scroll, max_scroll)

        start = max(0, len(wrapped) - visible_rows - scroll)
        view  = wrapped[start : start + visible_rows]

        for i, line in enumerate(view):
            safe_addstr(self.stdscr, y1 + 2 + i, x1 + 2, line,
                        curses.color_pair(self.C_DIM))

        # Scroll indicator
        if max_scroll > 0:
            scroll_info = f" ↑{scroll}/{max_scroll} " if scroll else " (end) "
            safe_addstr(self.stdscr, y1 + 1, x2 - len(scroll_info) - 1,
                        scroll_info, curses.color_pair(self.C_WARN))

        return scroll

    def draw_stdout_tab(self, y1, x1, y2, x2):
        sel_site = self.sites[self.selected_site_idx] if self.sites else None
        lines = []
        show_all = False
        if sel_site is not None:
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
        title = " STDOUT — Show All: ON  (Press A to toggle) " if show_all else " STDOUT — Show All: OFF (Press A to toggle) "
        self._stdout_scroll = self._draw_pipe_tab(
            y1, x1, y2, x2, title, lines, self._stdout_scroll)

    def draw_stderr_tab(self, y1, x1, y2, x2):
        sel_site = self.sites[self.selected_site_idx] if self.sites else None
        lines = []
        show_all = False
        if sel_site is not None:
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
        title = " STDERR — Show All: ON  (Press A to toggle) " if show_all else " STDERR — Show All: OFF (Press A to toggle) "
        self._stderr_scroll = self._draw_pipe_tab(
            y1, x1, y2, x2, title, lines, self._stderr_scroll)

    # ── EventSub tab ─────────────────────────────────────────────────────────
    def draw_eventsub_tab(self, y1, x1, y2, x2):
        draw_box(self.stdscr, y1, x1, y2, x2, self.C_CHROME)
        safe_addstr(self.stdscr, y1, x1 + 2, " TWITCH EVENTSUB ",
                    curses.color_pair(self.C_INVHEAD) | curses.A_BOLD)

        row_y = y1 + 2
        for site in self.sites:
            if row_y >= y2 - 1:
                break
            lbl = site.get_cached_config().get("site_label",
                              os.path.basename(site.config_path))
            safe_addstr(self.stdscr, row_y, x1 + 2, f"-- {lbl} --",
                        curses.color_pair(self.C_WARN) | curses.A_BOLD)
            row_y += 1

            es = site.eventsub_state
            if es is None:
                safe_addstr(self.stdscr, row_y, x1 + 4, "EventSub not available",
                            curses.color_pair(self.C_DIM))
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
                safe_addstr(self.stdscr, row_y, x1 + 4,
                            f"{label:<16}", curses.color_pair(self.C_INVHEAD))
                safe_addstr(self.stdscr, row_y, x1 + 21, val, curses.color_pair(cpair))
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
            hints = (f"  [{action.upper()} streamer on {site_lbl}]  "
                     f"Type username then Enter  |  Esc to cancel  |  "
                     f"Input: {self._mgmt_buf}_")
        else:
            current_tab = self.TABS[self.selected_tab]
            if current_tab in ("Log",):
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  UP: scroll up  DOWN: scroll down"
                         f"  C: colors  Q: quit  ")
            elif current_tab == "Stdout":
                sel_site = self.sites[self.selected_site_idx] if self.sites else None
                show_all = sel_site.show_checker_stdout if sel_site else False
                show_label = "ON " if show_all else "OFF"
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  UP: scroll up  DOWN: scroll down"
                         f"  A: Show All [{show_label}]"
                         f"  C: colors  Q: quit  ")
            elif current_tab == "Stderr":
                sel_site = self.sites[self.selected_site_idx] if self.sites else None
                show_all = sel_site.show_checker_stderr if sel_site else False
                show_label = "ON " if show_all else "OFF"
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  UP: scroll up  DOWN: scroll down"
                         f"  A: Show All [{show_label}]"
                         f"  C: colors  Q: quit  ")
            else:
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  A: add streamer R: remove streamer D: disable streamer"
                         f"  C: colors  Q: quit  ")
        safe_addstr(self.stdscr, h - 1, 0,
                    hints.ljust(w - 1)[:w - 1],
                    curses.color_pair(self.C_INVHEAD))

    # ── Streamer management overlay ───────────────────────────────────────────
    def draw_mgmt_overlay(self):
        if not self._mgmt_mode:
            return
        h, w = self.stdscr.getmaxyx()
        action, site_idx = self._mgmt_mode
        site = self.sites[site_idx]
        with site.dash_lock:
            all_s = list(site.dash_all_streamers)

        box_h, box_w = min(20, h - 4), min(60, w - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        # Fill background
        for y in range(by1, by2 + 1):
            safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1),
                        curses.color_pair(self.C_NORMAL))

        draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_WARN)
        title = f" {action.upper()} STREAMER "
        site_lbl = site.get_cached_config().get("site_label",
                               os.path.basename(site.config_path))
        safe_addstr(self.stdscr, by1, bx1 + 2, title,
                    curses.color_pair(self.C_WARN) | curses.A_BOLD)
        safe_addstr(self.stdscr, by1 + 1, bx1 + 2,
                    f"Site: {site_lbl}", curses.color_pair(self.C_DIM))

        row = by1 + 3
        if all_s:
            safe_addstr(self.stdscr, row, bx1 + 2, "Streamers:",
                        curses.color_pair(self.C_CHROME))
            row += 1
            for s in all_s:
                if row >= by2 - 4:
                    break
                safe_addstr(self.stdscr, row, bx1 + 4, f"- {s}",
                            curses.color_pair(self.C_DIM))
                row += 1

        row = by2 - 4
        if self._mgmt_result:
            safe_addstr(self.stdscr, row, bx1 + 2, self._mgmt_result[:box_w - 4],
                        curses.color_pair(self.C_LIVE) | curses.A_BOLD)
        row = by2 - 2
        safe_addstr(self.stdscr, row, bx1 + 2, "Username:",
                    curses.color_pair(self.C_WARN) | curses.A_BOLD)
        safe_addstr(self.stdscr, row, bx1 + 12,
                    (self._mgmt_buf + "_")[:box_w - 14],
                    curses.color_pair(self.C_NORMAL) | curses.A_BOLD)

    # ── Full screen refresh ───────────────────────────────────────────────────
    def refresh_screen(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        self.stdscr.bkgd(" ", curses.color_pair(self.C_NORMAL))

        # Logo (6 lines tall, starts at row 1)
        self.draw_logo(1, 2)

        # System time top-right
        sys_time_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        safe_addstr(self.stdscr, 1, w - len(sys_time_str) - 3, sys_time_str,
                    curses.color_pair(self.C_CHROME))

        # Track the next available row on the right side
        next_right_row = 2

        # Update Available indicator (below system time)
        with update_available_lock:
            if UPDATE_AVAILABLE:
                update_str = "Update Available"
                safe_addstr(self.stdscr, next_right_row, w - len(update_str) - 3, update_str,
                            curses.color_pair(self.C_WARN) | curses.A_BOLD)
                next_right_row += 1
        
        # App version indicator (Below Update Available, or directly below time)
        version_str = f"v{__version__}"
        safe_addstr(self.stdscr, next_right_row, w - len(version_str) - 3, version_str,
                    curses.color_pair(self.C_DIM))

        # Blank line after logo (row 7), then tab bar at row 8
        # (Logo occupies rows 1-6, row 7 is blank, tabs at row 8)
        self.draw_tabs(8, 2)

        # Separator
        safe_addstr(self.stdscr, 9, 1, "-" * (w - 2), curses.color_pair(self.C_CHROME))

        # Content area starts at row 10
        content_y1 = 10
        content_y2 = h - 2

        # System panel sidebar (right column, always visible)
        sidebar_w  = 28
        sidebar_x1 = w - sidebar_w - 1
        sidebar_x2 = w - 2
        _t0 = time.time()
        self.draw_system_panel(content_y1, sidebar_x1, content_y2, sidebar_x2)
        _t_system_panel = time.time() - _t0

        # Content area is to the left of the sidebar
        content_x2 = sidebar_x1 - 1

        # Get the name of the currently selected tab
        current_tab_name = self.TABS[self.selected_tab]

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
        _t_main_tab = time.time() - _t0

        self.draw_footer()

        if self._mgmt_mode:
            self.draw_mgmt_overlay()

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
        if self._mgmt_mode:
            return self._handle_mgmt_key(key)
            
        current_tab_name = self.TABS[self.selected_tab]
        if current_tab_name == "Config":
            # Pass keys to ConfigEditor first. But still handle global site switching:
            if key not in (ord(']'), curses.KEY_NPAGE, ord('['), curses.KEY_PPAGE):
                if self.config_editor.handle_key(key):
                    return True

        if key in (ord('q'), ord('Q'), 27):
            return False
        elif key in (ord('\t'), curses.KEY_RIGHT, ord('l')):
            self.selected_tab = (self.selected_tab + 1) % len(self.TABS)
        elif key in (curses.KEY_LEFT, ord('h')):
            self.selected_tab = (self.selected_tab - 1) % len(self.TABS)
        elif key in (ord(']'), curses.KEY_NPAGE):   # next site (log/config tabs)
            self.selected_site_idx = (self.selected_site_idx + 1) % max(1, len(self.sites))
            # Reset scroll when switching sites
            self._log_scroll = self._stdout_scroll = self._stderr_scroll = 0
        elif key in (ord('['), curses.KEY_PPAGE):   # prev site
            self.selected_site_idx = (self.selected_site_idx - 1) % max(1, len(self.sites))
            # Reset scroll when switching sites
            self._log_scroll = self._stdout_scroll = self._stderr_scroll = 0
        elif key in (curses.KEY_UP, ord('k')):
            tab = self.TABS[self.selected_tab]
            if tab == "Log":
                self._log_scroll += 1
            elif tab == "Stdout":
                self._stdout_scroll += 1
            elif tab == "Stderr":
                self._stderr_scroll += 1
        elif key in (curses.KEY_DOWN, ord('j')):
            tab = self.TABS[self.selected_tab]
            if tab == "Log":
                self._log_scroll = max(0, self._log_scroll - 1)
            elif tab == "Stdout":
                self._stdout_scroll = max(0, self._stdout_scroll - 1)
            elif tab == "Stderr":
                self._stderr_scroll = max(0, self._stderr_scroll - 1)
        elif key in (ord('a'), ord('A')):
            if current_tab_name == "Stdout" and self.sites:
                sel = self.sites[self.selected_site_idx]
                sel.show_checker_stdout = not sel.show_checker_stdout
                self._stdout_scroll = 0
            elif current_tab_name == "Stderr" and self.sites:
                sel = self.sites[self.selected_site_idx]
                sel.show_checker_stderr = not sel.show_checker_stderr
                self._stderr_scroll = 0
            else:
                self._start_mgmt("add")
        elif key in (ord('r'), ord('R')):
            self._start_mgmt("remove")
        elif key in (ord('d'), ord('D')):
            self._start_mgmt("disable")
        elif key in (ord('c'), ord('C')):
            self.randomize_colors()
        elif key == ord('\x0f'):  # Ctrl+O — switch to terminal mode
            with output_mode_lock:
                global OUTPUT_MODE
                OUTPUT_MODE = 2
            return False  # exit curses loop, drop to terminal
        return True

    def _start_mgmt(self, action: str):
        if not self.sites:
            return
        self._mgmt_mode   = (action, self.selected_site_idx)
        self._mgmt_buf    = ""
        self._mgmt_result = ""

    def _handle_mgmt_key(self, key) -> bool:
        if key == 27:  # Escape
            self._mgmt_mode   = None
            self._mgmt_buf    = ""
            self._mgmt_result = ""
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self._mgmt_buf = self._mgmt_buf[:-1]
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
            if self._mgmt_buf.strip():
                action, site_idx = self._mgmt_mode
                site    = self.sites[site_idx]
                result  = _modify_config_streamer(site.config_path,
                                                  self._mgmt_buf.strip(), action)
                site.trigger_event.set()
                self._mgmt_result = result
                self._mgmt_buf    = ""
            else:
                self._mgmt_mode   = None
                self._mgmt_buf    = ""
                self._mgmt_result = ""
        elif 32 <= key < 127:
            self._mgmt_buf += chr(key)
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

        with _logger.debug_log_lock:
            _logger.DEBUG_LOGS_ENABLED = new_enabled
            if new_enabled:
                if new_path:
                    _logger.DEBUG_LOG_PATH = new_path
                elif not _logger.DEBUG_LOG_PATH:
                    # Fall back to the first site's resolved path
                    _logger.DEBUG_LOG_PATH = get_debug_log_path(
                        load_config(self.sites[0].config_path)
                    )
            # If disabling, leave DEBUG_LOG_PATH as-is so re-enabling reuses it

        _logger.dbg(
            f"[CONFIG] apply_global_cfg: DEBUG_LOGS={new_enabled} "
            f"DEBUG_LOG_PATH={_logger.DEBUG_LOG_PATH!r}"
        )

    # ── Run loop ──────────────────────────────────────────────────────────────
    def run(self):
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        self.setup_colors()

        _perf_frame_count = 0
        _perf_next_report = time.time() + 10.0  # report every 10 seconds

        while True:
            _t_frame_start = time.time()
            self.refresh_screen()
            _t_after_refresh = time.time()

            key = self.stdscr.getch()
            if key != -1:
                if not self.handle_key(key):
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

def _curses_choose_config(stdscr, found: List[str]) -> List[str]:
    """
    MenuWorks-style config file chooser.
    Space = toggle [x],  Enter = confirm,  Q = quit.
    Returns list of selected config file paths (at least 1).
    """
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN,    curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_WHITE,   curses.COLOR_BLUE)
    curses.init_pair(3, curses.COLOR_YELLOW,  curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_GREEN,   curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_WHITE,   curses.COLOR_CYAN)
    curses.init_pair(6, curses.COLOR_MAGENTA, curses.COLOR_BLACK)

    curses.curs_set(0)
    stdscr.keypad(True)

    # ── Phase 1: config file selection ───────────────────────────────────────
    selected  = set(range(len(found)))   # start with all config files selected
    cursor    = 0
    n         = len(found)
    do_not_show_config = False

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.bkgd(" ", curses.color_pair(0))

        # Logo
        for i, line in enumerate(ASCII_LOGO):
            safe_addstr(stdscr, 1 + i, 2, line, curses.color_pair(6) | curses.A_BOLD)

        ts = time.strftime("%Y-%m-%d  %H:%M:%S")
        safe_addstr(stdscr, 1, w - len(ts) - 3, ts, curses.color_pair(1))
        safe_addstr(stdscr, 7, 2, "-" * (w - 4), curses.color_pair(1))

        # Title
        title = "SELECT CONFIG FILE(S)"
        safe_addstr(stdscr, 9, 2, title, curses.color_pair(5) | curses.A_BOLD)

        # Instructions
        safe_addstr(stdscr, 10, 2,
                    "Space = toggle [x]   Enter = confirm   Q = quit",
                    curses.color_pair(3))

        # File list
        for i, name in enumerate(found):
            row     = 12 + i
            checked = "[x]" if i in selected else "[ ]"
            is_cur  = i == cursor
            if is_cur:
                attr = curses.color_pair(2) | curses.A_BOLD
            elif i in selected:
                attr = curses.color_pair(4) | curses.A_BOLD
            else:
                attr = curses.color_pair(1)
            safe_addstr(stdscr, row, 4, f"  {checked}  {name}", attr)

        # "Do not show again" checkbox
        dna_row = 12 + n + 1
        dna_box = "[x]" if do_not_show_config else "[ ]"
        dna_attr = curses.color_pair(3) | curses.A_BOLD if do_not_show_config else curses.color_pair(3)
        safe_addstr(stdscr, dna_row, 4,
                    f"  {dna_box}  Do not show again (press D to toggle)",
                    dna_attr)

        # Footer
        sel_count = len(selected)
        footer = (f"  {sel_count} file(s) selected  "
                  f"↑/↓ navigate  Space toggle  Enter confirm  D do not show  ")
        safe_addstr(stdscr, h - 1, 0,
                    footer.ljust(w - 1)[:w - 1],
                    curses.color_pair(5) | curses.A_BOLD)

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
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
            if selected:
                chosen_files = [found[i] for i in sorted(selected)]
                
                # Save chosen config files to global.json
                global_data = _load_global_json()
                global_data["startup_configs"] = chosen_files
                _save_global_json(global_data)
                
                if do_not_show_config:
                    _write_global_conf_key("ASK_FOR_CONFIG", "false")
                
                return chosen_files
        elif key in (ord('q'), ord('Q'), 27):
            sys.exit(0)

def _curses_choose_browser(stdscr, chosen_files: List[str]) -> List[str]:
    """
    Browser-cookie picker.
    ↑/↓ navigate, Enter = confirm, Q = quit.
    Writes the chosen browser back into each selected config file.
    Returns chosen_files unmodified.
    """
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN,    curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_WHITE,   curses.COLOR_BLUE)
    curses.init_pair(3, curses.COLOR_YELLOW,  curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_GREEN,   curses.COLOR_BLACK)
    curses.init_pair(5, curses.COLOR_WHITE,   curses.COLOR_CYAN)
    curses.init_pair(6, curses.COLOR_MAGENTA, curses.COLOR_BLACK)

    curses.curs_set(0)
    stdscr.keypad(True)

    # Build a per-file config map for all selected files (used for flag checks).
    file_cfgs = {
        fname: load_config(os.path.join(os.getcwd(), fname))
        for fname in chosen_files
    }

    first_fpath = os.path.join(os.getcwd(), chosen_files[0])

    # Read the current browser from the first selected config file so we can
    # pre-select it.  All selected configs will be updated with the same choice.
    browsers     = _SUPPORTED_BROWSERS          # e.g. ['brave', 'chrome', ..., 'other']
    nb           = len(browsers)
    current_br   = _read_browser_from_config(first_fpath)
    try:
        br_cursor = browsers.index(current_br)
    except ValueError:
        br_cursor = browsers.index("firefox")

    # "Do not show again" toggle state (starts unchecked)
    do_not_show = False

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.bkgd(" ", curses.color_pair(0))

        # Logo
        for i, line in enumerate(ASCII_LOGO):
            safe_addstr(stdscr, 1 + i, 2, line, curses.color_pair(6) | curses.A_BOLD)

        ts = time.strftime("%Y-%m-%d  %H:%M:%S")
        safe_addstr(stdscr, 1, w - len(ts) - 3, ts, curses.color_pair(1))
        safe_addstr(stdscr, 7, 2, "-" * (w - 4), curses.color_pair(1))

        # Browser sub-title
        br_title_row = 9
        safe_addstr(stdscr, br_title_row, 2,
                    "SELECT BROWSER",
                    curses.color_pair(5) | curses.A_BOLD)
        safe_addstr(stdscr, br_title_row + 1, 2,
                    "Select your browser for the yt-dlp --cookies-from-browser option.",
                    curses.color_pair(3))
        safe_addstr(stdscr, br_title_row + 2, 2,
                    "Note: Chrome based browsers are not supported. Firefox is recommended.",
                    curses.color_pair(3))
        applies_to_labels = [
            file_cfgs[fname].get("site_label")
            for fname in chosen_files
            if file_cfgs[fname].get("downloader_cookies", True) or file_cfgs[fname].get("checker_cookies", False)
        ]
        safe_addstr(stdscr, br_title_row + 4, 2,
                    f"Applies to: {', '.join(applies_to_labels)}",
                    curses.color_pair(4))

        # Browser list (single-select radio buttons)
        list_start_row = br_title_row + 6
        for i, br in enumerate(browsers):
            row    = list_start_row + i
            dot    = "(*)" if i == br_cursor else "( )"
            is_cur = i == br_cursor
            if is_cur:
                attr = curses.color_pair(2) | curses.A_BOLD
            else:
                attr = curses.color_pair(1)
            label = f"  {dot}  {br}" + ("  ← remove cookies option" if br == "disabled" else "")
            safe_addstr(stdscr, row, 4, label, attr)

        # "Do not show again" checkbox (below the browser list)
        dna_row  = list_start_row + nb + 1
        dna_box  = "[x]" if do_not_show else "[ ]"
        dna_attr = curses.color_pair(3) | curses.A_BOLD if do_not_show else curses.color_pair(3)
        safe_addstr(stdscr, dna_row, 4,
                    f"  {dna_box}  Do not show again (press D to toggle)",
                    dna_attr)

        # Footer
        footer = "  ↑/↓ navigate  Enter = confirm  D = do not show again  Q = quit  "
        safe_addstr(stdscr, h - 1, 0,
                    footer.ljust(w - 1)[:w - 1],
                    curses.color_pair(5) | curses.A_BOLD)

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_UP, ord('k')):
            br_cursor = (br_cursor - 1) % nb
        elif key in (curses.KEY_DOWN, ord('j')):
            br_cursor = (br_cursor + 1) % nb
        elif key in (ord('d'), ord('D')):
            do_not_show = not do_not_show
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
            chosen_browser = browsers[br_cursor]
            for fname in chosen_files:
                fpath = os.path.join(os.getcwd(), fname)
                # DOWNLOADER_COOKIES and CHECKER_COOKIES are per-file.
                write_dl = file_cfgs[fname].get("downloader_cookies", True)
                write_ck = file_cfgs[fname].get("checker_cookies", False)
                if write_dl or write_ck:
                    _write_browser_to_config(fpath, chosen_browser, write_downloader=write_dl, write_checker=write_ck)
                # If "Do not show again" was checked, persist ASK_FOR_BROWSER = False
                if do_not_show:
                    _write_global_conf_key("ASK_FOR_BROWSER", "false")
            return chosen_files
        elif key in (ord('q'), ord('Q'), 27):
            sys.exit(0)

    return chosen_files  # unreachable, satisfies type checker


def _input_with_timeout(prompt: str, timeout_seconds: int = 10) -> Optional[str]:
    """Prompt the user for a single keypress (y/n) with a timeout.

    Returns the character immediately when pressed — no Enter required.
    Returns None if the timeout expires without a response.
    """
    print(prompt, end="", flush=True)

    if sys.platform == "win32" and sys.stdin.isatty():
        import msvcrt

        end_time = time.time() + timeout_seconds
        while time.time() < end_time:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch == "\x03":
                    raise KeyboardInterrupt
                print(ch)  # echo the character
                return ch.lower()
            time.sleep(0.01)
        print()
        return None

    # Unix / macOS: use termios to switch to raw (no-echo, no-buffering) mode.
    if sys.stdin.isatty():
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            rlist, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
            if rlist:
                ch = sys.stdin.read(1)
                print(ch)  # echo the character
                if ch == "\x03":
                    raise KeyboardInterrupt
                return ch.lower()
            else:
                print()
                return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # Fallback for non-tty environments (pipes, CI, etc.) — still line-buffered.
    result = []

    def _read_input():
        try:
            user_input = input()
            result.append(user_input)
        except (EOFError, KeyboardInterrupt):
            result.append(None)

    thread = threading.Thread(target=_read_input, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        print()
        return None

    return result[0].strip()[:1].lower() if result and result[0] is not None else None


# ══════════════════════════════════════════════════════════════════════════════
# main()
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # ── Pre-flight dependency checks ──────────────────────────────────────────
    # Must run before any yt-dlp activity so the user sees a clear error
    # rather than a confusing partially-functional dashboard.
    # Kept inside main() (not at module scope) so that importing from this
    # module never triggers interactive prompts or sys.exit().
    ensure_curses()
    if not plain_ffmpeg_check():
        print(f"\njj-dlp v{__version__}  ·  Aborted during ffmpeg check.")
        sys.exit(1)

    _script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.getcwd() != _script_dir:
        os.chdir(_script_dir)
        startup_dbg(f"CWD changed to: {_script_dir}")

    startup_dbg_flush()

    parser = argparse.ArgumentParser(description="jj-dlp multi-site stream recorder")
    parser.add_argument("--config", nargs="+", default=None,
                        help="Path(s) to config file(s). Omit to auto-discover.")
    parser.add_argument("--update", action="store_true", help="Update jj-dlp to the latest version")
    args = parser.parse_args()

    if args.update:
        from .updater import perform_update
        perform_update()
        sys.exit(0)

    # ── Config discovery / selection ──────────────────────────────────────────
    if args.config is not None:
        config_paths = []
        for p in args.config:
            ap = os.path.abspath(p)
            if os.path.basename(ap) == _GLOBAL_CONF_NAME:
                # global.conf is always loaded separately via load_global_config();
                # passing it via --config would create a spurious site panel.
                startup_dbg(f"[CONFIG] Ignoring {ap!r} from --config — global.conf is loaded automatically.")
                print(f"Note: {_GLOBAL_CONF_NAME} is loaded automatically and does not need to be passed via --config. Skipping.")
                continue
            if not os.path.isfile(ap):
                print(f"ERROR: Config file not found: {ap}", file=sys.stderr)
                sys.exit(1)
            config_paths.append(ap)
    else:
        default_path = os.path.abspath("jj-dlp.conf")
        if os.path.isfile(default_path):
            config_paths = [default_path]
        else:
            cwd   = os.getcwd()
            search_dirs = []
            if os.path.isdir(os.path.join(cwd, "configs")):
                search_dirs.append(os.path.join(cwd, "configs"))
            search_dirs.append(cwd)

            found = []
            for d in search_dirs:
                for f in os.listdir(d):
                    if f.endswith(".conf") and os.path.isfile(os.path.join(d, f)):
                        # global.conf is always loaded silently; never shown in the chooser
                        if f == _GLOBAL_CONF_NAME:
                            continue
                        rel = os.path.relpath(os.path.join(d, f), cwd)
                        if rel not in found:
                            found.append(rel)
            found.sort()

            if not found:
                print(f"ERROR: No .conf files found in {cwd} or configs/. "
                      "Pass --config <path> or place a jj-dlp.conf here.",
                      file=sys.stderr)
                sys.exit(1)
            if len(found) == 1:
                print(f"Using: {found[0]}")
                chosen = [found[0]]
            else:
                # Load global.conf to check if we should show the UI
                global_cfg = load_global_config()
                ask_for_config = global_cfg.get("ask_for_config", True)
                
                # Load global.json to see if we have saved configs
                global_data = _load_global_json()
                saved_configs = global_data.get("startup_configs", [])
                
                if not ask_for_config and saved_configs and all(c in found for c in saved_configs):
                    chosen = saved_configs
                else:
                    # Multi-select chooser
                    chosen = curses.wrapper(_curses_choose_config, found)
                    
            # ASK_FOR_BROWSER logic
            _global_cfg = load_global_config()
            ask_for_browser = _global_cfg.get("ask_for_browser", None)
            if ask_for_browser is None:
                # Fall back to per-site values for backwards compatibility
                ask_for_browser = any(
                    load_config(os.path.join(cwd, f)).get("ask_for_browser", True) 
                    for f in chosen
                )
            
            if ask_for_browser:
                chosen = curses.wrapper(_curses_choose_browser, chosen)

            config_paths = [os.path.join(cwd, f) for f in chosen]

    # ── Global config / debug setup ───────────────────────────────────────────
    initial_cfg = load_config(config_paths[0])

    # Load global.conf — app-wide settings, independent of any site config.
    global_cfg = load_global_config()
    startup_dbg(f"[GLOBAL] loaded global.conf: {global_cfg!r}")

    # ── Updater logic ─────────────────────────────────────────────────────────
    from .updater import check_for_updates_background, is_update_available, perform_update
    # CHECK_FOR_UPDATES is now a global setting.
    any_check = global_cfg.get("check_for_updates", True)
    update_interval = global_cfg.get("update_interval", 30)
    if update_interval <= 0:
        update_interval = 30
    
    global UPDATE_AVAILABLE
    if any_check:
        dbg(f"[UPDATER] enabled startup checker update_interval={update_interval}")
        startup_available = is_update_available()
        dbg(f"[UPDATER] startup read update_available={startup_available}")
        if startup_available:
            with update_available_lock:
                UPDATE_AVAILABLE = True
            print("\n[Updater] A new version of jj-dlp is available!")
            ans = _input_with_timeout("[Updater] Do you want to update now? (y/n) [timeout in 10s]: ", timeout_seconds=10)
            if ans == 'y':
                perform_update()
                sys.exit(0)
            elif ans is None:
                print("[Updater] No response received. Continuing with current version.")

        def _periodic_update_checker() -> None:
            global UPDATE_AVAILABLE
            while True:
                check_for_updates_background()
                new_available = is_update_available()
                with update_available_lock:
                    prev_available = UPDATE_AVAILABLE
                    UPDATE_AVAILABLE = new_available
                dbg(f"[UPDATER] periodic check prev={prev_available} new={new_available}")
                # When an update becomes available while the dashboard is active,
                # only use the dashboard indicator and do not prompt interactively.
                time.sleep(update_interval * 60)

        threading.Thread(target=_periodic_update_checker, daemon=True).start()

    from . import logger as _logger
    with _logger.debug_log_lock:
        # DEBUG_LOGS / DEBUG_LOG_PATH are now global settings.
        any_debug = global_cfg.get("debug_logs", False)
        _logger.DEBUG_LOGS_ENABLED = any_debug
        if any_debug:
            raw_path = global_cfg.get("debug_log_path", "")
            if raw_path:
                _logger.DEBUG_LOG_PATH = raw_path
            else:
                _logger.DEBUG_LOG_PATH = get_debug_log_path(load_config(config_paths[0]))
        
    # ── Launch per-site state + threads ──────────────────────────────────────
    sites: List[SiteState] = []
    for cp in config_paths:
        site = SiteState(cp)
        sites.append(site)

    # Sort sites by site_order so they appear in the desired positions in the dashboard
    sites.sort(key=lambda s: s.site_order)

    for site in sites:
        # Monitor thread (liveness check loop)
        mt = threading.Thread(target=monitor_site, args=(site,), daemon=True)
        mt.start()
        site.monitor_thread = mt

        # Config watcher thread
        cfg_i = load_config(site.config_path)
        wt = threading.Thread(target=config_watcher,
                              args=(site, cfg_i.get("config_check_interval", 3)),
                              daemon=True)
        wt.start()
        site.watcher_thread = wt

    # ── Launch curses dashboard ───────────────────────────────────────────────
    try:
        def _run_dashboard(stdscr):
            h, w = stdscr.getmaxyx()
            min_h, min_w = 30, 90
            if h < min_h or w < min_w:
                stdscr.clear()
                stdscr.addstr(0, 0,
                    f"Terminal too small — need at least {min_w}×{min_h} "
                    f"(currently {w}×{h}). Resize and re-run.")
                stdscr.refresh()
                stdscr.getch()
                return
            JJDlpDashboard(stdscr, sites, global_cfg=global_cfg).run()

        while True:
            with output_mode_lock:
                mode = OUTPUT_MODE
            if mode == 1:
                curses.wrapper(_run_dashboard)
            # After curses exits check mode again
            with output_mode_lock:
                mode = OUTPUT_MODE
            if mode != 1:
                # Terminal mode — just wait for Ctrl+C
                print("\n[jj-dlp terminal mode] Press Ctrl+O to return to dashboard, Ctrl+C to quit.\n",
                      flush=True)
                try:
                    while True:
                        with output_mode_lock:
                            if OUTPUT_MODE == 1:
                                break
                        time.sleep(0.5)
                except KeyboardInterrupt:
                    break
            else:
                break

    except KeyboardInterrupt:
        pass
    finally:
        for site in sites:
            site.stop()
            if site.eventsub is not None:
                try:
                    site.eventsub.stop(timeout=5)
                except Exception:
                    pass

        print(f"\njj-dlp v{__version__}  ·  Shutting down...")
        active = [t for site in sites for t in site.recording_threads if t.is_alive()]
        if active:
            print(f"Waiting for {len(active)} active recording(s) to finish...")
            for t in active:
                t.join(timeout=15)
        print("✓  All done. Goodbye!\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as _top_e:
        log_crash(_top_e)
        raise
