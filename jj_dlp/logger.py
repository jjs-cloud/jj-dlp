"""
logger.py  —  logging & debug helpers for jj-dlp

Globals (set once at startup from config, then read-only):
    ENABLE_STARTUP_LOG   bool  — write jj-dlp-startup-debug.log
    ENABLE_CRASH_LOG     bool  — write jj-dlp-crash.log on unhandled exception

Runtime debug-log state is held in a single ``DebugLogConfig`` instance
(_debug_cfg) that is updated atomically under _debug_cfg_lock via
configure_debug_log().  All other modules interact with this state only
through that function; the old bare globals (DEBUG_LOGS_ENABLED,
DEBUG_LOG_PATH, debug_log_lock) no longer exist.

Public API
----------
startup_dbg(msg)                   Write a line to the startup log (if enabled).
startup_dbg_flush()                Write the opening banner (argv, cwd, python path).
dbg(msg)                           Write to debug log (filtered by DBG_FILTERS).
log_crash(e)                       Write an unhandled exception to jj-dlp-crash.log.
configure_debug_log(enabled, path) Atomically update the debug-log config.
get_debug_log_config()             Return current (enabled, path) debug-log state.
get_dbg_filters()                  Return a snapshot copy of DBG_FILTERS {tag: bool}.
set_dbg_filter(tag, enabled)       Atomically update one tag in DBG_FILTERS.
load_dbg_filters(overrides)        Batch-apply saved tag states (startup restore).
configure(output_mode_fn, ...)     Inject output-mode accessor, dashboard logger, and
                                   optional per-line debug callback for the Log tab.
get_debug_log_path(cfg)            Resolve the debug log path from a config dict.
get_log_path(cfg)                  Resolve the activity log path from a config dict.
get_log_file_paths(cfg)            Return (stdout_path, stderr_path) for yt-dlp logging.
"""

import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime


# ── Startup / crash log flags ─────────────────────────────────────────────────
ENABLE_STARTUP_LOG: bool = False
ENABLE_CRASH_LOG:   bool = True

_ROOT_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_STARTUP_LOG: str = os.path.join(_ROOT_DIR, "jj-dlp-startup-debug.log")
_CRASH_LOG:   str = os.path.join(_ROOT_DIR, "jj-dlp-crash.log")

# ── Runtime debug log config ──────────────────────────────────────────────────

@dataclass
class DebugLogConfig:
    """Holds the mutable runtime state for the debug log.

    A single module-level instance (_debug_cfg) is updated atomically under
    _debug_cfg_lock via configure_debug_log().  No other code should mutate
    these fields directly.
    """
    enabled: bool = False
    path:    str  = ""

_debug_cfg:      DebugLogConfig = DebugLogConfig()
_debug_cfg_lock: threading.Lock = threading.Lock()


def configure_debug_log(enabled: bool, path: str = "") -> None:
    """Atomically update the debug-log enabled flag and file path.

    This is the sole write path for debug-log configuration.  Call once
    from main() after the global config has been loaded.
    """
    with _debug_cfg_lock:
        _debug_cfg.enabled = enabled
        _debug_cfg.path    = path


def get_debug_log_config() -> tuple[bool, str]:
    """Return the current ``(enabled, path)`` debug-log state.

    Use this instead of reaching into the private ``_debug_cfg`` /
    ``_debug_cfg_lock`` internals from outside this module.
    """
    with _debug_cfg_lock:
        return _debug_cfg.enabled, _debug_cfg.path


def get_dbg_filters() -> dict[str, bool]:
    """Return a snapshot copy of the current DBG_FILTERS state.

    Call this to read tag states without touching module internals directly.
    Insertion order (Python 3.7+) is preserved so callers get a stable list.
    """
    with _dbg_filters_lock:
        return dict(DBG_FILTERS)


def set_dbg_filter(tag: str, enabled: bool) -> None:
    """Atomically update a single tag in DBG_FILTERS.

    Unknown tags are silently ignored so stale callers can't introduce
    phantom filter keys.
    """
    with _dbg_filters_lock:
        if tag in DBG_FILTERS:
            DBG_FILTERS[tag] = bool(enabled)


def load_dbg_filters(overrides: dict) -> None:
    """Batch-apply saved tag states from a config / JSON dict.

    Only keys already present in DBG_FILTERS are updated; unknown keys in
    *overrides* are ignored so stale saved data can't introduce phantom tags.
    Call this at startup after loading global.json to restore persisted states.
    """
    with _dbg_filters_lock:
        for tag, val in overrides.items():
            if tag in DBG_FILTERS:
                DBG_FILTERS[tag] = bool(val)

# ── References to output-mode state (injected by main module at startup) ──────
# These are set by jj-dlp.py via configure() so logger doesn't import main.
_output_mode_ref   = None   # callable() -> int  (1=curses, 2=terminal)
_dashboard_log_ref = None   # callable(str) -> None  (debug-log write errors)
_dashboard_dbg_ref = None   # callable(str) -> None  (every dbg() line that passes filters)


def configure(output_mode_fn, dashboard_log_fn=None, dashboard_dbg_fn=None) -> None:
    """
    Inject accessor for OUTPUT_MODE and optional dashboard callbacks.

    dashboard_log_fn  – called with a string when a debug-log write error
                        occurs (existing behaviour).
    dashboard_dbg_fn  – called with every dbg() line that passes the tag
                        filter, so the Log tab can optionally display it.
    Call once from jj-dlp.py after the globals are defined there.
    """
    global _output_mode_ref, _dashboard_log_ref, _dashboard_dbg_ref
    _output_mode_ref = output_mode_fn
    if dashboard_log_fn is not None:
        _dashboard_log_ref = dashboard_log_fn
    if dashboard_dbg_fn is not None:
        _dashboard_dbg_ref = dashboard_dbg_fn


# ── Per-tag debug filter ───────────────────────────────────────────────────────
# Controls which [TAG] groups appear in the debug log.
# Keys must match the bracketed tag at the start of each dbg() message exactly.
# Set a tag to False to silence all dbg() calls that begin with [TAG].
# Set to True to allow them through (subject to DEBUG_LOGS_ENABLED being on).
#
# Tags used in jj-dlp:
#   DRAIN    — yt-dlp stdout/stderr pipe drain threads
#   CHECKER  — liveness-check subprocess calls
#   SPLIT    — split-recording file-tracking logic
#   POPEN    — yt-dlp process launch details
#   PERF     — performance timing summaries (high-frequency)
#   DISK     — disk usage display in the system panel
#   UPDATER  — update checker and periodic updater thread
#   TWITCH   — twitch eventsub and token operations
#   KILL     — yt-dlp process termination
#   CONFIG   — config editor save/backup operations
#   POPUP    — live popup notification creation and suppression
#
DBG_FILTERS: dict[str, bool] = {
    "DRAIN":   False,
    "CHECKER": False,
    "SPLIT":   False,
    "POPEN":   False,
    "PERF":    False,
    "DISK":    False,
    "UPDATER": False,
    "TWITCH":  False,
    "CONFIG":  False,
    "KILL":    True,
    "STALL":   True,
    "POPUP":   False,
}

_dbg_filters_lock = threading.Lock()


# ── Startup log ───────────────────────────────────────────────────────────────

def startup_dbg(msg: str) -> None:
    """Append a timestamped line to the startup debug log (no-op if disabled)."""
    if not ENABLE_STARTUP_LOG:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_STARTUP_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def startup_dbg_flush() -> None:
    """Write the opening banner to the startup log."""
    startup_dbg("=" * 60)
    startup_dbg(f"NEW RUN  argv={sys.argv}")
    startup_dbg(f"cwd      = {os.getcwd()}")
    startup_dbg(f"__file__ = {os.path.abspath(__file__)}")
    startup_dbg(f"python   = {sys.executable}")


# ── Runtime debug log ─────────────────────────────────────────────────────────

_last_debug_err = ""

def _write_debug_log(msg: str) -> None:
    global _last_debug_err
    with _debug_cfg_lock:
        enabled  = _debug_cfg.enabled
        path     = _debug_cfg.path
        last_err = _last_debug_err
    if not enabled or not path:
        return
    try:
        dir_part = os.path.dirname(path)
        if dir_part:
            os.makedirs(dir_part, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        with _debug_cfg_lock:
            _last_debug_err = ""
    except Exception as e:
        err_msg = f"DEBUG LOG ERROR: Could not write to {path}: {e}"
        if _dashboard_log_ref and err_msg != last_err:
            _dashboard_log_ref(err_msg)
            with _debug_cfg_lock:
                _last_debug_err = err_msg


def dbg(msg: str, site_name: str = "") -> None:
    """
    Write msg (with timestamp and optional site name) to the debug log.

    The message is dropped silently if its leading [TAG] appears in
    DBG_FILTERS with a value of False.  Messages with no recognisable
    [TAG] are always written.
    """
    # ── Tag-based filter ──────────────────────────────────────────────────────
    # Extract the first [TAG] token from the message, e.g. "[DRAIN]" -> "DRAIN".
    # A compound tag like "[SPLIT][wait_for_streamer_file]" uses only the first
    # bracket group as the filter key so a single switch covers the whole group.
    if msg.startswith("["):
        end = msg.find("]")
        if end > 1:
            tag = msg[1:end]
            with _dbg_filters_lock:
                allowed = DBG_FILTERS.get(tag, False)   # unknown tags are dropped
            if not allowed:
                return

    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Prepend site name if provided
    prefix = f"[{site_name}] " if site_name else ""
    full = f"[{ts}] {prefix}{msg}"
    
    _write_debug_log(full)

    # Route to the dashboard Log tab (filtered lines only; caller opted in via
    # configure(dashboard_dbg_fn=...)).  Runs regardless of whether file
    # logging is enabled so the Log tab toggle works even without a debug file.
    if _dashboard_dbg_ref is not None:
        try:
            _dashboard_dbg_ref(full)
        except Exception:
            pass


# ── Crash log ─────────────────────────────────────────────────────────────────

def log_crash(e: Exception) -> None:
    """
    Log an unhandled exception to both the startup log and the crash log.
    """
    import traceback
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Log to startup log (always good for context)
    startup_dbg(f"UNCAUGHT EXCEPTION: {type(e).__name__}: {e}")
    startup_dbg(traceback.format_exc())

    # 2. Log to crash log (the user-visible artifact)
    if not ENABLE_CRASH_LOG:
        return

    try:
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\nCRASH at {ts}\n")
            f.write(traceback.format_exc())
    except Exception:
        pass


# ── Log-path helpers ──────────────────────────────────────────────────────────

def get_debug_log_path(cfg: dict) -> str:
    """Return the resolved debug log path for a given config dict."""
    p = cfg.get("debug_log_path") or ""
    if p.strip():
        return p
    return os.path.join(cfg.get("output_dir", "."), "debug.log")


def get_log_path(cfg: dict) -> str:
    """Return the resolved activity log path for a given config dict."""
    lp = cfg.get("log_path") or ""
    if lp.strip():
        return lp
    return os.path.join(cfg.get("output_dir", "."), "jj-dlp.log")


def get_log_file_paths(cfg: dict) -> tuple:
    """
    Return (stdout_log_path, stderr_log_path).
    When split_logs is False both paths are the same file.
    """
    base = get_log_path(cfg)
    if cfg.get("split_logs"):
        return f"{base}.stdout.log", f"{base}.stderr.log"
    return base, base



