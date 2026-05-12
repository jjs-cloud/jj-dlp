"""
logger.py  —  logging & debug helpers for jj-dlp

Globals (set once at startup from config, then read-only):
    ENABLE_STARTUP_LOG   bool  — write jj-dlp-startup-debug.log
    ENABLE_CRASH_LOG     bool  — write jj-dlp-crash.log on unhandled exception
    DEBUG_LOGS_ENABLED   bool  — write per-session debug.log
    DEBUG_LOG_PATH       str   — path for debug.log
    debug_log_lock       Lock  — guards the two DEBUG_* vars above

Public API
----------
startup_dbg(msg)          Write a line to the startup log (if enabled).
startup_dbg_flush()       Write the opening banner (argv, cwd, python path).
dbg(msg)                  Write to debug log.
get_debug_log_path(cfg)   Resolve the debug log path from a config dict.
get_log_path(cfg)         Resolve the activity log path from a config dict.
get_log_file_paths(cfg)   Return (stdout_path, stderr_path) for yt-dlp logging.
"""

import os
import sys
import threading
from datetime import datetime


# ── Startup / crash log flags ─────────────────────────────────────────────────
ENABLE_STARTUP_LOG: bool = False
ENABLE_CRASH_LOG:   bool = True

_STARTUP_LOG: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jj-dlp-startup-debug.log"
)

# ── Runtime debug log (path resolved from config after startup) ───────────────
DEBUG_LOGS_ENABLED: bool = False
DEBUG_LOG_PATH:     str  = ""
debug_log_lock = threading.Lock()

# ── References to output-mode state (injected by main module at startup) ──────
# These are set by jj-dlp.py via configure() so logger doesn't import main.
_output_mode_ref  = None   # callable() -> int  (1=curses, 2=terminal)


def configure(output_mode_fn) -> None:
    """
    Inject accessor for OUTPUT_MODE.
    Call once from jj-dlp.py after the globals are defined there.
    """
    global _output_mode_ref
    _output_mode_ref = output_mode_fn


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

def _write_debug_log(msg: str) -> None:
    with debug_log_lock:
        enabled = DEBUG_LOGS_ENABLED
        path    = DEBUG_LOG_PATH
    if not enabled or not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def dbg(msg: str, site_name: str = "") -> None:
    """
    Write msg (with timestamp and optional site name) to the debug log.
    """
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Prepend site name if provided
    prefix = f"[{site_name}] " if site_name else ""
    full = f"[{ts}] {prefix}{msg}"
    
    _write_debug_log(full)


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
