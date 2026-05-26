"""
logger.py  —  logging & debug helpers for jj-dlp

Globals (set once at startup from config, then read-only):
    ENABLE_STARTUP_LOG   bool  — write jj-dlp-startup-debug.log
    ENABLE_CRASH_LOG     bool  — write jj-dlp-crash.log on unhandled exception

Runtime debug-log state is held in a single ``DebugLogConfig`` instance
(_debug_cfg) that is swapped atomically under its own lock.  Callers that
previously imported ``DEBUG_LOGS_ENABLED``, ``DEBUG_LOG_PATH``, or
``debug_log_lock`` directly can continue to do so — module-level properties
forward to the object — but the preferred API is:

    configure_debug_log(enabled=True, path="/some/debug.log")

Public API
----------
startup_dbg(msg)                  Write a line to the startup log (if enabled).
startup_dbg_flush()               Write the opening banner (argv, cwd, python path).
dbg(msg)                          Write to debug log (filtered by DBG_FILTERS).
log_crash(e)                      Write an unhandled exception to jj-dlp-crash.log.
configure_debug_log(enabled, path) Atomically update the debug-log config.
get_debug_log_path(cfg)           Resolve the debug log path from a config dict.
get_log_path(cfg)                 Resolve the activity log path from a config dict.
get_log_file_paths(cfg)           Return (stdout_path, stderr_path) for yt-dlp logging.
configure_filters(d)              Replace DBG_FILTERS with a new tag→bool dict.
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

# ── Runtime debug log config (replaces bare module-level mutable globals) ─────

@dataclass
class DebugLogConfig:
    """Holds the mutable runtime state for the debug log.

    A single module-level instance (_debug_cfg) is swapped atomically under
    _debug_cfg_lock via configure_debug_log().  This confines all mutation to
    one place and makes the shared state explicit and thread-safe.
    """
    enabled: bool = False
    path:    str  = ""

_debug_cfg:      DebugLogConfig = DebugLogConfig()
_debug_cfg_lock: threading.Lock = threading.Lock()


def configure_debug_log(enabled: bool, path: str = "") -> None:
    """Atomically update the debug-log enabled flag and file path.

    Replaces the previous pattern of setting ``logger.DEBUG_LOGS_ENABLED``
    and ``logger.DEBUG_LOG_PATH`` under ``logger.debug_log_lock`` directly.
    """
    with _debug_cfg_lock:
        _debug_cfg.enabled = enabled
        _debug_cfg.path    = path


# ── Backward-compatible shims ─────────────────────────────────────────────────
# Code that imported DEBUG_LOGS_ENABLED / DEBUG_LOG_PATH / debug_log_lock
# from this module continues to work unchanged.  Direct mutation via these
# names (e.g. ``_logger.DEBUG_LOGS_ENABLED = True``) is still supported but
# deprecated; prefer configure_debug_log().

class _DebugLogProxy:
    """Module-level descriptor proxy so attribute access forwards to _debug_cfg."""

    # Expose the underlying lock for callers that acquired it directly.
    @property
    def debug_log_lock(self) -> threading.Lock:          # noqa: D401
        return _debug_cfg_lock

    @property
    def DEBUG_LOGS_ENABLED(self) -> bool:
        return _debug_cfg.enabled

    @DEBUG_LOGS_ENABLED.setter
    def DEBUG_LOGS_ENABLED(self, value: bool) -> None:
        with _debug_cfg_lock:
            _debug_cfg.enabled = value

    @property
    def DEBUG_LOG_PATH(self) -> str:
        return _debug_cfg.path

    @DEBUG_LOG_PATH.setter
    def DEBUG_LOG_PATH(self, value: str) -> None:
        with _debug_cfg_lock:
            _debug_cfg.path = value


import sys as _sys
import types as _types

class _LoggerModule(_types.ModuleType):
    """Wraps this module to expose DEBUG_LOGS_ENABLED / DEBUG_LOG_PATH as
    module-level properties that forward to the DebugLogConfig instance."""

    _proxy = _DebugLogProxy()

    def __getattr__(self, name: str):
        if name in ("DEBUG_LOGS_ENABLED", "DEBUG_LOG_PATH", "debug_log_lock"):
            return getattr(self._proxy, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        if name in ("DEBUG_LOGS_ENABLED", "DEBUG_LOG_PATH"):
            setattr(self._proxy, name, value)
        else:
            super().__setattr__(name, value)


# Make the lock importable as a module attribute directly (read-only via proxy.
debug_log_lock = _debug_cfg_lock

# ── References to output-mode state (injected by main module at startup) ──────
# These are set by jj-dlp.py via configure() so logger doesn't import main.
_output_mode_ref  = None   # callable() -> int  (1=curses, 2=terminal)
_dashboard_log_ref = None  # callable(str) -> None


def configure(output_mode_fn, dashboard_log_fn=None) -> None:
    """
    Inject accessor for OUTPUT_MODE and an optional dashboard logger.
    Call once from jj-dlp.py after the globals are defined there.
    """
    global _output_mode_ref, _dashboard_log_ref
    _output_mode_ref = output_mode_fn
    if dashboard_log_fn is not None:
        _dashboard_log_ref = dashboard_log_fn


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
    "CONFIG":  True,
    "KILL":    False,
    "POPUP":   False,
    "RUN":     True,
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
        enabled = _debug_cfg.enabled
        path    = _debug_cfg.path
    if not enabled or not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        _last_debug_err = ""
    except Exception as e:
        err_msg = f"DEBUG LOG ERROR: Could not write to {path}: {e}"
        if _dashboard_log_ref and err_msg != _last_debug_err:
            _dashboard_log_ref(err_msg)
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


# Replace this module's entry in sys.modules with the proxy subclass so that
# attribute access on the module object goes through __getattr__/__setattr__.
_current = _sys.modules[__name__]
_wrapper = _LoggerModule(__name__, __doc__)
_wrapper.__dict__.update({
    k: v for k, v in _current.__dict__.items()
    if k not in ("DEBUG_LOGS_ENABLED", "DEBUG_LOG_PATH", "debug_log_lock")
})
_sys.modules[__name__] = _wrapper
