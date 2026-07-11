#!/usr/bin/env python3
"""
jj-dlp  —  multi-site stream recorder with MenuWorks-style curses dashboard
"""
__version__ = "1.21.10"

import subprocess
import time
import sys
import os
import json
import re as _re
import threading
from datetime import datetime, timedelta
from datetime import time as _dt_time
from typing import List, Set, Tuple, Dict, Optional
import configparser
import argparse
import shlex
from urllib.parse import urlparse
import shutil

from .deps import ensure_curses, plain_ffmpeg_check
from . import logger as _logger
from .logger import (
    startup_dbg, startup_dbg_flush,
    dbg,
    log_crash,
    get_debug_log_path, get_log_path, get_log_file_paths,
    ENABLE_CRASH_LOG,
    configure_debug_log as _configure_debug_log,
)

from .browser_config import (
    _SUPPORTED_BROWSERS,
    _read_browser_from_config,
    _write_browser_to_config,
    _write_ask_for_browser_to_config,
)
from .config_editor import CONFIG_KEYS, _KEY_DEFAULTS, _compute_config_id, SiteSortManager, SORT_OPTIONS, _SORT_LABELS

import curses  # noqa: E402


# ── Script start time (for uptime display)) ──────────────────────────────────
_SCRIPT_START_TIME: float = time.time()

# ── Global structures for concurrency control ────────────────────────────────
_global_sites: List["SiteState"] = []
_recording_start_lock = threading.Lock()


def _get_config_id() -> str:
    """Return a stable short ID for the current set of loaded config file paths.

    Delegates to config_editor._compute_config_id so there is a single
    implementation of this hashing logic.

    Lock ordering note: callers that also acquire _recording_start_lock or
    site.lock must do so *after* _get_config_id() returns; this function
    reads _global_sites without a lock, which is safe because _global_sites
    is written once at startup and is effectively read-only thereafter.
    """
    return _compute_config_id([site.config_path for site in _global_sites])


# ══════════════════════════════════════════════════════════════════════════════
# Config loading
# ══════════════════════════════════════════════════════════════════════════════

def _safe_int(value, default):
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value)
    except Exception:
        return default


def _parse_general_section(general, config_path: str) -> dict:
    """Read all site-scoped CONFIG_KEYS from the [General] section.

    Returns a flat dict keyed by the lower-cased config key name.
    Boolean and integer defaults are coerced automatically; string values are
    stripped of surrounding whitespace and quotes.  ``output_dir`` is resolved
    to an absolute path, and ``site_label`` defaults to the config filename.
    """
    cfg_dict: dict = {}
    for kdef in CONFIG_KEYS:
        if kdef.scope != "site":
            continue
        raw = general.get(kdef.name, kdef.default)
        if raw is None:
            raw = kdef.default

        val_str = str(raw).strip().strip('"\'')

        if kdef.default.lower() in ("true", "false"):
            val = val_str.lower() not in ("false", "0", "no")
        elif kdef.default.isdigit():
            val = _safe_int(val_str, _safe_int(kdef.default, 0))
        else:
            val = val_str

        cfg_dict[kdef.name.lower()] = val

    if not os.path.isabs(cfg_dict["output_dir"]):
        cfg_dict["output_dir"] = os.path.abspath(cfg_dict["output_dir"])

    # SITE_LABEL defaults to the config filename rather than a fixed string.
    site_label = general.get("SITE_LABEL", os.path.basename(config_path))
    if site_label is None:
        site_label = os.path.basename(config_path)
    cfg_dict["site_label"] = str(site_label).strip().strip('"\'')

    startup_dbg(
        f"[BAR_WIDTH] _parse_general_section: "
        f"progress_bar_width={cfg_dict.get('progress_bar_width')}"
    )
    return cfg_dict


def _parse_streamers_and_blocked(parser: configparser.ConfigParser) -> tuple:
    """Return (streamers, blocked) lists from [Streamers] and [Block] sections."""
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

    return streamers, blocked


def _parse_twitch_section(parser: configparser.ConfigParser) -> dict:
    """Extract all Twitch-related settings from the [Twitch] section."""
    twitch_cfg = parser["Twitch"] if parser.has_section("Twitch") else {}
    client_id     = twitch_cfg.get("CLIENT_ID", "").strip().strip('"\'')
    client_secret = twitch_cfg.get("CLIENT_SECRET", "").strip().strip('"\'')
    webhook_secret = twitch_cfg.get("WEBHOOK_SECRET", "jj-dlp-secret").strip().strip('"\'')
    callback_url  = twitch_cfg.get("CALLBACK_URL", "").strip().strip('"\'')
    webhook_port  = _safe_int(twitch_cfg.get("WEBHOOK_PORT", 8888), 8888)
    enabled       = bool(client_id and client_secret and callback_url)

    return {
        "twitch_enabled": enabled,
        "twitch_client_id": client_id,
        "twitch_client_secret": client_secret,
        "twitch_webhook_secret": webhook_secret,
        "twitch_callback_url": callback_url,
        "twitch_webhook_port": webhook_port,
    }


def _parse_checker_and_downloader(parser: configparser.ConfigParser) -> tuple:
    """Return (checker_cmd, downloader_cmd, lq_downloader_cmd) lists from
    [Checker], [Downloader], and [LQ_Downloader]."""
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

    lq_downloader_cmd = []
    if parser.has_section("LQ_Downloader"):
        for key, val in parser.items("LQ_Downloader"):
            item = (val or key).strip()
            if item:
                lq_downloader_cmd.extend(shlex.split(item, posix=(sys.platform != "win32")))

    return checker_cmd, downloader_cmd, lq_downloader_cmd


def _derive_username_idx(cfg_dict: dict) -> Optional[int]:
    """Return the negative URL-path index where ``{username}`` appears in SITE_TMPL.

    Returns ``None`` when SITE_TMPL is absent or contains no ``{username}``
    placeholder.
    """
    site_tmpl = cfg_dict.get("site_tmpl", "")
    if not site_tmpl:
        return None
    tmpl_parts = urlparse(site_tmpl).path.rstrip("/").split("/")
    for i, part in enumerate(tmpl_parts):
        if "{username}" in part:
            return i - len(tmpl_parts)
    return None


def _resolve_yt_dlp_path(cfg_dict: dict) -> str:
    """Determine the yt-dlp invocation string for the current platform.

    Resolution order:
    1. The platform-specific config key (YT_DLP_PATH_WINDOWS / _MAC / _LINUX).
       Relative paths are anchored to the project root (the directory that
       contains the jj_dlp/ package), not to CWD.
    2. A bundled ``yt-dlp/yt_dlp`` module next to the project root.
       When found, PYTHONPATH is updated so subprocesses can import it, and
       ``python -m yt_dlp`` is used as the command.  On Windows, ``pythonw.exe``
       is silently rewritten to ``python.exe`` so child processes have working
       stdio handles.
    3. The system ``yt-dlp`` binary as a last resort.
    """
    # 1. Pick the platform-specific raw path from config.
    platform_key_map = {
        "win32":  "yt_dlp_path_windows",
        "darwin": "yt_dlp_path_mac",
    }
    platform_key = platform_key_map.get(sys.platform, "yt_dlp_path_linux")
    yt_dlp_path_raw = cfg_dict.get(platform_key, "")
    startup_dbg(f"[YT_DLP] platform={sys.platform!r} → yt_dlp_path_raw={yt_dlp_path_raw!r}")

    # 2. Detect a bundled yt-dlp module sitting next to the project root.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bundled_yt_dlp_dir    = os.path.join(project_root, "yt-dlp")
    bundled_yt_dlp_module = os.path.join(bundled_yt_dlp_dir, "yt_dlp")

    startup_dbg(f"[YT_DLP] bundled_yt_dlp_dir={bundled_yt_dlp_dir!r}")
    startup_dbg(f"[YT_DLP] bundled_yt_dlp_module={bundled_yt_dlp_module!r} "
                f"exists={os.path.isdir(bundled_yt_dlp_module)}")
    startup_dbg(f"[YT_DLP] sys.executable={sys.executable!r} platform={sys.platform!r}")

    if os.path.isdir(bundled_yt_dlp_module):
        _inject_bundled_pythonpath(bundled_yt_dlp_dir)
        py_exe = _resolve_python_executable()
        default_yt_dlp = f"{py_exe} -m yt_dlp"
        startup_dbg(f"[YT_DLP] bundled module found → default_yt_dlp={default_yt_dlp!r}")
    else:
        default_yt_dlp = "yt-dlp"
        startup_dbg("[YT_DLP] bundled module NOT found → falling back to system yt-dlp")

    # Resolve a bare relative path (no spaces, not absolute) against the project
    # root so that FileNotFoundError can't occur when CWD shifts after startup.
    if yt_dlp_path_raw and " " not in yt_dlp_path_raw and not os.path.isabs(yt_dlp_path_raw):
        yt_dlp_path_raw = os.path.join(project_root, yt_dlp_path_raw)
        startup_dbg(f"[YT_DLP] relative path resolved to absolute: {yt_dlp_path_raw!r}")

    return yt_dlp_path_raw if yt_dlp_path_raw else default_yt_dlp


def _inject_bundled_pythonpath(bundled_yt_dlp_dir: str) -> None:
    """Prepend *bundled_yt_dlp_dir* to PYTHONPATH if it is not already present."""
    current_pp = os.environ.get("PYTHONPATH", "")
    if bundled_yt_dlp_dir not in current_pp:
        os.environ["PYTHONPATH"] = (
            f"{bundled_yt_dlp_dir}{os.pathsep}{current_pp}" if current_pp
            else bundled_yt_dlp_dir
        )
    startup_dbg(f"[YT_DLP] PYTHONPATH set to: {os.environ.get('PYTHONPATH', '')!r}")


def _resolve_python_executable() -> str:
    """Return the path to python.exe, rewriting pythonw.exe on Windows.

    Subprocesses spawned from ``pythonw.exe`` inherit broken pipe handles and
    produce no output — yt-dlp goes completely silent.  Forcing ``python.exe``
    gives the child process a proper stdio environment.
    """
    py_exe = sys.executable
    if sys.platform == "win32" and py_exe.lower().endswith("pythonw.exe"):
        py_exe = py_exe[:-len("pythonw.exe")] + "python.exe"
        startup_dbg(f"[YT_DLP] pythonw.exe detected — rewriting to python.exe: {py_exe!r}")
    else:
        startup_dbg(f"[YT_DLP] python executable OK (not pythonw): {py_exe!r}")
    return py_exe


def load_config(config_path: str) -> dict:
    """Read a site config file and return a fully-resolved settings dict."""
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

    general = parser["General"] if parser.has_section("General") else {}

    cfg_dict = _parse_general_section(general, config_path)

    streamers, blocked = _parse_streamers_and_blocked(parser)
    cfg_dict["streamers"] = streamers
    cfg_dict["blocked"]   = blocked

    checker_cmd, downloader_cmd, lq_downloader_cmd = _parse_checker_and_downloader(parser)

    cfg_dict.update({
        "checker_cmd":       checker_cmd,
        "downloader_cmd":    downloader_cmd,
        "lq_downloader_cmd": lq_downloader_cmd,
        "username_idx":      _derive_username_idx(cfg_dict),
        "config_path":       config_path,
        "yt_dlp_path":       _resolve_yt_dlp_path(cfg_dict),
        **_parse_twitch_section(parser),
    })

    return cfg_dict


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
            # Allow 0 explicitly (e.g. MAX_CONCURRENT_REC = 0 means "unlimited").
            # Only fall back to the default when the raw string is absent/empty.
            return value if value >= 0 else default
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
        "max_concurrent_rec": _int("MAX_CONCURRENT_REC", 0),
        "lq_downloader":      _bool("LQ_DOWNLOADER", False),
        "ff_err_thresh":      _int("FF_ERR_THRESH", 200),
        "subfolders":         _bool("SUBFOLDERS", False),
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

_global_json_lock: threading.Lock = threading.Lock()

# How often global.json should be backed up.  The timestamp of the last
# backup is stored inside global.json itself (key "_last_backup_ts"), so the
# 24h window survives restarts instead of resetting every time the app launches.
_GLOBAL_JSON_BACKUP_INTERVAL: float = 24 * 60 * 60  # seconds


def _global_json_path() -> str:
    """Return the absolute path to global.json (next to this file)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "global.json")


def _load_global_json() -> dict:
    """Load the global.json file.  Returns an empty dict if the file does not
    exist or cannot be parsed."""
    try:
        with open(_global_json_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _backup_global_json_if_due(data: dict) -> None:
    """Back up global.json into backups/ if it's due, per "_last_backup_ts".

    "Due" means more than _GLOBAL_JSON_BACKUP_INTERVAL seconds have passed
    since the last backup, or no backup has ever been recorded.  *data* is
    the dict about to be written by _save_global_json; on a successful (or
    skipped-because-the-file-doesn't-exist-yet) check, it is updated in
    place with a fresh "_last_backup_ts" so the new timestamp gets persisted
    along with the rest of the save.  If copying the file fails, the
    timestamp is left untouched so the next save retries.
    """
    last_backup_ts = data.get("_last_backup_ts")
    now = time.time()
    if isinstance(last_backup_ts, (int, float)) and (now - last_backup_ts) < _GLOBAL_JSON_BACKUP_INTERVAL:
        return  # Backed up recently enough.

    src = _global_json_path()
    if os.path.isfile(src):
        # Same backups/ folder (sibling of configs/) used for global.conf and
        # site .conf backups in config_editor.py.
        backup_dir = os.path.abspath("backups")
        try:
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            backup_path = os.path.join(backup_dir, f"global.json.{stamp}.bak")
            shutil.copy2(src, backup_path)
            dbg(f"[GLOBAL_JSON] backup written to {backup_path!r}")
        except Exception as e:
            dbg(f"[GLOBAL_JSON] ERROR writing backup: {e}")
            return  # Don't update the timestamp — try again on the next save.

    data["_last_backup_ts"] = now


def _save_global_json(data: dict) -> None:
    """Write *data* to global.json.  Silently ignores errors.

    Before writing, backs up the current global.json to backups/ if it's
    been more than 24h since the last backup (see _backup_global_json_if_due).
    """
    _backup_global_json_if_due(data)
    try:
        with open(_global_json_path(), "w", encoding="utf-8") as f:
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
        self.evicted_streamers:   Set[str] = set()
        # Resolution (height, in px) each currently-recording streamer started
        # at, per the checker's --dump-json output. Used by UPGRADE_QUALITY to
        # detect when a source switches to a higher resolution mid-recording.
        # Guarded by self.lock. Cleared when the recording ends.
        self.recording_resolution: Dict[str, int] = {}
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

        # Log tab: whether to show debug messages inline (off by default — can be very verbose)
        self.show_debug_log: bool = False

        # Popup cooldown: streamer -> epoch of last popup shown
        self.popup_last_shown:    Dict[str, float] = {}
        # Streamers for whom a "not recording" (disabled / lower-priority) popup
        # has already been shown during the current continuous live session.
        # Cleared when the streamer goes offline so the popup can fire again
        # next time they go live. This prevents the popup from re-appearing
        # every popup_cooldown minutes for as long as the streamer stays live.
        self.popup_shown_session: set = set()

        # Active yt-dlp subprocesses: streamer -> proc
        # Written by record_stream threads; read by stop() for clean kill.
        self._procs_lock          = threading.Lock()
        self._active_procs:       Dict[str, object] = {}

        # ffmpeg error counts — streamer -> cumulative error count for current session
        # Written by _drain_pipe threads under dash_lock; read by the dashboard renderer.
        self.ffmpeg_error_counts: Dict[str, int] = {}

        # stall tracking — streamer -> epoch when file growth was last seen to stop
        # Set when size stops growing; cleared when growth resumes or recording ends.
        self.stall_since: Dict[str, float] = {}

        # Ad alert tracking — streamer -> epoch of most recent ad signal.
        # Written by _drain_pipe (update_ad_alert); read by draw_system_panel.
        self.ad_alerts: Dict[str, float] = {}

        # Cached config for the dashboard renderer — refreshed at most every 2s
        # so we avoid 7+ file reads per frame in draw_system_panel.
        self._cfg_cache:          Optional[dict] = None
        self._cfg_cache_time:     float = 0.0
        self._cfg_cache_lock:     threading.Lock = threading.Lock()
        
        self.last_ffmpeg_error:   Dict[str, float] = {}

    def register_proc(self, streamer: str, proc) -> None:
        """Register an active yt-dlp subprocess so stop() can kill it."""
        with self._procs_lock:
            self._active_procs[streamer] = proc

    def unregister_proc(self, streamer: str) -> None:
        """Remove a subprocess from the registry (after it exits)."""
        with self._procs_lock:
            self._active_procs.pop(streamer, None)

    def kill_proc_for_streamer(self, streamer: str) -> None:
        with self._procs_lock:
            proc = self._active_procs.get(streamer)
        if proc:
            try:
                kill_proc(proc)
            except Exception:
                pass

    def set_ffmpeg_error_count(self, streamer: str, count: int) -> None:
        """Update the ffmpeg error count for *streamer* (called from _drain_pipe)."""
        with self.dash_lock:
            if count > 0:
                self.ffmpeg_error_counts[streamer] = count
                self.last_ffmpeg_error[streamer] = time.time()
            else:
                self.ffmpeg_error_counts.pop(streamer, None)

    def clear_ffmpeg_error_count(self, streamer: str) -> None:
        """Reset the ffmpeg error count for *streamer* (called at recording start/reset)."""
        with self.dash_lock:
            self.ffmpeg_error_counts.pop(streamer, None)

    def set_stall_since(self, streamer: str, epoch: float) -> None:
        """Record that *streamer*'s file stopped growing at *epoch*."""
        with self.dash_lock:
            self.stall_since.setdefault(streamer, epoch)

    def clear_stall_since(self, streamer: str) -> None:
        """Clear stall tracking for *streamer* (growth resumed or recording ended)."""
        with self.dash_lock:
            self.stall_since.pop(streamer, None)

    def update_ad_alert(self, streamer: str) -> None:
        """Record that an ad signal was just seen for *streamer*."""
        with self.dash_lock:
            self.ad_alerts[streamer] = time.time()

    def clear_ad_alert(self, streamer: str) -> None:
        """Remove the ad alert for *streamer* (called when recording ends)."""
        with self.dash_lock:
            self.ad_alerts.pop(streamer, None)

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
                try:
                    mtime = os.path.getmtime(self.config_path)
                except Exception:
                    mtime = 0.0
                
                if self._cfg_cache is None or getattr(self, '_cfg_last_mtime', 0.0) != mtime:
                    t0 = time.time()
                    self._cfg_cache      = load_config(self.config_path)
                    self._cfg_last_mtime = mtime
                    dbg(f"[PERF][get_cached_config] load_config({self.config_path}) took {(time.time() - t0)*1000:.2f}ms")
                
                self._cfg_cache_time = now
            return self._cfg_cache

    def invalidate_config_cache(self) -> None:
        """Force the next get_cached_config() call to re-read the file.
        Call this after writing changes to the config (e.g. from ConfigEditor)."""
        with self._cfg_cache_lock:
            self._cfg_cache_time = 0.0

    def log_line(self, msg: str) -> None:
        """Append a timestamped line to the site's activity log (capped at 200 lines).

        If debug logging is currently enabled, the same line is also mirrored
        into the debug log file (see logger.log_dashboard_line) so the debug
        file always contains everything visible in the dashboard Log tab.
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        with self.dash_lock:
            self.dash_log_lines.append(line)
            if len(self.dash_log_lines) > 200:
                self.dash_log_lines = self.dash_log_lines[-200:]
        _logger.log_dashboard_line(msg)

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
# Global singletons
# ══════════════════════════════════════════════════════════════════════════════

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
# Debug messages routed to the Log tab are stored with this prefix so they can
# be toggled independently of regular activity messages.
_DEBUG_LOG_PREFIX: str = "\x00debug\x00"
FFMPEG_ERROR_RESTART_THRESHOLD: int = 200

# ── Ad detection patterns (used by _drain_pipe when AD_ALERTS is enabled) ─────
# Any match updates the per-streamer last-seen timestamp in site.ad_alerts.
_AD_DISCONTINUITY_RE = _re.compile(r"#EXT-X-DISCONTINUITY(?!-SEQUENCE)", _re.IGNORECASE)
_AD_SEGMENT_URL_RE   = _re.compile(r"(amazon|twitch-ad|/ad/|admanifest|/ads/)", _re.IGNORECASE)
_AD_TWITCH_TAG_RE    = _re.compile(r'#EXT-X-TWITCH-AD|CLASS="twitch-stitched-ad"', _re.IGNORECASE)

# ── LQ (low-quality) downloader bandwidth-saving state ───────────────────────
# Maps (streamer, site_label) → epoch when an LQ_Downloader recording was last
# *attempted* for that streamer.  Entries are cleared when the streamer goes
# offline.  Any entry whose timestamp is within _LQ_RECENT_WINDOW seconds of
# now is considered "recent" and makes the streamer ineligible for another LQ
# trigger during that online session.
_lq_attempted: Dict[Tuple[str, str], float] = {}
_lq_attempted_lock: threading.Lock = threading.Lock()
_LQ_RECENT_WINDOW: float = 30 * 60   # 30 minutes

# ── Keybinds ──
KEYBIND_ADD       = "a"
KEYBIND_REMOVE    = "r"
KEYBIND_DISABLE   = "d"
KEYBIND_LABELS = {
    KEYBIND_ADD:       "A",
    KEYBIND_REMOVE:    "R",
    KEYBIND_DISABLE:   "D",
}


# ══════════════════════════════════════════════════════════════════════════════
# Process helpers
# ══════════════════════════════════════════════════════════════════════════════

def kill_proc(proc) -> None:
    # Scan all running yt-dlp processes before attempting the kill so we can
    # compare the system-visible PIDs against the proc.pid we intend to kill.
    # This is Linux-only (/proc-based); skipped silently on other platforms.
    if sys.platform != "win32":
        try:
            ytdlp_pids = []
            for entry in os.scandir("/proc"):
                if not entry.name.isdigit():
                    continue
                try:
                    cmdline_path = f"/proc/{entry.name}/cmdline"
                    with open(cmdline_path, "r", encoding="utf-8", errors="replace") as _f:
                        cmdline = _f.read().replace("\x00", " ").strip()
                    if "yt_dlp" in cmdline or "yt-dlp" in cmdline:
                        ytdlp_pids.append((int(entry.name), cmdline[:120]))
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    pass
            if ytdlp_pids:
                pid_summary = "; ".join(f"pid={p} cmd={c!r}" for p, c in ytdlp_pids)
                dbg(f"[KILL][scan_procs] yt-dlp processes on system (count={len(ytdlp_pids)}): {pid_summary}")
            else:
                dbg("[KILL][scan_procs] no yt-dlp processes found on system")
        except Exception as _scan_err:
            dbg(f"[KILL][scan_procs] /proc scan failed: {_scan_err}")

    dbg(f"[KILL] Attempting to kill proc.pid={proc.pid}")
    if sys.platform == "win32":
        dbg(f"[KILL] win32: using taskkill on pid={proc.pid}")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        # PyInstaller yt-dlp binaries spawn two processes: a bootloader and the
        # real Python worker.  proc.kill() only kills the bootloader; the worker
        # becomes an orphan and keeps recording.  Kill the entire process group
        # instead so both processes are terminated together.
        import signal as _signal
        try:
            pgid = os.getpgid(proc.pid)
            dbg(f"[KILL] Linux: found pgid={pgid} for pid={proc.pid}, sending SIGKILL to pgid")
            os.killpg(pgid, _signal.SIGKILL)
            dbg(f"[KILL] Linux: successfully sent SIGKILL to pgid={pgid}")
        except (ProcessLookupError, OSError) as e:
            # Process already gone or pgid unavailable — fall back to direct kill
            dbg(f"[KILL] Linux: pgid lookup or killpg failed for pid={proc.pid} ({e}), falling back to proc.kill()")
            try:
                proc.kill()
                dbg(f"[KILL] Linux: successfully called proc.kill() for pid={proc.pid}")
            except Exception as e2:
                dbg(f"[KILL] Linux: proc.kill() failed for pid={proc.pid} ({e2})")


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

def _format_live_popup(streamer: str, is_recording: bool = True,
                       reason: str = "", warning: str = "") -> list:
    marker = "🔴" if is_recording else "🟡"
    status = "Recording" if is_recording else "Not recording"
    lines = [
        f"{marker} {streamer} is LIVE",
        f"● {status}",
    ]
    if warning:
        lines.append(f"Warning: {warning}")
    if reason:
        lines.append(f"Reason: {reason}")
    return lines


def _show_live_popup(streamer: str, source: str = "poll", popup_timeout: int = 15,
                     is_recording: bool = True, reason: str = "",
                     warning: str = "") -> None:
    dbg(f"[POPUP] enqueue popup streamer={streamer!r} source={source!r} timeout={popup_timeout} is_recording={is_recording} reason={reason!r} warning={warning!r}")
    def _run():
        popup_lines = _format_live_popup(streamer, is_recording, reason, warning)
        popup_text = "\n".join(popup_lines)
        if sys.platform.startswith("linux"):
            notify_cmd = shutil.which("notify-send")
            if notify_cmd:
                title = popup_lines[0]
                body = "\n".join(popup_lines[1:])
                try:
                    subprocess.run([notify_cmd, "-t", str(popup_timeout * 1000), title, body],
                                   check=False)
                    dbg(f"[POPUP] notify-send invoked for streamer={streamer!r}")
                    return
                except Exception as e:
                    dbg(f"[POPUP] notify-send failed for streamer={streamer!r}: {e}")
            else:
                dbg(f"[POPUP] notify-send not found; falling back to tkinter for streamer={streamer!r}")
        try:
            import tkinter as tk
            dbg(f"[POPUP] tkinter imported successfully for streamer={streamer!r}")
            root = tk.Tk()
            root.withdraw()
            win = tk.Toplevel(root)
            win.title("jj-dlp — Stream Live")
            win.resizable(False, False)
            win.attributes("-topmost", True)
            bg = "#15171a"
            fg = "#f4f5f7"
            muted_fg = "#d5d8de"
            accent = "#ff4d4f" if is_recording else "#f5c542"
            button_bg = "#24282e"
            button_active = "#303640"

            win.configure(bg=bg)
            content = tk.Frame(win, bg=bg, padx=22, pady=16)
            content.pack(fill="both", expand=True)

            title_row = tk.Frame(content, bg=bg)
            title_row.pack(anchor="w", fill="x")
            title_dot = tk.Canvas(title_row, width=14, height=14, bg=bg, highlightthickness=0)
            title_dot.create_oval(1, 1, 13, 13, fill=accent, outline=accent)
            title_dot.pack(side="left")
            tk.Label(title_row, text=f" {streamer} is LIVE", fg=fg, bg=bg,
                     font=("Segoe UI", 15, "bold")).pack(side="left")

            for line in popup_lines[1:]:
                text = line[2:] if len(line) > 1 and line[1] == " " else line
                row = tk.Frame(content, bg=bg)
                row.pack(anchor="w", fill="x", padx=(22, 0), pady=(3, 0))
                dot = tk.Canvas(row, width=8, height=8, bg=bg, highlightthickness=0)
                dot.create_oval(1, 1, 7, 7, fill=accent, outline=accent)
                dot.pack(side="left", pady=(4, 0))
                tk.Label(row, text=f" {text}", fg=muted_fg, bg=bg,
                         font=("Segoe UI", 11, "bold"), justify="left").pack(side="left")

            tk.Button(win, text="Dismiss", command=win.destroy, padx=14, pady=4,
                      bg=button_bg, fg=fg, activebackground=button_active,
                      activeforeground=fg, relief="flat",
                      highlightthickness=1, highlightbackground="#3a4048").pack(pady=(0, 14))
            win.after(popup_timeout * 1000, win.destroy)
            dbg(f"[POPUP] running popup mainloop for streamer={streamer!r}")
            root.mainloop()
        except ImportError as ie:
            dbg(f"[POPUP] tkinter import failed: {ie}")
        except Exception as e:
            dbg(f"[POPUP] exception while creating popup for streamer={streamer!r}: {e}")
    threading.Thread(target=_run, daemon=True, name=f"popup-{streamer}").start()


def _maybe_show_live_popup(streamer: str, cfg: dict, site: "SiteState",
                           show_popup: bool = True, source: str = "poll",
                           is_recording: bool = True, reason: str = "",
                           warning: str = "") -> None:
    if not show_popup or not cfg.get("popup_notifications", True):
        dbg(f"[POPUP] popup skipped for streamer={streamer!r} show_popup={show_popup} popup_notifications={cfg.get('popup_notifications', True)}")
        return

    # Streamers that are NOT being recorded (disabled / lower-priority) get
    # re-passed to this function on every single poll for as long as they
    # remain live, since nothing else about their state changes to exclude
    # them from the caller's candidate list. Relying on popup_cooldown alone
    # then means the popup keeps re-appearing every popup_cooldown minutes
    # for the entire time they're live. Instead, only show this popup once
    # per continuous live session; site.popup_shown_session is cleared when
    # the streamer goes offline (see the poll loop).
    if not is_recording and streamer in site.popup_shown_session:
        dbg(f"[POPUP] popup suppressed - already shown this live session for streamer={streamer!r} reason={reason!r}")
        return

    dbg(f"[POPUP] popup condition check for streamer={streamer!r} show_popup={show_popup} popup_notifications={cfg.get('popup_notifications', True)}")
    cooldown_secs = cfg.get("popup_cooldown", 30) * 60
    last_shown    = site.popup_last_shown.get(streamer, 0)
    elapsed       = time.time() - last_shown
    if elapsed >= cooldown_secs:
        dbg(f"[POPUP] popup allowed by cooldown for streamer={streamer!r} elapsed={elapsed:.1f}s cooldown={cooldown_secs}s")
        _show_live_popup(streamer, source=source,
                         popup_timeout=cfg.get("popup_timeout", 15),
                         is_recording=is_recording,
                         reason=reason,
                         warning=warning)
        site.popup_last_shown[streamer] = time.time()
        if not is_recording:
            site.popup_shown_session.add(streamer)
    else:
        dbg(f"[POPUP] popup suppressed by cooldown for streamer={streamer!r} elapsed={elapsed:.1f}s required={cooldown_secs}s")


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
    if cfg.get("logging"):
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


_YTDLP_DESTINATION_PREFIX: str = "[download] Destination: "


def _drain_pipe(pipe, log_fp, pipe_type: str,
                ffmpeg_error_counter=None, ffmpeg_error_event=None,
                streamer: str = "", site: Optional[SiteState] = None,
                filename_holder: Optional[List[str]] = None,
                filename_event: Optional[threading.Event] = None,
                ad_alerts_enabled: bool = False) -> None:
    """Drain one pipe (stdout or stderr) from a yt-dlp subprocess.

    If *filename_holder* and *filename_event* are provided, the first
    ``[download] Destination: <path>`` line seen on either pipe is stored in
    ``filename_holder[0]`` and *filename_event* is set, allowing
    ``record_stream`` to learn the exact output path without scanning the
    directory.
    """
    dbg(f"[DRAIN] thread started pipe_type={pipe_type!r} streamer={streamer!r} pipe={pipe!r}")
    line_count = 0
    try:
        for raw in pipe:
            line = raw.decode(errors="replace").rstrip("\n")
            line_count += 1
            if line_count <= 3:
                dbg(f"[DRAIN] pipe_type={pipe_type!r} streamer={streamer!r} line#{line_count}: {line[:200]!r}")

            # ── Parse yt-dlp destination filename ────────────────────────────
            if (filename_holder is not None and filename_event is not None
                    and not filename_event.is_set()):
                stripped = line.strip()
                if stripped.startswith(_YTDLP_DESTINATION_PREFIX):
                    dest = stripped[len(_YTDLP_DESTINATION_PREFIX):].strip()
                    if dest:
                        filename_holder.append(dest)
                        filename_event.set()
                        dbg(f"[DRAIN] parsed destination filename={dest!r} "
                            f"pipe_type={pipe_type!r} streamer={streamer!r}")

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
            if (ffmpeg_error_counter is not None and ffmpeg_error_event is not None
                    and FFMPEG_ERROR_RESTART_THRESHOLD > 0 and not ffmpeg_error_event.is_set()):
                line_lower = line.lower()
                for pattern in FFMPEG_ERROR_PATTERNS:
                    if pattern.lower() in line_lower:
                        ffmpeg_error_counter[0] += 1
                        if site is not None and streamer:
                            site.set_ffmpeg_error_count(streamer, ffmpeg_error_counter[0])
                        if ffmpeg_error_counter[0] >= FFMPEG_ERROR_RESTART_THRESHOLD:
                            ffmpeg_error_event.set()
                        break

            if ad_alerts_enabled and site is not None and streamer:
                if (_AD_DISCONTINUITY_RE.search(line) or
                        _AD_SEGMENT_URL_RE.search(line) or
                        _AD_TWITCH_TAG_RE.search(line)):
                    site.update_ad_alert(streamer)
                    dbg(f"[AD] signal detected streamer={streamer!r} "
                        f"pipe={pipe_type!r}: {line[:120]!r}",
                        site_name=streamer)
    except Exception as _drain_exc:
        dbg(f"[DRAIN] pipe_type={pipe_type!r} streamer={streamer!r} EXCEPTION: {_drain_exc!r}")
    dbg(f"[DRAIN] thread exiting pipe_type={pipe_type!r} streamer={streamer!r} total_lines={line_count}")


_RESOLUTION_RE = _re.compile(r'(\d+)\s*x\s*(\d+)')


def _extract_resolution_height(info: dict) -> Optional[int]:
    """Extract the vertical resolution (height, in px) a checker
    (--dump-json) result reports for a live stream.
    """
    if not isinstance(info, dict):
        return None

    res = info.get("resolution")
    if isinstance(res, str):
        m = _RESOLUTION_RE.search(res)
        if m:
            return int(m.group(2))

    return None


def get_live_streamers(streamers: List[str], cfg: dict,
                       site: Optional["SiteState"] = None) -> Dict[str, Optional[int]]:
    """Run the checker command and return the streamers found to be live.

    Returns a dict mapping each (lower-cased) live streamer username to the
    best-effort resolution height of their current stream (see
    _extract_resolution_height), or None if it couldn't be determined.
    """
    if not streamers:
        return {}
    # NOTE: Do NOT filter out blocked streamers here. We still need to know
    # if a blocked/disabled streamer is live so the dashboard can flash
    # [●Live] ↔ [DIS]. Recording is suppressed downstream in
    # start_recording_if_needed(), not here.
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
    if cfg["logging"]:
        out_path, err_path = get_log_file_paths(cfg)
        try:
            if result.stdout:
                with open(out_path, "a", encoding="utf-8") as _lf:
                    _lf.write(result.stdout)
        except Exception:
            pass
    # Feed checker stdout/stderr into the site's pipe buffers (tagged so the
    # tabs can filter them based on the "Show All" toggle).
    if site is not None:
        for _chk_line in result.stdout.splitlines():
            site.add_stdout_line(_CHECKER_STDOUT_PREFIX + _chk_line)
        for _chk_line in result.stderr.splitlines():
            site.add_stderr_line(_CHECKER_STDERR_PREFIX + _chk_line)
    live: Dict[str, Optional[int]] = {}
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
                    live[streamer] = _extract_resolution_height(info)
        except Exception:
            pass
    return live


def get_streamer_file_size(output_dir, streamer, cfg=None,
                           last_growth_time=None, stall_timeout=None,
                           stall_check_interval=None, proc_start_time=None,
                           known_filename=None):
    try:
        filename = known_filename
        size = os.path.getsize(filename) if filename else 0
        stall_detected = False
        if last_growth_time is not None and stall_timeout is not None:
            time_now = time.time()
            time_since_growth = time_now - last_growth_time
            stalled = max(0.0, time_since_growth - stall_check_interval)
            dbg(f"[STALL] size={size} time_since_growth={time_since_growth:.2f}s "
                f"stall_check_interval={stall_check_interval}s "
                f"stalled={stalled:.2f}s threshold={stall_timeout}s "
                f"file={filename!r}",
                site_name=streamer)
            if stalled >= stall_timeout:
                stall_detected = True
                dbg(f"[STALL] TRIGGERED: stalled={stalled:.2f}s >= threshold={stall_timeout}s",
                    site_name=streamer)
        return size, stall_detected, filename or "", False
    except Exception as e:
        dbg(f"[STALL] exception in get_streamer_file_size: {type(e).__name__}: {e}",
            site_name=streamer)
        return 0, False, "", True

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




def _launch_lq_recording(streamer: str, cfg: dict, site: "SiteState",
                          site_label: str) -> None:
    """Wait for the evicted recording thread to exit, then start an LQ recording.

    Runs in its own daemon thread so it never blocks the caller.
    """
    deadline = time.time() + 20.0
    while time.time() < deadline:
        with site.lock:
            if streamer not in site.currently_recording:
                break
        time.sleep(0.3)

    with site.lock:
        if streamer in site.currently_recording:
            dbg(f"[LQ] Timed out waiting for {streamer} eviction — aborting LQ start")
            return
        # Claim the slot before starting the thread.
        site.currently_recording.add(streamer)
        site.evicted_streamers.discard(streamer)
        with site.dash_lock:
            if streamer not in site.dash_live_since:
                site.dash_live_since[streamer] = time.time()

    site.log_line(f"LQ recording starting for {streamer}")
    dbg(f"[LQ] Launching LQ record_stream for {streamer}")
    t = threading.Thread(
        target=record_stream,
        args=(streamer, cfg, site),
        kwargs={"use_lq": True},
        daemon=True,
        name=f"lq-rec-{streamer}",
    )
    t.start()
    site.recording_threads.append(t)


def _maybe_trigger_lq(triggering_site: "SiteState", triggering_streamer: str) -> None:
    """Evaluate whether LQ-downloader conditions are satisfied and, if so,
    stop the lowest-priority eligible recording and restart it in LQ mode.

    Conditions for triggering:
      1. At least one OTHER currently-recording streamer has any ffmpeg errors.
      2. There is at least one currently-recording streamer that:
         - is not the triggering streamer,
         - is not a bypass streamer,
         - was not recently attempted with the LQ downloader (< _LQ_RECENT_WINDOW),
         - has an [LQ_Downloader] section configured in its site config.
    """
    now = time.time()

    # ── Gate: LQ_DOWNLOADER must be enabled in global.conf ───────────────────
    _gcfg = load_global_config()
    if not _gcfg.get("lq_downloader", False):
        dbg("[LQ] Skipping LQ trigger — LQ_DOWNLOADER is disabled in global config")
        return

    # ── Condition 1: another active recording must have ffmpeg errors ─────────
    has_other_errors = False
    for s in _global_sites:
        with s.dash_lock:
            counts = dict(s.ffmpeg_error_counts)
            recent = dict(getattr(s, "last_ffmpeg_error", {}))
        with s.lock:
            recording = set(s.currently_recording)
        for st in recording:
            if st == triggering_streamer and s is triggering_site:
                continue
            if counts.get(st, 0) > 0 or (now - recent.get(st, 0.0) < 300):
                has_other_errors = True
                break
        if has_other_errors:
            break

    if not has_other_errors:
        dbg("[LQ] Skipping LQ trigger — no other recording has ffmpeg errors")
        return

    # ── Load priority map from global.json ────────────────────────────────────
    with _global_json_lock:
        global_data = _load_global_json()
    config_id = _get_config_id()
    saved_entries = (global_data.get("priorities", {})
                                .get(config_id, {})
                                .get("entries", []))
    priority_map: Dict[Tuple[str, str], dict] = {}
    for e in saved_entries:
        key = (e.get("streamer", ""), e.get("site", ""))
        priority_map[key] = {
            "priority":      e.get("priority", 999999),
            "bypass":        e.get("bypass", False),
            "split_enabled": e.get("split_enabled", False),
            "split_after":   e.get("split_after", 0),
        }

    # ── Condition 2: find eligible candidates ─────────────────────────────────
    candidates = []
    for s in _global_sites:
        try:
            s_cfg = s.get_cached_config()
        except Exception:
            continue
        # Site must have an LQ_Downloader section configured.
        if not s_cfg.get("lq_downloader_cmd"):
            continue
        s_label = s_cfg.get("site_label", os.path.basename(s.config_path))
        with s.lock:
            recording = set(s.currently_recording) - s.evicted_streamers
        for st in recording:
            if st == triggering_streamer and s is triggering_site:
                continue
            key = (st, s_label)
            info = priority_map.get(key, {"priority": 999999, "bypass": False})
            # Bypass streamers are never throttled.
            if info.get("bypass", False):
                continue
            # Skip if recently LQ-attempted.
            with _lq_attempted_lock:
                attempt_ts = _lq_attempted.get(key, 0.0)
            if now - attempt_ts < _LQ_RECENT_WINDOW:
                dbg(f"[LQ] Skipping {st} — LQ attempted {now - attempt_ts:.0f}s ago (window={_LQ_RECENT_WINDOW}s)")
                continue
            candidates.append({
                "streamer":  st,
                "site":      s,
                "site_label": s_label,
                "priority":  info.get("priority", 999999),
                "cfg":       _resolve_split_after(s_cfg, info),
            })

    if not candidates:
        dbg("[LQ] LQ conditions met but no eligible candidates found")
        return

    # ── Choose the lowest-priority (highest numeric value) candidate ──────────
    target = max(candidates, key=lambda x: x["priority"])
    tgt_str   = target["streamer"]
    tgt_site  = target["site"]
    tgt_cfg   = target["cfg"]
    tgt_label = target["site_label"]

    dbg(f"[LQ] Targeting {tgt_str} (priority={target['priority']}) for LQ restart")
    tgt_site.log_line(
        f"Bandwidth save: stopping {tgt_str} and restarting in LQ mode"
    )

    # Record the attempt *before* evicting so re-entrant calls can't double-target.
    with _lq_attempted_lock:
        _lq_attempted[(tgt_str, tgt_label)] = now

    # Evict the current recording.
    with tgt_site.lock:
        tgt_site.evicted_streamers.add(tgt_str)
    tgt_site.kill_proc_for_streamer(tgt_str)

    # Launch the LQ restart in a background thread (waits for eviction to clear).
    threading.Thread(
        target=_launch_lq_recording,
        args=(tgt_str, tgt_cfg, tgt_site, tgt_label),
        daemon=True,
        name=f"lq-launch-{tgt_str}",
    ).start()


def _resolve_split_after(cfg: dict, entry_info: dict) -> dict:
    """Return a cfg dict to use for a single streamer, applying that
    streamer's per-streamer Split override (set via the SPLIT settings
    popup) on top of the site's SPLIT_AFTER config value if enabled.

    entry_info is the priorities[...][entries] dict-like info for the
    streamer, expected to (optionally) contain "split_enabled" (bool) and
    "split_after" (int, minutes). When split_enabled is falsy or
    split_after <= 0, the site's SPLIT_AFTER config is left untouched and
    the *same* cfg object is returned (no copy needed). The override never
    affects other streamers sharing the same site config.
    """
    if not entry_info:
        return cfg
    try:
        split_enabled = bool(entry_info.get("split_enabled", False))
        split_after   = int(entry_info.get("split_after", 0) or 0)
    except (TypeError, ValueError):
        return cfg
    if not split_enabled or split_after <= 0:
        return cfg
    overridden = dict(cfg)
    overridden["split_after"] = split_after
    return overridden


def record_stream(streamer: str, cfg: dict, site: "SiteState",
                  use_lq: bool = False) -> None:
    channel_url = cfg["site_tmpl"].format(username=streamer)
    output_dir  = cfg["output_dir"]

    # If SUBFOLDERS is enabled in global.conf, nest recordings under a
    # per-streamer subdirectory (e.g. recordings/streamer_name/).
    _global_cfg_rs = load_global_config()
    if _global_cfg_rs.get("subfolders", False):
        output_dir = os.path.join(output_dir, streamer)

    os.makedirs(output_dir, exist_ok=True)

    split_after_minutes = max(0, cfg.get("split_after", 0))
    split_after_seconds = split_after_minutes * 60

    dbg(f"[SPLIT][record_stream] ENTER streamer={streamer!r} "
        f"split_after_minutes={split_after_minutes} split_after_seconds={split_after_seconds} "
        f"output_dir={output_dir!r}")

    site.log_line(f"Recording started: {streamer}" + (" [LQ]" if use_lq else ""))

    # ── LQ attempt bookkeeping ────────────────────────────────────────────────
    # Record the attempt immediately so that re-entrant LQ triggers during this
    # session cannot target this streamer again (even if the proc hasn't opened yet).
    if use_lq:
        _lq_site_label = cfg.get("site_label", os.path.basename(site.config_path))
        with _lq_attempted_lock:
            _lq_attempted[(_lq_site_label_key := (streamer, _lq_site_label))] = time.time()
        dbg(f"[LQ] LQ attempt recorded for {streamer} on {_lq_site_label}")

    proc = None
    close_logs = lambda: None
    segment_num = 1
    # Backoff for split attempts: after a failed split (e.g. couldn't find/
    # confirm the new segment file), don't retry every second — wait a bit
    # so a persistent problem doesn't spawn a fresh yt-dlp probe process
    # every loop iteration. 0.0 means "no cooldown in effect yet".
    _split_retry_cooldown_seconds = 60.0
    next_split_retry_time = 0.0

    try:
        while True:
            if site._stop_event.is_set() or streamer in site.evicted_streamers:
                break
            current_output_tmpl = cfg["output_tmpl"]
            # For segment 1 we intentionally omit the _part1 suffix — it will be
            # retroactively added (via rename) only if a second part is ever created.
            if split_after_seconds > 0 and segment_num > 1:
                current_output_tmpl = add_segment_suffix_to_tmpl(
                    current_output_tmpl,
                    segment_num
                )

            output_path = os.path.join(output_dir, current_output_tmpl)

            # ── Select downloader command (normal vs LQ) ──────────────────
            _active_dl_cmd = cfg["downloader_cmd"]
            if use_lq:
                _lq_cmd = cfg.get("lq_downloader_cmd", [])
                if _lq_cmd:
                    _active_dl_cmd = _lq_cmd
                else:
                    dbg(f"[LQ] use_lq=True but lq_downloader_cmd is empty — falling back to normal downloader for {streamer}")

            cmd = build_yt_dlp_command(
                cfg["yt_dlp_path"],
                _active_dl_cmd,
                ["-o", output_path, channel_url]
            )

            out_target, err_target, close_logs, log_out_fp, log_err_fp = open_log_streams(cfg)

            try:
                _popen_kwargs: dict = dict(stdout=out_target, stderr=err_target)
                if sys.platform == "win32":
                    _popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                else:
                    # Put the child in its own process group so we can kill both
                    # the PyInstaller bootloader and the real yt-dlp process at once.
                    _popen_kwargs["start_new_session"] = True
                dbg(f"[POPEN] streamer={streamer!r} cmd={cmd_display_str(cmd)!r}")
                dbg(f"[POPEN] Windows CREATE_NO_WINDOW={'yes' if sys.platform == 'win32' else 'n/a'}")
                dbg(f"[POPEN] PYTHONPATH={os.environ.get('PYTHONPATH', '<not set>')!r}")
                proc = subprocess.Popen(cmd, **_popen_kwargs)
                proc_start_time = time.time()
                dbg(f"[POPEN] launched pid={proc.pid}")

                site.register_proc(streamer, proc)

                ffmpeg_error_counter = [0]
                ffmpeg_error_event   = threading.Event()
                site.clear_ffmpeg_error_count(streamer)
                site.clear_stall_since(streamer)

                # Shared container for the filename parsed from yt-dlp output.
                # Both drain threads write here; the event is set on first hit.
                filename_holder: List[str] = []
                filename_event  = threading.Event()

                threading.Thread(
                    target=_drain_pipe,
                    args=(proc.stdout, log_out_fp, "stdout"),
                    kwargs={
                        "ffmpeg_error_counter": ffmpeg_error_counter,
                        "ffmpeg_error_event": ffmpeg_error_event,
                        "streamer": streamer,
                        "site": site,
                        "filename_holder": filename_holder,
                        "filename_event": filename_event,
                        "ad_alerts_enabled": cfg.get("ad_alerts", False),
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
                        "site": site,
                        "filename_holder": filename_holder,
                        "filename_event": filename_event,
                        "ad_alerts_enabled": cfg.get("ad_alerts", False),
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

            # ── Resolve active output file ────────────────────────────────
            # Prefer the filename parsed directly from yt-dlp's
            # "[download] Destination: <path>" line; fall back to querying
            # yt-dlp for JSON metadata when the line doesn't appear within
            # the wait window (e.g. the process exits immediately on error).
            _FILENAME_WAIT_TIMEOUT = 15.0
            filename_found = filename_event.wait(timeout=_FILENAME_WAIT_TIMEOUT)
            active_file = None
            if filename_found and filename_holder:
                raw_dest = filename_holder[0]
                # yt-dlp may emit a bare filename or a full path depending on
                # how -o was specified.  Normalise to an absolute path.
                if not os.path.isabs(raw_dest):
                    active_file = os.path.join(output_dir, raw_dest)
                else:
                    active_file = raw_dest
                
                # Check if the file actually exists. If yt-dlp outputs garbage characters
                # for the filename (like missing Chinese characters on Windows), active_file
                # might be wrong. Wait briefly just in case it's still being written.
                _chk_start = time.time()
                while time.time() - _chk_start < 2.0:
                    if os.path.exists(active_file):
                        break
                    time.sleep(0.5)

                if not os.path.exists(active_file):
                    dbg(f"[STALL] active_file {active_file!r} from Destination line does not exist, discarding.", site_name=streamer)
                    active_file = None
                else:
                    dbg(f"[STALL] resolved active_file from yt-dlp output: {active_file!r}",
                        site_name=streamer)

            if not active_file:
                dbg(f"[STALL] falling back to JSON parsing for streamer={streamer!r}",
                    site_name=streamer)
                json_cmd = build_yt_dlp_command(
                    cfg["yt_dlp_path"],
                    [],
                    ["--dump-json", "--no-warnings", "-o", output_path, channel_url]
                )
                try:
                    _run_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
                    if sys.platform == "win32":
                        _run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                    
                    dbg(f"[STALL] running json_cmd: {cmd_display_str(json_cmd)!r}", site_name=streamer)
                    res = subprocess.run(json_cmd, **_run_kwargs)
                    if res.stdout:
                        for line in reversed(res.stdout.splitlines()):
                            line = line.strip()
                            if line.startswith('{') and line.endswith('}'):
                                data = json.loads(line)
                                raw_json_dest = data.get("filename") or data.get("_filename")
                                if raw_json_dest:
                                    if not os.path.isabs(raw_json_dest):
                                        active_file = os.path.join(output_dir, raw_json_dest)
                                    else:
                                        active_file = raw_json_dest
                                    dbg(f"[STALL] resolved active_file from JSON: {active_file!r}", site_name=streamer)
                                break
                except Exception as e:
                    dbg(f"[STALL] json fallback failed: {e}", site_name=streamer)

            last_size, _, _, _ = get_streamer_file_size(
                output_dir,
                streamer,
                cfg=cfg,
                proc_start_time=proc_start_time,
                known_filename=active_file,
            )

            last_growth_time     = time.time()
            recording_start_time = time.time()
            stall_check_interval = cfg["stall_check_interval"]
            stall_timeout        = cfg["stall_timeout"]
            seconds_since_check  = 0
            _split_log_counter   = 0  # throttle periodic split-timer dbg lines
            # Set once we've already warned the Log tab about a missing/unreadable
            # recording file, so we don't spam the same warning every stall-check
            # cycle. Reset whenever the file is found again or a new attempt starts.
            filename_error_warned = False
            dbg(f"[STALL] init: stall_timeout={stall_timeout}s "
                f"stall_check_interval={stall_check_interval}s "
                f"last_size={last_size} last_growth_time={last_growth_time:.2f}",
                site_name=streamer)

            dbg(f"[SPLIT][record_stream] inner loop starting: streamer={streamer!r} "
                f"segment_num={segment_num} pid={proc.pid} "
                f"split_after_seconds={split_after_seconds} "
                f"stall_check_interval={stall_check_interval} stall_timeout={stall_timeout}")

            while proc.poll() is None:

                if site._stop_event.is_set() or streamer in site.evicted_streamers:
                    kill_proc(proc)
                    proc.wait()
                    site.unregister_proc(streamer)
                    try:
                        close_logs()
                    except Exception:
                        pass
                    return

                _t0 = time.time()
                current_cfg = site.get_cached_config()
                _load_cfg_ms = (time.time() - _t0) * 1000
                if _split_log_counter % 30 == 0:
                    dbg(f"[PERF][record_stream/inner] get_cached_config took {_load_cfg_ms:.2f}ms streamer={streamer!r}")

                if streamer in current_cfg["blocked"]:
                    kill_proc(proc)
                    site.log_line(f"Recording STOPPED (blocked) -> {streamer}")
                    site.unregister_proc(streamer)
                    site.clear_ffmpeg_error_count(streamer)
                    site.clear_stall_since(streamer)
                    site.clear_ad_alert(streamer)

                    try:
                        close_logs()
                    except Exception:
                        pass

                    with site.lock:
                        site.currently_recording.discard(streamer)

                    # Interruptible: wake immediately on shutdown instead of
                    # blocking the thread (and thus main()'s shutdown join)
                    # for the full cooldown period.
                    site._stop_event.wait(timeout=cfg["cooldown_after_recording"])
                    return

                if ffmpeg_error_event.is_set():
                    site.log_line(f"ffmpeg error threshold reached for {streamer} — restarting")
                    kill_proc(proc)
                    site.unregister_proc(streamer)
                    site.clear_ad_alert(streamer)

                    try:
                        close_logs()
                    except Exception:
                        pass

                    # ── LQ bandwidth-saving trigger (non-LQ recordings only) ──
                    # Only trigger LQ for normal recordings; if a LQ recording
                    # itself hits the threshold we just let it restart normally.
                    if not use_lq:
                        _maybe_trigger_lq(site, streamer)

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

                    if elapsed >= split_after_seconds and time.time() >= next_split_retry_time:
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

                        try:
                            _next_popen_kwargs: dict = dict(
                                stdout=next_out_target,
                                stderr=next_err_target,
                            )
                            if sys.platform != "win32":
                                # Same process-group isolation as the primary Popen above.
                                _next_popen_kwargs["start_new_session"] = True
                            dbg(f"[POPEN] streamer={streamer!r} split cmd={cmd_display_str(next_cmd)!r}")
                            next_proc = subprocess.Popen(next_cmd, **_next_popen_kwargs)

                            next_proc_start_time = time.time()
                            dbg(f"[SPLIT][record_stream] next_proc started pid={next_proc.pid} "
                                f"next_proc_start_time={next_proc_start_time:.3f}")

                            threading.Thread(
                                target=_drain_pipe,
                                args=(next_proc.stdout, next_log_out_fp, "stdout"),
                                kwargs={
                                    "streamer": streamer,
                                    "site": site,
                                    "ad_alerts_enabled": cfg.get("ad_alerts", False),
                                },
                                daemon=True
                            ).start()

                            threading.Thread(
                                target=_drain_pipe,
                                args=(next_proc.stderr, next_log_err_fp, "stderr"),
                                kwargs={
                                    "streamer": streamer,
                                    "site": site,
                                    "ad_alerts_enabled": cfg.get("ad_alerts", False),
                                },
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

                                # Part 2 is confirmed — retroactively rename the first
                                # segment from its clean name to FILENAME_part1.ext now
                                # that we know multiple parts exist.
                                if segment_num == 1 and active_file and os.path.isfile(active_file):
                                    _part1_path = add_segment_suffix_to_tmpl(active_file, 1)
                                    try:
                                        os.rename(active_file, _part1_path)
                                        site.log_line(
                                            f"Renamed first segment to: {os.path.basename(_part1_path)}"
                                        )
                                        dbg(f"[SPLIT][record_stream] renamed first segment: "
                                            f"{active_file!r} -> {_part1_path!r}")
                                    except Exception as _ren_err:
                                        dbg(f"[SPLIT][record_stream] rename of first segment FAILED: "
                                            f"{_ren_err!r}")

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
                                site.clear_ffmpeg_error_count(streamer)
                                site.clear_stall_since(streamer)

                                last_size = 0
                                last_growth_time = time.time()
                                next_split_retry_time = 0.0

                                dbg(f"[SPLIT][record_stream] switched to part {segment_num} "
                                    f"pid={proc.pid} active_file={active_file!r} "
                                    f"recording_start_time reset")

                                continue

                            dbg(f"[SPLIT][record_stream] SPLIT FAILED — "
                                f"next_file={next_file!r} split_success={split_success} — "
                                f"killing next_proc pid={next_proc.pid} and continuing current segment")
                            next_split_retry_time = time.time() + _split_retry_cooldown_seconds
                            site.log_line(
                                f"Split verification FAILED for {streamer} — keeping current recording "
                                f"(will retry split in {int(_split_retry_cooldown_seconds)}s)"
                            )

                            kill_proc(next_proc)

                            try:
                                next_close_logs()
                            except Exception:
                                pass

                        except Exception as e:
                            dbg(f"[SPLIT][record_stream] EXCEPTION launching next proc: "
                                f"{type(e).__name__}: {e}")
                            next_split_retry_time = time.time() + _split_retry_cooldown_seconds
                            site.log_line(
                                f"Failed to start split recording for {streamer}: {e} "
                                f"(will retry split in {int(_split_retry_cooldown_seconds)}s)"
                            )

                time.sleep(1)
                seconds_since_check += 1

                if seconds_since_check >= stall_check_interval:
                    seconds_since_check = 0
                    dbg(f"[STALL] check cycle: elapsed_since_growth="
                        f"{time.time() - last_growth_time:.2f}s",
                        site_name=streamer)
                    current_size, stall_detected, _, file_error = get_streamer_file_size(
                        output_dir,
                        streamer,
                        cfg=cfg,
                        proc_start_time=proc_start_time,
                        last_growth_time=last_growth_time,
                        stall_timeout=stall_timeout,
                        stall_check_interval=stall_check_interval,
                        known_filename=active_file,
                    )

                    if file_error:
                        # We couldn't even locate/read the recording file this
                        # cycle (e.g. active_file points at a filename that
                        # doesn't exist). Don't let that masquerade as "no
                        # growth" — that would show a false "stalled" state on
                        # the dashboard. Just give up on stall detection for
                        # this file until it resolves itself.
                        dbg("[STALL] filename lookup failed — giving up on "
                            "stall detection for this cycle", site_name=streamer)
                        site.clear_stall_since(streamer)
                        if not filename_error_warned:
                            site.log_line(
                                f"Warning: stall checker could not locate file for {streamer}"
                            )
                            filename_error_warned = True

                    elif stall_detected:
                        site.log_line(f"Stall detected for {streamer} — restarting")

                        kill_proc(proc)
                        site.unregister_proc(streamer)
                        site.clear_stall_since(streamer)
                        site.clear_ad_alert(streamer)

                        try:
                            close_logs()
                        except Exception:
                            pass

                        time.sleep(5)
                        break

                    elif current_size > last_size:
                        filename_error_warned = False
                        dbg(f"[STALL] grew: {last_size} -> {current_size} "
                            f"(+{current_size - last_size} bytes), resetting timer",
                            site_name=streamer)
                        last_size = current_size
                        last_growth_time = time.time()
                        site.clear_stall_since(streamer)
                    else:
                        filename_error_warned = False
                        dbg(f"[STALL] NO GROWTH: size={current_size} "
                            f"stall_since={time.time() - last_growth_time:.2f}s",
                            site_name=streamer)
                        site.set_stall_since(streamer, last_growth_time)

            else:
                site.unregister_proc(streamer)
                site.clear_stall_since(streamer)
                site.clear_ffmpeg_error_count(streamer)
                site.clear_ad_alert(streamer)

                try:
                    close_logs()
                except Exception:
                    pass

                # ── Clear LQ tracking when streamer goes offline ──────────
                # This ensures the next time they go live the normal downloader
                # is used (LQ is only attempted once per online session).
                _offline_site_label = cfg.get("site_label", os.path.basename(site.config_path))
                with _lq_attempted_lock:
                    _lq_attempted.pop((streamer, _offline_site_label), None)

                with site.dash_lock:
                    site.dash_last_live[streamer] = time.time()
                    _last_live_snapshot = dict(site.dash_last_live)
                _save_last_live_cache(site.config_path, _last_live_snapshot)

                site.log_line(f"Recording finished: {streamer}")
                break

    except KeyboardInterrupt:
        if proc is not None:
            try:
                kill_proc(proc)
            except Exception:
                pass

        site.unregister_proc(streamer)
        site.clear_ad_alert(streamer)

        try:
            close_logs()
        except Exception:
            pass

    finally:
        with site.lock:
            site.currently_recording.discard(streamer)
            # Clear the UPGRADE_QUALITY baseline along with currently_recording
            # so the next time this streamer starts recording (fresh or
            # restarted-for-quality) gets a clean baseline rather than
            # comparing against a stale resolution from a previous session.
            site.recording_resolution.pop(streamer, None)
            # Always clean up evicted_streamers here so the set doesn't grow
            # unboundedly over the lifetime of the process.  The eviction flag
            # is only meaningful while the recording thread is alive; once we
            # reach this finally block the thread is done regardless of why it
            # stopped (normal end, eviction, or crash).
            site.evicted_streamers.discard(streamer)

        site.clear_ad_alert(streamer)

        # Interruptible: on shutdown this returns instantly instead of
        # keeping the thread (and is_alive()) reporting "active" for up to
        # cooldown_after_recording seconds after the recording has actually
        # stopped. This is what was inflating the shutdown count and making
        # quit take so long.
        site._stop_event.wait(timeout=cfg["cooldown_after_recording"])


def start_recording_if_needed(live_now: List[str], cfg: dict, site: "SiteState",
                               show_popup: bool = True, source: str = "poll",
                               resolution_map: Optional[Dict[str, Optional[int]]] = None) -> None:
    with site.lock:
        currently_recording = set(site.currently_recording)
        blocked = set(cfg["blocked"])
        disabled_live = [s for s in live_now
                         if s in blocked and s not in currently_recording]
        to_start = [s for s in live_now
                    if s not in currently_recording and s not in blocked]

    for streamer in disabled_live:
        _maybe_show_live_popup(streamer, cfg, site, show_popup=show_popup,
                               source=source, is_recording=False,
                               reason="Disabled")

    if not to_start:
        site.recording_threads[:] = [t for t in site.recording_threads if t.is_alive()]
        return

    global_cfg = load_global_config()
    max_concurrent = global_cfg.get("max_concurrent_rec", 0)

    with _global_json_lock:
        global_data = _load_global_json()

    config_id = _get_config_id()
    saved_entries = global_data.get("priorities", {}).get(config_id, {}).get("entries", [])
    
    priority_map = {}
    for e in saved_entries:
        s_name = e.get("streamer", "")
        s_site = e.get("site", "")
        priority_map[(s_name, s_site)] = {
            "priority": e.get("priority", 999999),
            "bypass": e.get("bypass", False),
            "lq_enabled": e.get("lq_enabled", False),
            "split_enabled": e.get("split_enabled", False),
            "split_after": e.get("split_after", 0),
        }

    site_label = cfg.get("site_label", os.path.basename(site.config_path))

    with _recording_start_lock:
        # Re-check what still needs to start
        with site.lock:
            to_start = [s for s in to_start
                        if s not in site.currently_recording and s not in cfg["blocked"]]
            if not to_start:
                return

        for streamer in to_start:
            streamer_info = priority_map.get((streamer, site_label), {"priority": 999999, "bypass": False, "lq_enabled": False})
            is_bypass = streamer_info["bypass"]
            streamer_prio = streamer_info["priority"]
            is_lq = streamer_info.get("lq_enabled", False)
            streamer_cfg = _resolve_split_after(cfg, streamer_info)
            eviction_warning = ""

            # Concurrency enforcement
            # Lock ordering inside this block:
            #   _recording_start_lock  (already held by the outer `with`)
            #   -> site.lock / s.lock   (acquired below, released before kill)
            #   -> kill_proc_for_streamer (no locks held during the blocking call)
            #
            # Stale-count window: after kill_proc_for_streamer() returns, the
            # evicted record_stream thread is still alive until its finally block
            # removes the streamer from currently_recording. Both the evicted
            # and the new streamer are briefly in currently_recording. Because
            # _recording_start_lock serialises all starts, this window cannot
            # trigger a second eviction cascade.
            if max_concurrent > 0:
                active_recordings = []
                for s in _global_sites:
                    s_cfg = s.get_cached_config()
                    s_label = s_cfg.get("site_label", os.path.basename(s.config_path))
                    with s.lock:
                        for act_str in s.currently_recording:
                            if act_str in s.evicted_streamers:
                                continue
                            act_info = priority_map.get((act_str, s_label), {"priority": 999999, "bypass": False})
                            active_recordings.append({
                                "streamer": act_str,
                                "site": s,
                                "priority": act_info["priority"],
                                "bypass": act_info["bypass"]
                            })

                if len(active_recordings) >= max_concurrent:
                    if is_bypass:
                        eviction_candidates = [r for r in active_recordings if not r["bypass"]]
                    else:
                        eviction_candidates = [r for r in active_recordings
                                               if not r["bypass"] and r["priority"] > streamer_prio]

                    if eviction_candidates:
                        evict_target = max(eviction_candidates, key=lambda x: x["priority"])
                        target_site     = evict_target["site"]
                        target_streamer = evict_target["streamer"]
                        dbg(f"[CONCURRENCY] Evicting {target_streamer} "
                            f"(prio: {evict_target['priority']}) for {streamer} "
                            f"(prio: {streamer_prio}, bypass={is_bypass})")
                        target_site.log_line(
                            f"Warning: Evicted {target_streamer} (lower priority) - making room for {streamer}"
                        )
                        with target_site.lock:
                            target_site.evicted_streamers.add(target_streamer)
                        # kill_proc_for_streamer is called without holding
                        # any site.lock so it cannot deadlock against the
                        # finally block in record_stream.
                        target_site.kill_proc_for_streamer(target_streamer)
                        eviction_warning = f"evicted {target_streamer}"

                    elif not is_bypass:
                        dbg(f"[CONCURRENCY] max_concurrent ({max_concurrent}) reached. "
                            f"Streamer {streamer} (prio: {streamer_prio}) cannot evict "
                            f"any active stream.")
                        _maybe_show_live_popup(streamer, cfg, site,
                                               show_popup=show_popup,
                                               source=source,
                                               is_recording=False,
                                               reason="Lower priority")
                        continue

            with site.lock:
                site.currently_recording.add(streamer)
                site.evicted_streamers.discard(streamer)
                if resolution_map is not None:
                    _start_height = resolution_map.get(streamer)
                    if _start_height is not None:
                        site.recording_resolution[streamer] = _start_height
                    else:
                        site.recording_resolution.pop(streamer, None)
                with site.dash_lock:
                    if streamer not in site.dash_live_since:
                        site.dash_live_since[streamer] = time.time()
            _maybe_show_live_popup(streamer, cfg, site,
                                   show_popup=show_popup,
                                   source=source,
                                   is_recording=True,
                                   warning=eviction_warning)
            t = threading.Thread(target=record_stream, args=(streamer, streamer_cfg, site), kwargs={"use_lq": is_lq}, daemon=True)
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


def _process_streamer_schedules(site: "SiteState") -> None:
    """Evaluate schedule-based enable/disable for every streamer configured in
    global.json for the current config-id.

    Called at the top of each monitor_site iteration (every check_interval
    seconds, or sooner if trigger_event fires).

    Enable / disable logic:
      Enable:  now >= start  AND (last_enable  is None OR last_enable  < start)
      Disable: now >= end    AND (last_disable is None OR last_disable < end)

    For recurring schedules the most-recent occurrence of start/end is
    computed dynamically and the same logic is applied.
    """
    config_id = _get_config_id()
    now       = datetime.now()

    # Read entries outside the write-lock so _modify_config_streamer can run
    # without risk of deadlock (it touches .conf files, not global.json).
    with _global_json_lock:
        gdata = _load_global_json()

    prio_block = gdata.get("priorities", {}).get(config_id, {})
    entries    = prio_block.get("entries", [])
    if not entries:
        return

    # Only process entries that belong to this site
    def normalize_label(lbl: str) -> str:
        if not lbl:
            return ""
        lbl = lbl.lower().strip()
        if lbl.endswith(".conf"):
            lbl = lbl[:-5]
        return lbl

    try:
        current_site_label = normalize_label(site.get_cached_config().get(
            "site_label", os.path.basename(site.config_path)
        ))
    except Exception:
        current_site_label = normalize_label(os.path.basename(site.config_path))
    
    # Collect actions: list of (streamer, site_label, conf_action, log_label)
    # conf_action is "add" (enable) or "disable".
    pending: list = []

    _DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    _now_str   = now.strftime("%Y-%m-%d %H:%M:%S")

    for entry in entries:
        streamer    = entry.get("streamer", "")
        site_label  = normalize_label(entry.get("site", ""))
        if not streamer:
            continue

        # Skip entries that belong to a different site
        if site_label != current_site_label:
            continue

        sched = entry.get("schedule", {})

        # Log even skipped entries so every streamer is accounted for each cycle.
        if not sched.get("enabled"):
            dbg(
                f"[SCHEDULE] {streamer!r}: schedule not enabled — ignored",
                site.config_path,
            )
            continue

        mode = sched.get("mode", "one_off")

        def _parse_attempt(s):
            if not s:
                return None
            try:
                return datetime.fromisoformat(s)
            except Exception:
                return None

        last_enable  = _parse_attempt(sched.get("last_enable_attempt"))
        last_disable = _parse_attempt(sched.get("last_disable_attempt"))

        if mode == "one_off":
            oo = sched.get("one_off", {})
            try:
                start_dt = datetime.strptime(oo.get("start", ""), "%Y-%m-%d %H:%M")
                end_dt   = datetime.strptime(oo.get("end",   ""), "%Y-%m-%d %H:%M")
            except Exception:
                dbg(
                    f"[SCHEDULE] {streamer!r}: one_off — bad start/end format, skipping",
                    site.config_path,
                )
                continue

            dbg(
                f"[SCHEDULE] {streamer!r}: one_off check — "
                f"now={_now_str}  "
                f"start={start_dt.strftime('%Y-%m-%d %H:%M')}  "
                f"end={end_dt.strftime('%Y-%m-%d %H:%M')}  "
                f"last_enable={last_enable}  last_disable={last_disable}",
                site.config_path,
            )

            # ── Enable decision ───────────────────────────────────────────────
            if now >= start_dt:
                if last_enable is None or last_enable < start_dt:
                    dbg(
                        f"[SCHEDULE] {streamer!r}: → ENABLE "
                        f"(now≥start; last_enable={last_enable or 'never'})",
                        site.config_path,
                    )
                    pending.append((streamer, site_label, "add", "enabled"))
                else:
                    dbg(
                        f"[SCHEDULE] {streamer!r}: → enable skipped "
                        f"(already enabled at {last_enable})",
                        site.config_path,
                    )
            else:
                dbg(
                    f"[SCHEDULE] {streamer!r}: → enable skipped "
                    f"(start not yet reached: {start_dt.strftime('%Y-%m-%d %H:%M')})",
                    site.config_path,
                )

            # ── Disable decision ──────────────────────────────────────────────
            if now >= end_dt:
                if last_disable is None or last_disable < end_dt:
                    dbg(
                        f"[SCHEDULE] {streamer!r}: → DISABLE "
                        f"(now≥end; last_disable={last_disable or 'never'})",
                        site.config_path,
                    )
                    pending.append((streamer, site_label, "disable", "disabled"))
                else:
                    dbg(
                        f"[SCHEDULE] {streamer!r}: → disable skipped "
                        f"(already disabled at {last_disable})",
                        site.config_path,
                    )
            else:
                dbg(
                    f"[SCHEDULE] {streamer!r}: → disable skipped "
                    f"(end not yet reached: {end_dt.strftime('%Y-%m-%d %H:%M')})",
                    site.config_path,
                )

        elif mode == "recurring":
            rec            = sched.get("recurring", {})
            days           = rec.get("days", [])        # list of ints 0=Mon…6=Sun
            start_time_str = rec.get("start_time", "")
            end_time_str   = rec.get("end_time",   "")

            if not days or not start_time_str or not end_time_str:
                dbg(
                    f"[SCHEDULE] {streamer!r}: recurring — missing days/start_time/end_time, skipping",
                    site.config_path,
                )
                continue

            try:
                sh, sm = map(int, start_time_str.split(":"))
                eh, em = map(int, end_time_str.split(":"))
            except Exception:
                dbg(
                    f"[SCHEDULE] {streamer!r}: recurring — bad time format "
                    f"(start={start_time_str!r} end={end_time_str!r}), skipping",
                    site.config_path,
                )
                continue

            # Whether the window crosses midnight (e.g. 22:00 → 02:00 next day).
            crosses_midnight = (eh * 60 + em) < (sh * 60 + sm)

            # Most-recent occurrence of start_time on any selected weekday, <= now.
            most_recent_start = None
            for delta in range(14):
                cand_date = (now - timedelta(days=delta)).date()
                if cand_date.weekday() in days:
                    cand_dt = datetime.combine(cand_date, _dt_time(sh, sm))
                    if cand_dt <= now:
                        most_recent_start = cand_dt
                        break

            # Most-recent occurrence of end_time.
            # If the window crosses midnight the end falls on the day AFTER the
            # selected weekday; otherwise it falls on the same selected day.
            most_recent_end = None
            for delta in range(14):
                cand_date = (now - timedelta(days=delta)).date()
                if crosses_midnight:
                    prev_date = cand_date - timedelta(days=1)
                    if prev_date.weekday() in days:
                        cand_dt = datetime.combine(cand_date, _dt_time(eh, em))
                        if cand_dt <= now:
                            most_recent_end = cand_dt
                            break
                else:
                    if cand_date.weekday() in days:
                        cand_dt = datetime.combine(cand_date, _dt_time(eh, em))
                        if cand_dt <= now:
                            most_recent_end = cand_dt
                            break

            _days_str = ",".join(_DAY_NAMES[d] for d in sorted(days) if 0 <= d <= 6)
            dbg(
                f"[SCHEDULE] {streamer!r}: recurring check — "
                f"now={_now_str}  "
                f"days=[{_days_str}]  "
                f"window={start_time_str}→{end_time_str}  "
                f"crosses_midnight={crosses_midnight}  "
                f"most_recent_start={most_recent_start}  "
                f"most_recent_end={most_recent_end}  "
                f"last_enable={last_enable}  last_disable={last_disable}",
                site.config_path,
            )

            # ── Enable decision ───────────────────────────────────────────────
            if most_recent_start is not None:
                if last_enable is None or last_enable < most_recent_start:
                    dbg(
                        f"[SCHEDULE] {streamer!r}: → ENABLE "
                        f"(most_recent_start={most_recent_start}; "
                        f"last_enable={last_enable or 'never'})",
                        site.config_path,
                    )
                    pending.append((streamer, site_label, "add", "enabled"))
                else:
                    dbg(
                        f"[SCHEDULE] {streamer!r}: → enable skipped "
                        f"(already enabled at {last_enable}; "
                        f"most_recent_start={most_recent_start})",
                        site.config_path,
                    )
            else:
                dbg(
                    f"[SCHEDULE] {streamer!r}: → enable skipped "
                    f"(no matching start day found in past 14 days)",
                    site.config_path,
                )

            # ── Disable decision ──────────────────────────────────────────────
            if most_recent_end is not None:
                if last_disable is None or last_disable < most_recent_end:
                    dbg(
                        f"[SCHEDULE] {streamer!r}: → DISABLE "
                        f"(most_recent_end={most_recent_end}; "
                        f"last_disable={last_disable or 'never'})",
                        site.config_path,
                    )
                    pending.append((streamer, site_label, "disable", "disabled"))
                else:
                    dbg(
                        f"[SCHEDULE] {streamer!r}: → disable skipped "
                        f"(already disabled at {last_disable}; "
                        f"most_recent_end={most_recent_end})",
                        site.config_path,
                    )
            else:
                dbg(
                    f"[SCHEDULE] {streamer!r}: → disable skipped "
                    f"(no matching end day found in past 14 days)",
                    site.config_path,
                )

    if not pending:
        return

    # Execute config changes outside the global-json lock.
    attempt_ts = now.isoformat(timespec="seconds")
    for streamer, site_label, conf_action, log_label in pending:
        result = _modify_config_streamer(site.config_path, streamer, conf_action)
        site.log_line(f"Schedule: {log_label} {streamer}  ({result.strip()})")
        dbg(f"[CHECKER] schedule {log_label} {streamer}: {result.strip()}", site.config_path)

    # Persist attempt timestamps (re-read to avoid racing with other writers).
    with _global_json_lock:
        gdata   = _load_global_json()
        entries = (gdata.get("priorities", {})
                       .get(config_id, {})
                       .get("entries", []))
        for streamer, site_label, conf_action, log_label in pending:
            for e in entries:
                if e.get("streamer") == streamer and e.get("site") == site_label:
                    sched = e.setdefault("schedule", {})
                    if log_label == "enabled":
                        sched["last_enable_attempt"] = attempt_ts
                    else:
                        sched["last_disable_attempt"] = attempt_ts
                    break
        if "priorities" in gdata and config_id in gdata["priorities"]:
            gdata["priorities"][config_id]["entries"] = entries
        _save_global_json(gdata)

    # Trigger an immediate liveness recheck so the new enable/disable state is
    # picked up without waiting for the full check_interval.
    site.trigger_event.set()



def _check_quality_upgrades(site: "SiteState",
                            live_info: Dict[str, Optional[int]]) -> None:
    """Compare this cycle's checker resolutions against the resolution each
    currently-recording streamer started at (UPGRADE_QUALITY feature).

    If a streamer's source has switched to a higher resolution since the
    recording began (e.g. the streamer started at a low res and then fixed
    their settings), restart that streamer's recording so subsequent output
    is captured at the new, higher quality. Restart reuses the same
    kill+evict path as the concurrency-eviction feature: the current
    record_stream thread notices it's in evicted_streamers, tears itself
    down cleanly, and start_recording_if_needed picks the streamer back up
    fresh on (or before) the next poll cycle since it's still live.
    """
    with site.lock:
        active = set(site.currently_recording) - site.evicted_streamers

    dbg(f"[UPGRADE_QUALITY] Checking quality upgrades for {site.label}, active_recordings={active}")
    for streamer in active:
        if streamer not in live_info:
            dbg(f"[UPGRADE_QUALITY] {streamer} not in live_info - skipping")
            continue
        new_height = live_info[streamer]
        if new_height is None:
            dbg(f"[UPGRADE_QUALITY] {streamer} new_height is None - skipping")
            continue

        with site.lock:
            old_height = site.recording_resolution.get(streamer)
            if old_height is None:
                # No baseline yet (e.g. recording was started via EventSub,
                # which doesn't have checker JSON handy) -- establish one now
                # rather than guessing whether this is an upgrade.
                dbg(f"[UPGRADE_QUALITY] {streamer} old_height is None, establishing baseline: {new_height}p")
                site.recording_resolution[streamer] = new_height
                continue
            is_upgrade = new_height > old_height

        dbg(f"[UPGRADE_QUALITY] {streamer}: old_height={old_height}p, new_height={new_height}p, is_upgrade={is_upgrade}")
        if is_upgrade:
            dbg(f"[UPGRADE_QUALITY] Restarting recording for {streamer} to capture higher quality ({new_height}p > {old_height}p)")
            site.log_line(
                f"Quality upgrade detected for {streamer}: "
                f"{old_height}p -> {new_height}p — restarting recording to capture higher quality"
            )
            with site.lock:
                site.recording_resolution[streamer] = new_height
                site.evicted_streamers.add(streamer)
            site.kill_proc_for_streamer(streamer)


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
            if broadcaster_login in current_cfg.get("streamers", []):
                start_recording_if_needed([broadcaster_login], current_cfg, site,
                                          source="eventsub")

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
        # Evaluate schedule-based enable/disable for all streamers before the
        # liveness check so any config changes take effect this iteration.
        try:
            _process_streamer_schedules(site)
        except Exception as _sched_exc:
            dbg(f"[CHECKER] schedule processing error: {_sched_exc}", site.config_path)

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
            live_info = get_live_streamers(streamers, cfg, site=site)
            live_now  = list(live_info.keys())
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
                        site.popup_shown_session.discard(s)
                    elif s not in site.dash_live_since:
                        site.dash_live_since[s] = time.time()

            if live_now:
                start_recording_if_needed(live_now, cfg, site, resolution_map=live_info)

            if cfg.get("upgrade_quality", False):
                _check_quality_upgrades(site, live_info)

        wait_secs = cfg.get("check_interval", 60)
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
        self._mgmt_sel    = 0   # selected index for disable/remove list
        self._mgmt_scroll = 0   # scroll offset for disable/remove list
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

        # Sort manager — controls streamer ordering in site panels
        self.sort_manager = SiteSortManager(self)

        # ── Changelog popup state ─────────────────────────────────────────────
        # Shown once after startup when update_available=false & changelog_shown=false.
        self._changelog_popup_open   = False
        self._changelog_scroll       = 0   # lines scrolled up from the bottom (0 = top)
        self._changelog_lines: List[str] = []
        self._changelog_popup_queued = False   # will be set to True after first frame

        # ── Exit-confirmation popup state ─────────────────────────────────────
        self._exit_confirm_open      = False
        self._exit_confirm_sel       = 0   # 0 = Yes (default), 1 = No

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
            self.safe_addstr(self.stdscr, y + i, x, line,
                        curses.color_pair(self.C_LOGO) | curses.A_BOLD)

    # ── Tab bar ──────────────────────────────────────────────────────────────
    def draw_tabs(self, y, x):
        for i, tab in enumerate(self.TABS):
            label = f"  {tab}  "
            if i == self.selected_tab:
                self.safe_addstr(self.stdscr, y, x, label,
                            curses.color_pair(self.C_HILIGHT) | curses.A_BOLD)
            else:
                self.safe_addstr(self.stdscr, y, x, label, curses.color_pair(self.C_INVHEAD))
            x += len(label) + 1

    # ── System status sidebar ────────────────────────────────────────────────
    def draw_system_panel(self, y1, x1, y2, x2):
        """Draws the SYSTEM info panel (from demo). Placed in the sidebar."""
        self.draw_box(self.stdscr, y1, x1, y2, x2, self.C_SYSTEM)
        self.safe_addstr(self.stdscr, y1, x1 + 2, " SYSTEM ",
                    curses.color_pair(self.C_SYSTEM) | curses.A_BOLD)

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
                live_since = dict(site.dash_live_since)
                blocked    = set(site.dash_blocked)
            with site.lock:
                recording  = set(site.currently_recording)
            try:
                cfg = site.get_cached_config()
                site_label = cfg.get("site_label", os.path.basename(site.config_path))
                site_setting_values.append((site_label, cfg))
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
                except Exception:
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
                            curses.color_pair(self.C_DIM))
                self.safe_addstr(self.stdscr, row_y, x1 + 2 + label_w + 1,
                            str(val)[:inner_w - label_w - 1],
                            curses.color_pair(cpair) | curses.A_BOLD)

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
                                curses.color_pair(self.C_REC))
                    ffmpeg_row_y += 1
                for _streamer, _count in all_ffmpeg_errors:
                    if ffmpeg_row_y >= y2 - 1:
                        break
                    _label = _streamer[:label_w].ljust(label_w)
                    _val   = str(_count)
                    self.safe_addstr(self.stdscr, ffmpeg_row_y, x1 + 2,
                                _label,
                                curses.color_pair(self.C_REC))
                    self.safe_addstr(self.stdscr, ffmpeg_row_y, x1 + 2 + label_w + 1,
                                _val[:inner_w - label_w - 1],
                                curses.color_pair(self.C_REC))
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
                                curses.color_pair(self.C_REC))
                    disk_row_y += 1
                for _streamer, _secs in all_stalls:
                    if disk_row_y >= y2 - 1:
                        break
                    _label = _streamer[:label_w].ljust(label_w)
                    _val   = _fmt_duration(int(_secs))
                    self.safe_addstr(self.stdscr, disk_row_y, x1 + 2,
                                _label,
                                curses.color_pair(self.C_REC))
                    self.safe_addstr(self.stdscr, disk_row_y, x1 + 2 + label_w + 1,
                                _val[:inner_w - label_w - 1],
                                curses.color_pair(self.C_REC))
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
                                curses.color_pair(self.C_WARN) | curses.A_BOLD)
                    disk_row_y += 1
                for _streamer in all_ad_alerts:
                    if disk_row_y >= y2 - 1:
                        break
                    _label = _streamer[:label_w].ljust(label_w)
                    _attr  = curses.color_pair(self.C_WARN) | curses.A_BOLD
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

                self._disk_cache_time = now  # update immediately to prevent multiple threads
                
                # Query disk usage for each drive in a background thread
                def _update_disk_usage(drives_to_check):
                    import threading
                    t0 = time.time()
                    results = []
                    for drive in drives_to_check:
                        try:
                            usage = shutil.disk_usage(drive)
                            results.append((drive, usage))
                            dbg(f"[DISK] {drive!r} → free={usage.free/(1024**3):.1f}G")
                        except Exception as _disk_exc:
                            results.append((drive, None))
                            dbg(f"[DISK] shutil.disk_usage({drive!r}) FAILED: {type(_disk_exc).__name__}: {_disk_exc}")
                    
                    self._disk_cache_results = results
                    self._disk_cache_drives  = drives_to_check
                    dbg(f"[PERF][disk_usage] background check for {drives_to_check} took {(time.time() - t0)*1000:.2f}ms")

                import threading
                threading.Thread(target=_update_disk_usage, args=(drives,), daemon=True).start()

            if disk_row_y < y2 - 1:
                self.safe_addstr(self.stdscr, disk_row_y, x1 + 2, "── Disk ──",
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
                self.safe_addstr(self.stdscr, disk_row_y, x1 + 2,
                            disk_str[:inner_w],
                            curses.color_pair(color))
                disk_row_y += 1
        except Exception as _disk_outer_exc:
            dbg(f"[DISK] outer exception in disk section: {type(_disk_outer_exc).__name__}: {_disk_outer_exc}")

        # Uptime at bottom
        self.safe_addstr(self.stdscr, y2 - 1, x1 + 2,
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
        self.draw_box(self.stdscr, y1, x1, y2, x2, border_pair)

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
        with site.lock:
            recording     = set(site.currently_recording)
            recording_res = dict(site.recording_resolution)

        # Apply the active sort order to the streamer list.
        all_s = self.sort_manager.get_sorted_streamers(site, all_s, live_since, last_live)

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
        self.safe_addstr(self.stdscr, header_y, x1 + 2, label_text,
                    curses.color_pair(self.C_CHROME) | curses.A_BOLD)

        # Status badge row
        badge_y = y1 + 1
        bx = x1 + 2
        self.safe_addstr(self.stdscr, badge_y, bx,
                    f"LIVE:{live_cnt}",  curses.color_pair(self.C_LIVE) | curses.A_BOLD)
        bx += 7
        self.safe_addstr(self.stdscr, badge_y, bx,
                    f"REC:{rec_cnt}",    curses.color_pair(self.C_REC) | curses.A_BOLD)
        bx += 6
        self.safe_addstr(self.stdscr, badge_y, bx,
                    f"OFF:{off_cnt}",    curses.color_pair(self.C_DIM))
        bx += 6
        if dis_cnt:
            self.safe_addstr(self.stdscr, badge_y, bx,
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
            if is_rec and recording_res.get(s) is not None:
                last_live_str = f"{recording_res.get(s)}p"
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
                        status_str  = "[► REC] "
                        status_attr = curses.color_pair(self.C_REC) | curses.A_BOLD
                else:
                    status_str  = "[●Live]"
                    status_attr = curses.color_pair(self.C_LIVE) | curses.A_BOLD
                bar_str     = _live_bar(elapsed, bar_w, _bar_max_secs)
                bar_attr    = curses.color_pair(self.C_LIVE)
                dur_str     = _fmt_duration(elapsed)
                if not (is_rec and recording_res.get(s) is not None):
                    last_live_str = ""  # currently live, no "last live"
            else:
                name_attr   = curses.color_pair(self.C_DIM)
                status_str  = "[○ off]"
                status_attr = curses.color_pair(self.C_DIM)
                bar_str     = "─" * bar_w
                bar_attr    = curses.color_pair(self.C_DIM)
                dur_str     = ""

            col = x1 + 2
            self.safe_addstr(self.stdscr, row_y, col,
                        s[:name_w].ljust(name_w), name_attr)
            col += name_w + 1
            self.safe_addstr(self.stdscr, row_y, col,
                        status_str[:7].ljust(7), status_attr)
            col += 8
            self.safe_addstr(self.stdscr, row_y, col, bar_str, bar_attr)
            col += bar_w + 1
            if dur_str:
                self.safe_addstr(self.stdscr, row_y, col,
                            dur_str[:9].ljust(9), curses.color_pair(self.C_CHROME))
            else:
                self.safe_addstr(self.stdscr, row_y, col, " " * 9, 0)
            col += 10
            if last_live_str:
                # Highlight in C_LIVE if streamer was live within LAST_LIVE_HIGHLIGHT days
                if (ll_ts is not None
                        and _last_live_highlight_days > 0
                        and (now - ll_ts) <= _last_live_highlight_days * 86400):
                    ll_attr = curses.color_pair(self.C_LIVE) | curses.A_BOLD
                else:
                    ll_attr = curses.color_pair(self.C_DIM)
                self.safe_addstr(self.stdscr, row_y, col,
                            last_live_str[:last_live_w],
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
        self.safe_addstr(self.stdscr, y1, x1, "  Site: ",
                    curses.color_pair(self.C_DIM))
        tab_x += 8
        for i, site in enumerate(self.sites):
            lbl = site.get_cached_config().get("site_label",
                              os.path.basename(site.config_path))
            label = f" {lbl} "
            attr  = (curses.color_pair(self.C_HILIGHT) | curses.A_BOLD
                     if i == self.selected_site_idx
                     else curses.color_pair(self.C_CHROME))
            self.safe_addstr(self.stdscr, y1, tab_x, label, attr)
            tab_x += len(label) + 1

        show_debug = sel_site.show_debug_log if sel_site is not None else False
        title = (" ACTIVITY LOG — Show Debug: ON  (Press A to toggle) "
                 if show_debug
                 else " ACTIVITY LOG — Show Debug: OFF (Press A to toggle) ")

        self.draw_box(self.stdscr, y1 + 1, x1, y2, x2, self.C_DIM)
        self.safe_addstr(self.stdscr, y1 + 1, x1 + 2, title,
                    curses.color_pair(self.C_DIM) | curses.A_BOLD)

        if sel_site is None:
            return

        visible_rows = (y2 - y1) - 3
        line_width   = max(1, (x2 - x1) - 4)   # 2 chars padding each side

        with sel_site.dash_lock:
            raw_lines = list(sel_site.dash_log_lines)

        # Filter or strip debug lines depending on the toggle
        if show_debug:
            display_lines = [
                (ln[len(_DEBUG_LOG_PREFIX):] if ln.startswith(_DEBUG_LOG_PREFIX) else ln)
                for ln in raw_lines
            ]
        else:
            display_lines = [ln for ln in raw_lines if not ln.startswith(_DEBUG_LOG_PREFIX)]

        wrapped = self._wrap_lines(display_lines, line_width)

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
            elif "Warning" in line:
                attr = curses.color_pair(self.C_WARN)
            self.safe_addstr(self.stdscr, y1 + 2 + i, x1 + 2, line, attr)

        # Scroll indicator
        if max_scroll > 0:
            scroll_info = f" ↑{self._log_scroll}/{max_scroll} " if self._log_scroll else " (end) "
            self.safe_addstr(self.stdscr, y1 + 1, x2 - len(scroll_info) - 1,
                        scroll_info, curses.color_pair(self.C_WARN))

    def _draw_pipe_tab(self, y1, x1, y2, x2, title: str, lines: List[str],
                       scroll: int = 0) -> int:
        """Draw a pipe-output tab. Returns the clamped scroll value."""
        sel_site = self.sites[self.selected_site_idx] if self.sites else None
        tab_x    = x1 + 1
        self.safe_addstr(self.stdscr, y1, x1, "  Site: ",
                    curses.color_pair(self.C_DIM))
        tab_x += 8
        for i, site in enumerate(self.sites):
            lbl = site.get_cached_config().get("site_label",
                              os.path.basename(site.config_path))
            label = f" {lbl} "
            attr  = (curses.color_pair(self.C_HILIGHT) | curses.A_BOLD
                     if i == self.selected_site_idx
                     else curses.color_pair(self.C_CHROME))
            self.safe_addstr(self.stdscr, y1, tab_x, label, attr)
            tab_x += len(label) + 1

        self.draw_box(self.stdscr, y1 + 1, x1, y2, x2, self.C_DIM)
        self.safe_addstr(self.stdscr, y1 + 1, x1 + 2, f" {title} ",
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
            self.safe_addstr(self.stdscr, y1 + 2 + i, x1 + 2, line,
                        curses.color_pair(self.C_DIM))

        # Scroll indicator
        if max_scroll > 0:
            scroll_info = f" ↑{scroll}/{max_scroll} " if scroll else " (end) "
            self.safe_addstr(self.stdscr, y1 + 1, x2 - len(scroll_info) - 1,
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
        self.draw_box(self.stdscr, y1, x1, y2, x2, self.C_CHROME)
        self.safe_addstr(self.stdscr, y1, x1 + 2, " TWITCH EVENTSUB ",
                    curses.color_pair(self.C_INVHEAD) | curses.A_BOLD)

        row_y = y1 + 2
        for site in self.sites:
            if row_y >= y2 - 1:
                break
            lbl = site.get_cached_config().get("site_label",
                              os.path.basename(site.config_path))
            self.safe_addstr(self.stdscr, row_y, x1 + 2, f"-- {lbl} --",
                        curses.color_pair(self.C_WARN) | curses.A_BOLD)
            row_y += 1

            es = site.eventsub_state
            if es is None:
                self.safe_addstr(self.stdscr, row_y, x1 + 4, "EventSub not available",
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
                self.safe_addstr(self.stdscr, row_y, x1 + 4,
                            f"{label:<16}", curses.color_pair(self.C_INVHEAD))
                self.safe_addstr(self.stdscr, row_y, x1 + 21, val, curses.color_pair(cpair))
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
            if action in ("disable", "remove"):
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
            elif current_tab == "Dashboard":
                sort_lbl = self.sort_manager.current_sort_label
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  A: add/enable streamer R: remove streamer D: disable streamer"
                         f"  S: Sort"
                         f"  C: colors  Q: quit  ")
            else:
                hints = (f"  LEFT/RIGHT: switch tabs"
                         f"  [: prev site  ]: next site"
                         f"  Tab: Next Panel"
                         f"  C: colors  Q: quit  ")
        self.safe_addstr(self.stdscr, h - 1, 0,
                    hints.ljust(w - 1)[:w - 1],
                    curses.color_pair(self.C_INVHEAD))

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

    def draw_mgmt_overlay(self):
        if not self._mgmt_mode:
            return
        h, w = self.stdscr.getmaxyx()
        action, site_idx = self._mgmt_mode
        site = self.sites[site_idx]
        site_lbl = site.get_cached_config().get("site_label",
                                                os.path.basename(site.config_path))

        box_h, box_w = min(20, h - 4), min(60, w - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        # Fill background
        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1),
                        curses.color_pair(self.C_NORMAL))

        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_WARN)
        title = f" {action.upper()} STREAMER "
        self.safe_addstr(self.stdscr, by1, bx1 + 2, title,
                    curses.color_pair(self.C_WARN) | curses.A_BOLD)
        self.safe_addstr(self.stdscr, by1 + 1, bx1 + 2,
                    f"Site: {site_lbl}", curses.color_pair(self.C_DIM))

        if action in ("disable", "remove"):
            # ── List-picker mode: arrow up/down to select a streamer ──────────
            enabled = self._mgmt_enabled_streamers(site)

            # Result message (shown after an action completes)
            if self._mgmt_result:
                self.safe_addstr(self.stdscr, by1 + 2, bx1 + 2,
                            self._mgmt_result[:box_w - 4],
                            curses.color_pair(self.C_LIVE) | curses.A_BOLD)

            if not enabled:
                self.safe_addstr(self.stdscr, by1 + 3, bx1 + 2,
                            "No enabled streamers.",
                            curses.color_pair(self.C_DIM))
                self.safe_addstr(self.stdscr, by2, bx1 + 2,
                            " Esc: Go back ",
                            curses.color_pair(self.C_INVHEAD))
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
                attr   = (curses.color_pair(self.C_HILIGHT) | curses.A_BOLD
                          if is_sel else curses.color_pair(self.C_NORMAL))
                self.safe_addstr(self.stdscr, row_y, bx1 + 2,
                            (prefix + s)[:box_w - 4], attr)

            self.safe_addstr(self.stdscr, by2, bx1 + 2,
                        " \u2191\u2193: select  Enter: confirm  Esc: Go back ",
                        curses.color_pair(self.C_INVHEAD))

        else:
            # ── ADD mode: disabled-streamer list + text input for new names ───
            disabled = self._mgmt_disabled_streamers(site)

            # Result message
            if self._mgmt_result:
                self.safe_addstr(self.stdscr, by1 + 2, bx1 + 2,
                            self._mgmt_result[:box_w - 4],
                            curses.color_pair(self.C_LIVE) | curses.A_BOLD)

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
                            curses.color_pair(self.C_CHROME))

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
                    attr   = (curses.color_pair(self.C_HILIGHT) | curses.A_BOLD
                              if is_sel else curses.color_pair(self.C_DIM))
                    self.safe_addstr(self.stdscr, row_y, bx1 + 2,
                                (prefix + s)[:box_w - 4], attr)
            else:
                self.safe_addstr(self.stdscr, list_top, bx1 + 2,
                            "No disabled streamers.",
                            curses.color_pair(self.C_DIM))

            # Text input (always shown at bottom)
            self.safe_addstr(self.stdscr, input_row, bx1 + 2, "New username:",
                        curses.color_pair(self.C_WARN) | curses.A_BOLD)
            input_attr = (curses.color_pair(self.C_HILIGHT) | curses.A_BOLD
                          if self._mgmt_sel == -1
                          else curses.color_pair(self.C_NORMAL) | curses.A_BOLD)
            self.safe_addstr(self.stdscr, input_row, bx1 + 16,
                        (self._mgmt_buf + "_")[:box_w - 18], input_attr)

            if disabled:
                legend = " \u2191\u2193: select disabled  Enter: add/enable  Esc: Go back "
            else:
                legend = " Enter: add  Esc: Go back "
            self.safe_addstr(self.stdscr, legend_row, bx1 + 2,
                        legend[:box_w - 4],
                        curses.color_pair(self.C_INVHEAD))

    # ── Full screen refresh ───────────────────────────────────────────────────
    def refresh_screen(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        self.stdscr.bkgd(" ", curses.color_pair(self.C_NORMAL))

        # Logo (6 lines tall, starts at row 1)
        self.draw_logo(1, 2)

        # System time top-right
        sys_time_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        self.safe_addstr(self.stdscr, 1, w - len(sys_time_str) - 3, sys_time_str,
                    curses.color_pair(self.C_CHROME))

        # Track the next available row on the right side
        next_right_row = 2

        # Update Available indicator (below system time)
        with update_available_lock:
            if UPDATE_AVAILABLE:
                update_str = "Update Available"
                self.safe_addstr(self.stdscr, next_right_row, w - len(update_str) - 3, update_str,
                            curses.color_pair(self.C_WARN) | curses.A_BOLD)
                next_right_row += 1
        
        # App version indicator (Below Update Available, or directly below time)
        version_str = f"v{__version__}"
        self.safe_addstr(self.stdscr, next_right_row, w - len(version_str) - 3, version_str,
                    curses.color_pair(self.C_DIM))

        # Blank line after logo (row 7), then tab bar at row 8
        # (Logo occupies rows 1-6, row 7 is blank, tabs at row 8)
        self.draw_tabs(8, 2)

        # Separator
        self.safe_addstr(self.stdscr, 9, 1, "-" * (w - 2), curses.color_pair(self.C_CHROME))

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

        # Sort popup — drawn on top of everything else.
        if self.sort_manager.popup_open:
            self.sort_manager.draw_popup(self.stdscr)

        # Changelog popup — drawn on top of sort popup if both somehow open.
        if self._changelog_popup_open:
            self.draw_changelog_popup()

        # Exit-confirmation popup — drawn on top of everything else.
        if self._exit_confirm_open:
            self.draw_exit_confirm_popup()

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
        # Exit-confirmation popup intercepts all keys while open.
        if self._exit_confirm_open:
            return self._handle_exit_confirm_key(key)

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

        if self._mgmt_mode:
            return self._handle_mgmt_key(key)

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

        if key in (ord('q'), ord('Q'), 27):
            self._open_exit_confirm()
        elif key in (curses.KEY_RIGHT, ord('l')):
            self.selected_tab = (self.selected_tab + 1) % len(self.TABS)
        elif key in (curses.KEY_LEFT, ord('h')):
            self.selected_tab = (self.selected_tab - 1) % len(self.TABS)
        elif key in (ord(']'), curses.KEY_NPAGE):   # next site (log/config tabs)
            self.selected_site_idx = (self.selected_site_idx + 1) % max(1, len(self.sites))
            # Reset scroll when switching sites
            self._log_scroll = self._stdout_scroll = self._stderr_scroll = 0
            self.config_editor.notify_site_changed(self.selected_site_idx)
        elif key in (ord('['), curses.KEY_PPAGE):   # prev site
            self.selected_site_idx = (self.selected_site_idx - 1) % max(1, len(self.sites))
            # Reset scroll when switching sites
            self._log_scroll = self._stdout_scroll = self._stderr_scroll = 0
            self.config_editor.notify_site_changed(self.selected_site_idx)
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
            if current_tab_name == "Log" and self.sites:
                sel = self.sites[self.selected_site_idx]
                sel.show_debug_log = not sel.show_debug_log
                self._log_scroll = 0
            elif current_tab_name == "Stdout" and self.sites:
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
        elif key in (ord('s'), ord('S')):
            if current_tab_name == "Dashboard":
                self.sort_manager.open_popup()
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

    def _handle_mgmt_key(self, key) -> bool:
        action, site_idx = self._mgmt_mode
        site = self.sites[site_idx]

        if action in ("disable", "remove"):
            # ── List-picker mode ───────────────────────────────────────────────
            enabled = self._mgmt_enabled_streamers(site)
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
                    site.invalidate_config_cache()
                    self.config_editor.load_config(site.config_path)
                    self.config_editor.priority_editor.force_reload()
                    site.trigger_event.set()
                    self._mgmt_result = result
                    # Keep selection clamped to the (now shorter) list
                    new_enabled = self._mgmt_enabled_streamers(site)
                    self._mgmt_sel = min(self._mgmt_sel, max(0, len(new_enabled) - 1))
                else:
                    self._mgmt_mode   = None
                    self._mgmt_buf    = ""
                    self._mgmt_result = ""
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
                    result = _modify_config_streamer(site.config_path,
                                                     self._mgmt_buf.strip(), "add")
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
        global FFMPEG_ERROR_RESTART_THRESHOLD
        _new_thresh_raw = new_cfg.get("FF_ERR_THRESH", "200").strip()
        try:
            _new_thresh = int(_new_thresh_raw)
            if _new_thresh >= 0:
                FFMPEG_ERROR_RESTART_THRESHOLD = _new_thresh
                _logger.dbg(
                    f"[CONFIG] apply_global_cfg: FFMPEG_ERROR_RESTART_THRESHOLD "
                    f"updated to {_new_thresh}"
                )
        except (ValueError, TypeError):
            _logger.dbg(
                f"[CONFIG] apply_global_cfg: invalid FF_ERR_THRESH value "
                f"{_new_thresh_raw!r} — keeping current threshold"
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
        with update_available_lock:
            update_av = UPDATE_AVAILABLE
        if update_av:
            return False
        with _global_json_lock:
            gd = _load_global_json()
        # Key missing (fresh install / manual update / global.json reset) OR
        # explicitly False → show the changelog.
        return gd.get("changelog_shown") is not True

    def _mark_changelog_shown(self) -> None:
        """Persist changelog_shown=True immediately when the popup opens."""
        with _global_json_lock:
            gd = _load_global_json()
            gd["changelog_shown"] = True
            _save_global_json(gd)
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
            return [f"Error reading changelog: {e}"]

    def open_changelog_popup(self) -> None:
        """Open the changelog popup, load content, and persist changelog_shown=True."""
        self._changelog_lines = self._load_changelog_lines()
        self._changelog_scroll = 0
        self._changelog_popup_open = True
        self._mark_changelog_shown()

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
        legend  = " \u2190/\u2192: Select  Enter: Confirm  Esc: Exit "
        box_w = min(max(len(message) + 6, len(legend) + 4, 34), w - 4)
        box_h = 5
        by1 = max(0, (h - box_h) // 2)
        bx1 = max(0, (w - box_w) // 2)
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        # Fill background
        for y in range(by1, by2 + 1):
            self.safe_addstr(self.stdscr, y, bx1, " " * (box_w + 1),
                        curses.color_pair(self.C_NORMAL))

        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_WARN)
        title = " CONFIRM EXIT "
        self.safe_addstr(self.stdscr, by1, bx1 + 2, title,
                    curses.color_pair(self.C_WARN) | curses.A_BOLD)

        self.safe_addstr(self.stdscr, by1 + 2, bx1 + max(0, (box_w - len(message)) // 2),
                    message, curses.color_pair(self.C_NORMAL) | curses.A_BOLD)

        yes_label = " Yes "
        no_label  = " No "
        gap = 4
        buttons_w = len(yes_label) + len(no_label) + gap
        start_x = bx1 + max(0, (box_w - buttons_w) // 2)
        yes_attr = (curses.color_pair(self.C_HILIGHT) | curses.A_BOLD) if self._exit_confirm_sel == 0 \
                   else curses.color_pair(self.C_NORMAL)
        no_attr  = (curses.color_pair(self.C_HILIGHT) | curses.A_BOLD) if self._exit_confirm_sel == 1 \
                   else curses.color_pair(self.C_NORMAL)
        self.safe_addstr(self.stdscr, by1 + 3, start_x, yes_label, yes_attr)
        self.safe_addstr(self.stdscr, by1 + 3, start_x + len(yes_label) + gap, no_label, no_attr)

        self.safe_addstr(self.stdscr, by2, bx1 + max(0, (box_w - len(legend)) // 2),
                    legend[:max(0, box_w - 2)],
                    curses.color_pair(self.C_INVHEAD))

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
                        curses.color_pair(self.C_NORMAL))

        self.draw_box(self.stdscr, by1, bx1, by2, bx2, self.C_CHROME)
        title = " WHAT'S NEW "
        self.safe_addstr(self.stdscr, by1, bx1 + 2, title,
                    curses.color_pair(self.C_HILIGHT) | curses.A_BOLD)

        content_width = max(1, box_w - 4)
        wrapped = self._wrap_lines(self._changelog_lines, content_width)

        visible_rows = box_h - 3   # top border + title row + bottom legend row
        max_scroll   = max(0, len(wrapped) - visible_rows)
        self._changelog_scroll = min(self._changelog_scroll, max_scroll)

        start = self._changelog_scroll
        view  = wrapped[start : start + visible_rows]

        for i, line in enumerate(view):
            self.safe_addstr(self.stdscr, by1 + 1 + i, bx1 + 2, line,
                        curses.color_pair(self.C_NORMAL))

        # Scroll indicator
        if max_scroll > 0:
            pct = int(100 * self._changelog_scroll / max_scroll)
            scroll_info = f" ↑↓/PgUp/PgDn  {pct}% "
        else:
            scroll_info = " (all) "
        legend = f" Q/Esc: close {scroll_info}"
        self.safe_addstr(self.stdscr, by2, bx1 + 2,
                    legend[:box_w - 4],
                    curses.color_pair(self.C_INVHEAD))

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
                if not self.handle_key(key):
                    should_quit = True
                    break
            if should_quit:
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
            JJDlpDashboard.safe_addstr(stdscr, 1 + i, 2, line, curses.color_pair(6) | curses.A_BOLD)

        ts = time.strftime("%Y-%m-%d  %H:%M:%S")
        JJDlpDashboard.safe_addstr(stdscr, 1, w - len(ts) - 3, ts, curses.color_pair(1))
        JJDlpDashboard.safe_addstr(stdscr, 7, 2, "-" * (w - 4), curses.color_pair(1))

        # Title
        title = "SELECT CONFIG FILE(S)"
        JJDlpDashboard.safe_addstr(stdscr, 9, 2, title, curses.color_pair(5) | curses.A_BOLD)

        # Instructions
        JJDlpDashboard.safe_addstr(stdscr, 10, 2,
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
            JJDlpDashboard.safe_addstr(stdscr, row, 4, f"  {checked}  {name}", attr)

        # "Do not show again" checkbox
        dna_row = 12 + n + 1
        dna_box = "[x]" if do_not_show_config else "[ ]"
        dna_attr = curses.color_pair(3) | curses.A_BOLD if do_not_show_config else curses.color_pair(3)
        JJDlpDashboard.safe_addstr(stdscr, dna_row, 4,
                    f"  {dna_box}  Do not show again (press D to toggle)",
                    dna_attr)

        # Footer
        sel_count = len(selected)
        footer = (f"  {sel_count} file(s) selected  "
                  f"↑/↓ navigate  Space toggle  Enter confirm  D do not show  ")
        JJDlpDashboard.safe_addstr(stdscr, h - 1, 0,
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
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
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
            JJDlpDashboard.safe_addstr(stdscr, 1 + i, 2, line, curses.color_pair(6) | curses.A_BOLD)

        ts = time.strftime("%Y-%m-%d  %H:%M:%S")
        JJDlpDashboard.safe_addstr(stdscr, 1, w - len(ts) - 3, ts, curses.color_pair(1))
        JJDlpDashboard.safe_addstr(stdscr, 7, 2, "-" * (w - 4), curses.color_pair(1))

        # Browser sub-title
        br_title_row = 9
        JJDlpDashboard.safe_addstr(stdscr, br_title_row, 2,
                    "SELECT BROWSER",
                    curses.color_pair(5) | curses.A_BOLD)
        JJDlpDashboard.safe_addstr(stdscr, br_title_row + 1, 2,
                    "Select your browser for the yt-dlp --cookies-from-browser option.",
                    curses.color_pair(3))
        JJDlpDashboard.safe_addstr(stdscr, br_title_row + 2, 2,
                    "Note: Chrome based browsers are not supported. Firefox is recommended.",
                    curses.color_pair(3))
        applies_to_labels = [
            file_cfgs[fname].get("site_label")
            for fname in chosen_files
            if file_cfgs[fname].get("downloader_cookies", True) or file_cfgs[fname].get("checker_cookies", False)
        ]
        JJDlpDashboard.safe_addstr(stdscr, br_title_row + 4, 2,
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
            JJDlpDashboard.safe_addstr(stdscr, row, 4, label, attr)

        # "Do not show again" checkbox (below the browser list)
        dna_row  = list_start_row + nb + 1
        dna_box  = "[x]" if do_not_show else "[ ]"
        dna_attr = curses.color_pair(3) | curses.A_BOLD if do_not_show else curses.color_pair(3)
        JJDlpDashboard.safe_addstr(stdscr, dna_row, 4,
                    f"  {dna_box}  Do not show again (press D to toggle)",
                    dna_attr)

        # Footer
        footer = "  ↑/↓ navigate  Enter = confirm  D = do not show again  Q = quit  "
        JJDlpDashboard.safe_addstr(stdscr, h - 1, 0,
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
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
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

            config_paths = [os.path.join(cwd, f) for f in chosen]

    # ASK_FOR_BROWSER logic
    _global_cfg = load_global_config()
    ask_for_browser = _global_cfg.get("ask_for_browser", None)
    if ask_for_browser is None:
        # Fall back to per-site values for backwards compatibility
        ask_for_browser = any(
            load_config(p).get("ask_for_browser", True)
            for p in config_paths
        )

    if ask_for_browser:
        curses.wrapper(_curses_choose_browser, config_paths)

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
            # Reset changelog_shown so it will display after the update is applied.
            with _global_json_lock:
                _gd = _load_global_json()
                if _gd.get("changelog_shown") is not False:
                    _gd["changelog_shown"] = False
                    _save_global_json(_gd)
                    dbg("[UPDATER] startup: update available — changelog_shown set to false")
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
                # When an update becomes newly available, reset changelog_shown so it will
                # display to the user after the update is applied.
                if new_available and not prev_available:
                    with _global_json_lock:
                        _gd = _load_global_json()
                        _gd["changelog_shown"] = False
                        _save_global_json(_gd)
                    dbg("[UPDATER] periodic: update newly available — changelog_shown set to false")
                # When an update becomes available while the dashboard is active,
                # only use the dashboard indicator and do not prompt interactively.
                time.sleep(update_interval * 60)

        threading.Thread(target=_periodic_update_checker, daemon=True).start()

    from . import logger as _logger
    # DEBUG_LOGS / DEBUG_LOG_PATH are now global settings.
    any_debug = global_cfg.get("debug_logs", False)
    debug_path = ""
    if any_debug:
        raw_path = global_cfg.get("debug_log_path", "")
        debug_path = raw_path if raw_path else get_debug_log_path(load_config(config_paths[0]))
    _configure_debug_log(enabled=any_debug, path=debug_path)

    # ── Apply FF_ERR_THRESH from global config ────────────────────────────────
    global FFMPEG_ERROR_RESTART_THRESHOLD
    _startup_thresh = global_cfg.get("ff_err_thresh", 200)
    if _startup_thresh >= 0:
        FFMPEG_ERROR_RESTART_THRESHOLD = _startup_thresh
        
    # ── Launch per-site state + threads ──────────────────────────────────────
    global _global_sites
    sites: List[SiteState] = []
    _global_sites = sites
    for cp in config_paths:
        site = SiteState(cp)
        sites.append(site)

    def _dash_log(msg: str):
        for s in sites:
            s.log_line(msg)

    def _dash_dbg(msg: str):
        """Route a dbg() line to every site's log buffer with the debug prefix."""
        for s in sites:
            prefixed = _DEBUG_LOG_PREFIX + msg
            with s.dash_lock:
                s.dash_log_lines.append(prefixed)
                if len(s.dash_log_lines) > 200:
                    s.dash_log_lines = s.dash_log_lines[-200:]

    _logger.configure(_dash_log, _dash_dbg)

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

        curses.wrapper(_run_dashboard)

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
            # Join against a single shared deadline rather than giving each
            # thread its own 15s timeout — otherwise N active recordings
            # could take up to 15*N seconds to shut down instead of ~15s.
            deadline = time.time() + 15
            for t in active:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                t.join(timeout=remaining)
        print("✓  All done. Goodbye!\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as _top_e:
        log_crash(_top_e)
        raise
