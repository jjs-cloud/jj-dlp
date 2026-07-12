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
dbg(msg)                           Write to debug log (filtered by DBG_FILTERS, then by
                                    any per-message overrides for that tag).
log_dashboard_line(msg)             Mirror a Log-tab line into the debug log
                                    (unfiltered; only gated on enabled/path).
log_crash(e)                       Write an unhandled exception to jj-dlp-crash.log.
configure_debug_log(enabled, path) Atomically update the debug-log config.
get_debug_log_config()             Return current (enabled, path) debug-log state.
get_dbg_filters()                  Return a snapshot copy of the current tag states.
rescan_dbg_call_sites()            (Re)scan the package's .py files for dbg() call
                                    sites, grouped by their leading [TAG].
get_dbg_call_sites(tag)            Return [(callsite_id, label), ...] for a tag.
get_dbg_message_overrides(tag)     Return the saved per-message overrides for a tag.
configure(dashboard_log_fn, ...)   Inject dashboard logger and optional per-line debug
                                   optional per-line debug callback for the Log tab.
get_debug_log_path(cfg)            Resolve the debug log path from a config dict.
get_log_path(cfg)                  Resolve the activity log path from a config dict.
get_log_file_paths(cfg)            Return (stdout_path, stderr_path) for yt-dlp logging.
"""

import os
import re
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
    active = _get_active_tags()
    return {tag: bool(active.get(tag, False)) for tag in DBG_TAGS}

# ── References to dashboard callbacks (injected by main module at startup) ────
# These are set by jj-dlp.py via configure() so logger doesn't import main.
_dashboard_log_ref = None   # callable(str) -> None  (debug-log write errors)
_dashboard_dbg_ref = None   # callable(str) -> None  (every dbg() line that passes filters)


def configure(dashboard_log_fn=None, dashboard_dbg_fn=None) -> None:
    """
    Inject optional dashboard callbacks.

    dashboard_log_fn  – called with a string when a debug-log write error
                        occurs (existing behaviour).
    dashboard_dbg_fn  – called with every dbg() line that passes the tag
                        filter, so the Log tab can optionally display it.
    Call once from jj-dlp.py after sites are set up.
    """
    global _dashboard_log_ref, _dashboard_dbg_ref
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
#   LQ       — low-quality/bandwidth-saving downloader logic
#   SCHEDULE — per-streamer schedule evaluation on every check interval
#   GLOBAL_JSON — global.json read/write/backup diagnostics
#
DBG_TAGS: list[str] = [
    "DRAIN",
    "CHECKER",
    "SPLIT",
    "POPEN",
    "PERF",
    "DISK",
    "UPDATER",
    "TWITCH",
    "AD",
    "CONFIG",
    "KILL",
    "STALL",
    "POPUP",
    "LQ",
    "SCHEDULE",
    "UPGRADE_QUALITY",
    "GLOBAL_JSON",
]

import json
import time

_json_cache: dict = {}
_json_cache_mtime: float = 0.0
_json_cache_lock = threading.Lock()

# global.json lives inside the jj_dlp/ package directory (same dir as this file)
_PKG_DIR: str = os.path.dirname(os.path.abspath(__file__))
_GLOBAL_JSON_PATH: str = os.path.join(_PKG_DIR, "global.json")


def _get_global_json_cache() -> dict:
    """Return the parsed contents of global.json, using an mtime cache.

    Backs both the per-tag filter (DBG_TAGS) and the finer per-message
    filter (debug_log_message_filters), so both read paths share one file
    read / cache-invalidation check.
    """
    global _json_cache, _json_cache_mtime
    path = _GLOBAL_JSON_PATH

    with _json_cache_lock:
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = 0.0

        if mtime != _json_cache_mtime:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            _json_cache = data
            _json_cache_mtime = mtime

        return _json_cache


def _get_active_tags() -> dict[str, bool]:
    """Return the currently enabled debug tags from global.json."""
    return _get_global_json_cache().get("debug_log_tags", {})


def _get_active_msg_filters() -> dict[str, dict[str, bool]]:
    """Return {tag: {callsite_id: False, ...}} per-message overrides.

    Only explicit disables are ever persisted — a callsite absent from a
    tag's dict is enabled by default.
    """
    return _get_global_json_cache().get("debug_log_message_filters", {})


# ── Per-message call-site registry ─────────────────────────────────────────────
# Powers the "drill into a tag" popup in the config editor: for a given TAG,
# lists every individual dbg()/_dbg() call site in the codebase that starts
# with "[TAG]" so each one can be toggled on/off independently. This registry
# is built by scanning source files — it is never consulted by dbg() itself,
# only get_dbg_call_sites() (used by the UI). dbg() only ever reads the saved
# overrides from _get_active_msg_filters().
_CALL_SITE_RE = re.compile(
    r'(?<![A-Za-z0-9_])(?:_dbg|dbg)\(\s*f?(["\'])((?:\\.|(?!\1).)*)\1', re.DOTALL
)
_TAG_PREFIX_RE = re.compile(r'^\[([A-Za-z_]+)\]')
_CALL_SITE_SKIP_DIRS = {"__pycache__", ".git", "venv", ".venv", "node_modules", "backups", "configs"}

_call_site_registry: dict[str, list[tuple[str, str]]] = {}
_call_site_lock = threading.Lock()
_call_site_scanned = False


def rescan_dbg_call_sites() -> None:
    """(Re)scan every .py file under the package directory for dbg() call
    sites, grouping them by their leading [TAG]. Cheap enough to call each
    time the config editor opens the per-tag message popup, so edits made
    to the source since the last scan show up without a restart.
    """
    global _call_site_registry, _call_site_scanned
    registry: dict[str, list[tuple[str, str]]] = {}

    for dirpath, dirnames, filenames in os.walk(_PKG_DIR):
        dirnames[:] = [d for d in dirnames if d not in _CALL_SITE_SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    src = f.read()
            except Exception:
                continue
            rel = os.path.relpath(fpath, _PKG_DIR)
            for m in _CALL_SITE_RE.finditer(src):
                literal = m.group(2)
                tag_m = _TAG_PREFIX_RE.match(literal)
                if not tag_m:
                    continue
                tag = tag_m.group(1)
                lineno = src.count("\n", 0, m.start()) + 1
                label = literal.strip()
                if len(label) > 90:
                    label = label[:87] + "..."
                registry.setdefault(tag, []).append((f"{rel}:{lineno}", label))

    for sites in registry.values():
        sites.sort(key=lambda t: t[0])

    with _call_site_lock:
        _call_site_registry = registry
        _call_site_scanned = True


def get_dbg_call_sites(tag: str) -> list[tuple[str, str]]:
    """Return [(callsite_id, label), ...] for every dbg() call site whose
    message starts with "[TAG]". Triggers a scan on first use.
    """
    with _call_site_lock:
        scanned = _call_site_scanned
    if not scanned:
        rescan_dbg_call_sites()
    with _call_site_lock:
        return list(_call_site_registry.get(tag, []))


def get_dbg_message_overrides(tag: str) -> dict[str, bool]:
    """Return the saved per-message overrides for a tag (only explicit
    disables are stored; anything absent from the dict defaults to enabled).
    """
    return dict(_get_active_msg_filters().get(tag, {}))


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


def log_dashboard_line(msg: str, site_name: str = "") -> None:
    """
    Mirror a dashboard Log-tab line into the debug log file.

    Unlike dbg(), this always writes when debug logging is enabled —
    it bypasses the DBG_TAGS filter entirely, since these lines are
    already the user-facing activity lines shown in the Log tab (e.g.
    "Recording started: ..."), not internal [TAG]-prefixed traces that
    are meant to be toggled on/off individually.

    No-op if debug logging is currently disabled.
    """
    enabled, path = get_debug_log_config()
    if not enabled or not path:
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[{site_name}] " if site_name else ""
    _write_debug_log(f"[{ts}] {prefix}{msg}")


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
            tags = _get_active_tags()
            allowed = tags.get(tag, False)   # unknown tags are dropped
            if not allowed:
                return

            # ── Per-message filter ──────────────────────────────────────────
            # Finer-grained than the tag switch above: individual call sites
            # within an enabled tag can still be silenced. Only pay the cost
            # of identifying the call site (via the caller's frame) when this
            # tag actually has overrides configured — the common case has
            # none, so this is a no-op dict lookup for most dbg() calls.
            msg_overrides = _get_active_msg_filters().get(tag)
            if msg_overrides:
                caller = sys._getframe(1)
                try:
                    rel = os.path.relpath(os.path.abspath(caller.f_code.co_filename), _PKG_DIR)
                except Exception:
                    rel = caller.f_code.co_filename
                callsite_id = f"{rel}:{caller.f_lineno}"
                if msg_overrides.get(callsite_id) is False:
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



