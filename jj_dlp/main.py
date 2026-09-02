#!/usr/bin/env python3
"""
jj-dlp  —  multi-site stream recorder
"""
__version__ = "1.28.12"

import subprocess
import textwrap
import time
import sys
import os
import json
import re as _re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as _dt_time
from typing import List, Set, Tuple, Dict, Optional
import configparser
import argparse
import locale
import shlex
import uuid
from urllib.parse import urlparse
import shutil

from .deps import ensure_curses, plain_ffmpeg_check, check_ffmpeg, ensure_bin_executable
from . import logger as _logger
from .logger import (
    startup_dbg, startup_dbg_flush,
    dbg,
    log_crash,
    get_debug_log_path, get_log_file_paths, get_checker_log_path,
    ENABLE_CRASH_LOG,
    configure_debug_log as _configure_debug_log,
)

from .config_editor import CONFIG_KEYS, _KEY_DEFAULTS, _compute_config_id, SORT_OPTIONS, _SORT_LABELS, get_config_file_lock
from .config_editor import DOWNLOADER_FLAG_KEYS
from .config_editor import load_global_config as _load_global_config_typed
from . import simulation as _simulation
from .twitch_eventsub import maybe_backfill_last_live

# ── Script start time (for uptime display)) ──────────────────────────────────
_SCRIPT_START_TIME: float = time.time()


class AppState:
    """Owns every piece of process-wide mutable state that used to live as
    module-level globals: the loaded sites, cross-site coordination locks,
    global.json access, and small in-memory caches shared across threads.

    One instance is created in main() and threaded explicitly through every
    function that needs it, instead of functions reaching for module
    globals.
    """

    def __init__(self) -> None:
        # ── Loaded sites + recording-start coordination ─────────────────────
        self.sites: List["SiteState"] = []
        self.recording_start_lock = threading.Lock()

        # ── Windows Job Object / console ctrl handler (orphan-process guard) ─
        self.win_job_handle = None
        self.win_ctrl_handler_ref = None

        # ── Single-instance OS lock handle ───────────────────────────────────
        self.instance_lock_handle = None

        # ── global.json access ───────────────────────────────────────────────
        self.global_json_lock: threading.Lock = threading.Lock()

        # ── App-update availability (set at startup + by the periodic checker) ─
        self.update_available: bool = False
        self.update_available_lock: threading.Lock = threading.Lock()

        # ── LQ (low-quality) downloader bandwidth-saving state ──────────────
        # (streamer, site_label) -> epoch an LQ_Downloader recording was last
        # attempted for that streamer.
        self.lq_attempted: Dict[Tuple[str, str], float] = {}
        self.lq_attempted_lock: threading.Lock = threading.Lock()

        # ── Cached ffprobe binary path, resolved once on first use ──────────
        # `False` means we already looked and found nothing. `None` means
        # not yet resolved.
        self.ffprobe_path_cache: Optional[object] = None

        # ── Process-wide (not per-site) activity log lines ──────────────────
        self.global_log_lines: deque = deque(maxlen=ACTIVITY_LOG_BUFFER_SIZE)
        self.global_log_lock: threading.Lock = threading.Lock()

        # ── Live-tunable ffmpeg-error restart threshold (FF_ERR_THRESH) ─────
        self.ffmpeg_error_restart_threshold: int = 200

    # ── global.json — the sole read/write/update path ──────────────────────

    @staticmethod
    def _global_json_path() -> str:
        """Return the absolute path to global.json (next to this file)."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "global.json")

    def load_global_json(self) -> dict:
        """Load the global.json file. Returns an empty dict if the file does
        not exist or cannot be parsed."""
        path = self._global_json_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(raw)
            if isinstance(data, dict):
                n_prio = len(data.get("priorities", {}))
                dbg(f"[GLOBAL_JSON][DIAG] load OK: path={path!r} size={len(raw)} priorities_keys={n_prio}")
                return data
            dbg(f"[GLOBAL_JSON][DIAG] load: parsed JSON was not a dict (type={type(data).__name__}) — returning {{}}")
        except FileNotFoundError:
            dbg(f"[GLOBAL_JSON][DIAG] load: file not found at {path!r} — returning {{}}")
        except Exception as e:
            # This is the dangerous case: the file exists but failed to parse
            # (e.g. truncated by a crash/kill mid-write). Log the raw content
            # length so we can tell a torn write from a genuinely empty/missing
            # file after the fact.
            try:
                size = os.path.getsize(path)
            except OSError as size_err:
                dbg(f"load_global_json: {size_err}")
                size = -1
            dbg(f"[GLOBAL_JSON][DIAG] load FAILED: path={path!r} on-disk size={size} error={e!r} — returning {{}} (THIS IS LIKELY THE BUG)")
        return {}

    def _backup_global_json_if_due(self, data: dict) -> None:
        """Back up global.json into backups/ if more than
        _GLOBAL_JSON_BACKUP_INTERVAL seconds have passed since the last backup."""
        last_backup_ts = data.get("_last_backup_ts")
        now = time.time()
        if isinstance(last_backup_ts, (int, float)) and (now - last_backup_ts) < _GLOBAL_JSON_BACKUP_INTERVAL:
            return  # Backed up recently enough.

        src = self._global_json_path()
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

    def save_global_json(self, data: dict) -> None:
        """Write *data* to global.json. Silently ignores errors.

        Before writing, backs up the current global.json to backups/ if it's
        been more than 24h since the last backup (see _backup_global_json_if_due).
        """
        self._backup_global_json_if_due(data)
        path = self._global_json_path()
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            # os.replace is atomic on both POSIX and Windows — a crash/kill at
            # any point up to here leaves the original global.json untouched;
            # a crash after this line leaves the new file fully written.
            os.replace(tmp_path, path)
            dbg(f"[GLOBAL_JSON][DIAG] save OK: path={path!r} priorities_keys={len(data.get('priorities', {}))}")
        except Exception as e:
            dbg(f"[GLOBAL_JSON][DIAG] save FAILED: path={path!r} error={e!r}")
            try:
                os.remove(tmp_path)
            except Exception as e:
                dbg(f"save_global_json: {e}")
                pass

    def update_global_json(self, mutate_fn) -> dict:
        """Load global.json, apply mutate_fn(gdata), and save unless it returns False."""
        with self.global_json_lock:
            gdata = self.load_global_json()
            if mutate_fn(gdata) is not False:
                self.save_global_json(gdata)
            return gdata

    def get_config_id(self) -> str:
        """Return a stable short ID for the current set of loaded config file paths."""
        return _compute_config_id([site.config_path for site in self.sites])

    # ── Process-wide activity log ────────────────────────────────────────────

    def log_global_line(self, msg: str) -> None:
        """Append a timestamped line to the process-wide (not per-site) activity log."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.global_log_lock:
            self.global_log_lines.append(f"[{ts}] {msg}")
        _logger.log_dashboard_line(msg)

    # ── yt-dlp PID tracking (for zombie-process detection on next launch) ───

    def add_yt_dlp_pid(self, pid: int) -> None:
        def _mutate(gdata):
            pids = gdata.setdefault("yt_dlp_pids", [])
            if pid in pids:
                return False
            pids.append(pid)
        self.update_global_json(_mutate)

    def remove_yt_dlp_pid(self, pid: int) -> None:
        def _mutate(gdata):
            pids = gdata.get("yt_dlp_pids", [])
            if pid not in pids:
                return False
            pids.remove(pid)
            gdata["yt_dlp_pids"] = pids
        self.update_global_json(_mutate)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def emergency_kill_all(self) -> None:
        """Stop every loaded site and kill its yt-dlp/ffmpeg processes.
        Last-resort cleanup for OS-level close/kill signals (X button, SIGHUP/SIGTERM)."""
        for _s in list(self.sites):
            try:
                _s.stop()
            except Exception as e:
                dbg(f"emergency_kill_all: {e}")
                pass
        # Best-effort: give threads a brief window to reach their finally:
        # blocks and persist segment-continuation state before the process
        # is torn down.
        deadline = time.time() + 2.5
        for _s in list(self.sites):
            for _t in list(_s.recording_threads):
                remaining = deadline - time.time()
                if remaining <= 0:
                    return
                if _t.is_alive():
                    try:
                        _t.join(timeout=remaining)
                    except Exception as e:
                        dbg(f"emergency_kill_all: join failed: {e}")
                        pass


def _install_windows_job_object(app: "AppState") -> None:
    """Assign this process to a Windows Job Object so all child yt-dlp/ffmpeg
    processes are force-killed by the OS the instant this process dies."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            dbg(f"[JOBOBJECT] CreateJobObjectW failed, err={ctypes.get_last_error()}")
            return

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            dbg(f"[JOBOBJECT] SetInformationJobObject failed, err={ctypes.get_last_error()}")
            kernel32.CloseHandle(job)
            return

        current_process = kernel32.GetCurrentProcess()
        ok = kernel32.AssignProcessToJobObject(job, current_process)
        if not ok:
            # Common cause: already running inside another job that doesn't
            # allow nesting on older Windows versions. Not fatal — we just
            # lose the OS-level guarantee and fall back to the console
            # ctrl handler / signal handlers below.
            dbg(f"[JOBOBJECT] AssignProcessToJobObject failed, err={ctypes.get_last_error()}")
            kernel32.CloseHandle(job)
            return

        app.win_job_handle = job
        dbg("[JOBOBJECT] Process assigned to job with KILL_ON_JOB_CLOSE — "
            "yt-dlp/ffmpeg children will be killed automatically if jj-dlp exits/dies.")
    except Exception as e:
        dbg(f"[JOBOBJECT] setup failed: {e}")


def _install_windows_console_ctrl_handler(app: "AppState") -> None:
    """Catch the Windows console close/logoff/shutdown events and run cleanup
    before the ~5s grace period expires."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6

        HANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def _console_ctrl_handler(ctrl_type):
            if ctrl_type in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                dbg(f"[CTRLHANDLER] console ctrl_type={ctrl_type} received — killing all procs")
                app.emergency_kill_all()
                # Returning False lets the OS's default action (terminate)
                # proceed after our cleanup runs.
                return 0
            return 0

        # Keep a reference alive on app so it isn't garbage collected
        # (ctypes callback objects must outlive the registration).
        app.win_ctrl_handler_ref = HANDLER_ROUTINE(_console_ctrl_handler)
        ok = ctypes.windll.kernel32.SetConsoleCtrlHandler(app.win_ctrl_handler_ref, True)
        if not ok:
            dbg(f"[CTRLHANDLER] SetConsoleCtrlHandler failed, err={ctypes.get_last_error()}")
        else:
            dbg("[CTRLHANDLER] console ctrl handler installed")
    except Exception as e:
        dbg(f"[CTRLHANDLER] setup failed: {e}")


def _install_posix_signal_handlers(app: "AppState") -> None:
    """Catch SIGHUP/SIGTERM on Linux/macOS and kill child processes explicitly,
    since they run in their own session and don't get these signals automatically."""
    if sys.platform == "win32":
        return
    import signal as _signal

    def _handler(signum, _frame):
        dbg(f"[SIGHANDLER] received signal={signum} — killing all procs before exit")
        app.emergency_kill_all()
        # Restore default behavior and re-raise so the process still exits
        # the way it normally would for this signal.
        _signal.signal(signum, _signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    # Deliberately NOT touching SIGINT here: Ctrl-C is already handled
    # cleanly via KeyboardInterrupt elsewhere in main()/run(), and that path
    # already reaches the dashboard's normal quit/kill_all_procs logic. We
    # only need to catch the signals that bypass that normal path entirely
    # — SIGHUP (terminal window closed) and SIGTERM (killed from outside).
    for _sig in (_signal.SIGHUP, _signal.SIGTERM):
        try:
            _signal.signal(_sig, _handler)
        except Exception as e:
            dbg(f"[SIGHANDLER] failed to install handler for {_sig}: {e}")


def _install_shutdown_safety_net(app: "AppState") -> None:
    """Install every layer of orphan-process protection. Call once, as early
    as possible in main(). Cheap and safe to call even if some layers fail
    (e.g. Job Object nesting denied) — the remaining layers still apply.
    """
    _install_windows_job_object(app)
    _install_windows_console_ctrl_handler(app)
    _install_posix_signal_handlers(app)
    import atexit
    atexit.register(app.emergency_kill_all)


def _install_thread_excepthook(app: "AppState") -> None:
    """Route uncaught exceptions from any background thread to the Log tab
    instead of dying silently."""
    import traceback as _tb

    def _hook(args: "threading.ExceptHookArgs") -> None:
        exc_type  = args.exc_type
        exc_val   = args.exc_value
        exc_tb    = args.exc_traceback
        thread    = args.thread
        name      = thread.name if thread else "?"

        one_line = (
            f"BACKGROUND THREAD CRASHED ({name}): "
            f"{exc_type.__name__}: {exc_val}"
        )
        tb_text = "".join(_tb.format_exception(exc_type, exc_val, exc_tb))

        # Always persist the full traceback to the debug/crash logs.
        try:
            _logger.log_crash(exc_val)
        except Exception as e:
            dbg(f"_hook: {e}")
            pass
        try:
            startup_dbg(f"THREAD CRASH: {one_line}\n{tb_text}")
        except Exception as e:
            dbg(f"_hook: {e}")
            pass
        # Preserve the default behaviour of printing to stderr (useful when
        # running outside the curses UI).
        try:
            print(f"Exception in thread {name}:\n{tb_text}", file=sys.stderr)
        except Exception as e:
            dbg(f"_hook: {e}")
            pass

        # Surface on the Log tab for every site — the user-visible part.
        for s in list(app.sites):
            try:
                s.log_line(f"ERROR: {one_line}")
            except Exception as e:
                dbg(f"_hook: {e}")
                pass

    threading.excepthook = _hook


# ══════════════════════════════════════════════════════════════════════════════
# Config loading
# ══════════════════════════════════════════════════════════════════════════════

def _safe_int(value, default):
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value)
    except Exception as e:
        dbg(f"_safe_int: {e}")
        return default


def _parse_general_section(general, config_path: str) -> dict:
    """Read all site-scoped CONFIG_KEYS from the [General] section into a typed dict."""
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


def _build_section_cmd(parser: configparser.ConfigParser, section: str) -> list:
    """Build a yt-dlp argv list from the KEY = value pairs in *section*
    (Checker, Downloader, or LQ_Downloader)."""
    cmd: list = []
    if not parser.has_section(section):
        return cmd

    sect = parser[section]

    for flag_def in DOWNLOADER_FLAG_KEYS:
        key = flag_def.name
        # Coerce the schema default ("true"/"false") into the bool used when
        # the key is missing from this section entirely — e.g. shipped
        # templates set COOKIES_FROM_BROWSER's default to "true", so an
        # omitted key still enables cookies rather than silently disabling them.
        bool_fallback = flag_def.default.strip().lower() not in ("", "false", "0", "no")

        if key == "COOKIES_FROM_BROWSER":
            if sect.getboolean(key, fallback=bool_fallback):
                general = parser["General"] if parser.has_section("General") else {}
                browser = (general.get("BROWSER", "") or "").strip().lower()
                if browser and browser != "disabled":
                    cmd.extend([flag_def.cli_flag, browser])
            continue

        if key == "EXTRA_ARGS":
            extra_raw = sect.get(key, flag_def.default).strip()
            if extra_raw:
                cmd.extend(shlex.split(extra_raw, posix=True))
            continue

        if key not in sect:
            continue

        if flag_def.type == "bool":
            if sect.getboolean(key, fallback=bool_fallback):
                cmd.append(flag_def.cli_flag)
        else:
            raw = sect.get(key, "").strip()
            if raw:
                # NOTE: posix=True (not sys.platform-dependent) is required here.
                # Non-posix mode does NOT merge a quoted "a b c" into one token —
                # it still splits on whitespace and leaves stray quote chars in
                # the pieces, which breaks any value needing embedded spaces
                # (e.g. DOWNLOADER_ARGS = ffmpeg:"-fps_mode passthrough ...").
                # posix=True correctly groups quoted spans and strips the quotes.
                pieces = shlex.split(raw, posix=True) or [raw]
                cmd.append(flag_def.cli_flag)
                cmd.extend(pieces)

    return cmd


def _parse_checker_and_downloader(parser: configparser.ConfigParser) -> tuple:
    """Return (checker_cmd, downloader_cmd, lq_downloader_cmd) argv lists
    built from the KEY = value pairs in [Checker], [Downloader], and
    [LQ_Downloader]."""
    checker_cmd        = _build_section_cmd(parser, "Checker")
    downloader_cmd      = _build_section_cmd(parser, "Downloader")
    lq_downloader_cmd   = _build_section_cmd(parser, "LQ_Downloader")

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
    """Resolve the yt-dlp invocation for the current platform: configured
    path, then bundled module, then system binary."""
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

    # delimiters=('=',) — some Downloader/Checker/LQ_Downloader values contain
    # a colon (e.g. DOWNLOADER_ARGS = ffmpeg:"-fps_mode passthrough ..."). The
    # default ':' delimiter would misparse that colon as a second key/value
    # split, silently truncating the value. Restricting to '=' avoids that.
    # strict=False: tolerate duplicate keys/sections in hand-edited config
    # files (e.g. the same streamer accidentally added twice) instead of
    # raising DuplicateOptionError/DuplicateSectionError. The last value for
    # a duplicated key wins.
    parser = configparser.ConfigParser(allow_no_value=True, interpolation=None, delimiters=('=',), strict=False)
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
    """Load global.conf and return the keys that are truly global, fully typed.

    All key names, defaults, and types live in config_editor.CONFIG_KEYS — the
    single source of truth. This just resolves the file path and delegates the
    actual parsing/coercion to config_editor.load_global_config().
    """
    return _load_global_config_typed(get_global_conf_path())

def _write_global_conf_key(key: str, value: str) -> None:
    path = get_global_conf_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        dbg(f"_write_global_conf_key: {e}")
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
    except Exception as e:
        dbg(f"_write_global_conf_key: {e}")
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Per-site state
# ══════════════════════════════════════════════════════════════════════════════

# ── Single-instance guard ───────────────────────────────────────────────────
# Prevents two jj-dlp processes from running at once, since global.json has
# no cross-process locking and concurrent writers can silently overwrite
# each other's data.


def _instance_lock_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "jj-dlp.instance.lock")


def acquire_single_instance_lock(app: "AppState") -> bool:
    """Try to take an OS-level exclusive lock on the instance lock file.

    Returns True if acquired (safe to proceed) or False if another instance
    already holds it. The lock is released automatically on process exit.
    """
    path = _instance_lock_path()
    try:
        f = open(path, "a+")
    except Exception as e:
        # If we can't even open the lock file, don't block startup over it —
        # log and let the app run rather than failing closed on e.g. a
        # permissions hiccup.
        dbg(f"[GLOBAL_JSON][DIAG] instance lock: could not open {path!r}: {e!r} — proceeding without the guard")
        return True

    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                f.close()
                return False
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                return False
    except Exception as e:
        dbg(f"[GLOBAL_JSON][DIAG] instance lock: locking primitive failed unexpectedly: {e!r} — proceeding without the guard")
        f.close()
        return True

    # Lock acquired — record our PID for anyone who opens the file to look.
    try:
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}\n")
        f.flush()
    except Exception as e:
        dbg(f"acquire_single_instance_lock: {e}")
        pass  # Best-effort diagnostics only; the lock itself is already held.

    app.instance_lock_handle = f  # keep alive so the lock isn't released early
    dbg(f"[GLOBAL_JSON][DIAG] instance lock acquired: pid={os.getpid()} path={path!r}")
    return True

# How often global.json should be backed up.  The timestamp of the last
# backup is stored inside global.json itself (key "_last_backup_ts"), so the
# 24h window survives restarts instead of resetting every time the app launches.
_GLOBAL_JSON_BACKUP_INTERVAL: float = 24 * 60 * 60  # seconds


# How often global.json should be backed up.  The timestamp of the last
# backup is stored inside global.json itself (key "_last_backup_ts"), so the
# 24h window survives restarts instead of resetting every time the app launches.
_GLOBAL_JSON_BACKUP_INTERVAL: float = 24 * 60 * 60  # seconds


def _load_skip_disabled(app: "AppState", config_path: str) -> Set[str]:
    """Load the set of 'skip disabled' streamers (temporarily blocked for
    this live session only) for *config_path* from global.json."""
    gdata = app.load_global_json()
    entries = gdata.get("skip_disabled", {}).get(config_path, [])
    if not isinstance(entries, list):
        return set()
    return {str(s).strip().lower() for s in entries if str(s).strip()}


def _save_skip_disabled(app: "AppState", config_path: str, skip_disabled: Set[str]) -> None:
    def _mutate(gdata):
        all_skip = gdata.setdefault("skip_disabled", {})
        if skip_disabled:
            all_skip[config_path] = sorted(skip_disabled)
        else:
            all_skip.pop(config_path, None)
    app.update_global_json(_mutate)


def _site_json_bucket(gdata: dict, config_path: str) -> dict:
    """Return gdata['sites'][site_key], creating it if needed."""
    sites = gdata.setdefault("sites", {})
    return sites.setdefault(os.path.basename(config_path), {})


def _load_last_live_cache(app: "AppState", config_path: str) -> Dict[str, float]:
    """Return the last-live timestamps for the given site from global.json.

    The site is identified by its config filename (without path).  Each entry
    in the returned dict maps a streamer name to the Unix epoch at which their
    most recent recording ended.
    """
    site_key = os.path.basename(config_path)
    with app.global_json_lock:
        global_data = app.load_global_json()
    site_data = global_data.get("sites", {}).get(site_key, {})
    raw = site_data.get("last_live", {})
    if isinstance(raw, dict):
        return {k: float(v) for k, v in raw.items()}
    return {}


def _save_last_live_cache(app: "AppState", config_path: str, last_live: Dict[str, float]) -> None:
    """Persist last-live timestamps for the given site into global.json.

    Merges with any existing data so other sites' entries are preserved.
    """
    def _mutate(gdata):
        _site_json_bucket(gdata, config_path)["last_live"] = dict(last_live)
    app.update_global_json(_mutate)


# How many of the most recent disk-rate graph bars to persist across restarts.
# Deliberately larger than any realistic terminal width so a wider window on
# relaunch still shows a full graph.
_GRAPH_PERSIST_BARS: int = 500


def _load_last_gql_backfill_ts(app: "AppState", config_path: str) -> Optional[float]:
    """Return the epoch this site's last_live GQL backfill last fired, or
    None if it has never run for this site."""
    site_key = os.path.basename(config_path)
    with app.global_json_lock:
        global_data = app.load_global_json()
    site_data = global_data.get("sites", {}).get(site_key, {})
    ts = site_data.get("last_gql_backfill_ts")
    try:
        return float(ts) if ts is not None else None
    except (TypeError, ValueError):
        return None


def _save_last_gql_backfill_ts(app: "AppState", config_path: str, ts: float) -> None:
    """Persist the epoch this site's last_live GQL backfill last fired."""
    def _mutate(gdata):
        _site_json_bucket(gdata, config_path)["last_gql_backfill_ts"] = ts
    app.update_global_json(_mutate)


def _load_disk_rate_history(app: "AppState") -> List[float]:
    """Return the persisted top-graph disk-rate bars from global.json."""
    with app.global_json_lock:
        global_data = app.load_global_json()
    raw = global_data.get("disk_rate_history", [])
    if not isinstance(raw, list):
        return []
    bars: List[float] = []
    for v in raw[-_GRAPH_PERSIST_BARS:]:
        try:
            bars.append(float(v))
        except (TypeError, ValueError):
            continue
    return bars


def _save_disk_rate_history(app: "AppState", bars) -> None:
    """Persist the most recent disk-rate graph bars into global.json.

    Merges with any existing data so other keys are preserved. Keeps at most
    _GRAPH_PERSIST_BARS entries.
    """
    def _mutate(gdata):
        gdata["disk_rate_history"] = [float(b) for b in bars][-_GRAPH_PERSIST_BARS:]
    app.update_global_json(_mutate)


def _load_live_since_cache(app: "AppState", config_path: str) -> Dict[str, float]:
    """Return the persisted live-since timestamps for the given site from global.json.
    Maps streamer name to the epoch their current live session started."""
    site_key = os.path.basename(config_path)
    with app.global_json_lock:
        global_data = app.load_global_json()
    site_data = global_data.get("sites", {}).get(site_key, {})
    raw = site_data.get("live_since", {})
    if isinstance(raw, dict):
        return {k: float(v) for k, v in raw.items()}
    return {}


def _save_live_since_cache(app: "AppState", config_path: str, live_since: Dict[str, float]) -> None:
    """Persist live-since timestamps for the given site into global.json.

    Merges with any existing data so other sites' entries are preserved.
    Called on every live/offline transition (see SiteState.mark_live /
    mark_offline), mirroring _save_last_live_cache's call pattern.
    """
    def _mutate(gdata):
        _site_json_bucket(gdata, config_path)["live_since"] = dict(live_since)
    app.update_global_json(_mutate)


def _load_segment_continuation_cache(app: "AppState", config_path: str) -> Dict[str, dict]:
    """Return the persisted AUTO_SUFFIX/SPLIT_AFTER part-numbering continuation
    state for the given site from global.json."""
    site_key = os.path.basename(config_path)
    with app.global_json_lock:
        global_data = app.load_global_json()
    site_data = global_data.get("sites", {}).get(site_key, {})
    raw = site_data.get("segment_continuation", {})
    out: Dict[str, dict] = {}
    if isinstance(raw, dict):
        for streamer, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                next_part = int(entry.get("next_part", 1))
            except (TypeError, ValueError):
                next_part = 1
            unsuffixed_file = entry.get("unsuffixed_file")
            if unsuffixed_file is not None:
                unsuffixed_file = str(unsuffixed_file)
            out[streamer] = {"next_part": next_part, "unsuffixed_file": unsuffixed_file}
    return out


def _save_segment_continuation_cache(app: "AppState", config_path: str, mapping: Dict[str, dict]) -> None:
    """Persist AUTO_SUFFIX/SPLIT_AFTER part-numbering continuation state
    for the given site into global.json."""
    def _mutate(gdata):
        _site_json_bucket(gdata, config_path)["segment_continuation"] = {
            streamer: {"next_part": entry.get("next_part", 1),
                       "unsuffixed_file": entry.get("unsuffixed_file")}
            for streamer, entry in mapping.items()
        }
    app.update_global_json(_mutate)


@dataclass
class LiveSession:
    """All state scoped to one continuous live session for one streamer.
    Only `since` is persisted across restarts; the rest resets on restart."""
    since: float                          # epoch this live session started
    quality_upgraded: bool = False        # UPGRADE_QUALITY already fired once this session
    was_blocked_while_live: bool = False  # observed live-while-disabled at some point this session
    enable_anchor: Optional[float] = None # set on blocked->enabled transition; overrides `since` as NOTIFY_NO_CONFIRM_FILE anchor
    notif_shown: bool = False             # a non-recording notification has already fired this session
    last_restart_anchor: Optional[float] = None  # epoch of most recent restart this session; refreshes NOTIFY_NO_CONFIRM_FILE grace window
    evicted_for_concurrency: bool = False  # evicted for a higher-priority streamer; refreshed at actual restart, not eviction time
    # AUTO_SUFFIX/SPLIT_AFTER restart continuity: lets a new attempt continue
    # _partN numbering instead of starting fresh. Persisted in global.json.
    next_segment_part: int = 1
    unsuffixed_file: Optional[str] = None


class SiteState:
    """All mutable runtime state for a single monitored site/config."""

    def __init__(self, config_path: str, app: "AppState"):
        self.app                  = app
        self.config_path          = config_path
        self.label                = os.path.basename(config_path)
        
        # Load the configuration once during init to retrieve things like site_order
        cfg = load_config(config_path)
        try:
            os.makedirs(cfg["output_dir"], exist_ok=True)
        except Exception as e:
            startup_dbg(
                f"[OUTPUT_DIR] failed to initialize {cfg.get('output_dir')!r}: "
                f"{type(e).__name__}: {e}"
            )
        self.site_order           = cfg.get("site_order", 999)
        
        self.lock                 = threading.Lock()
        self.currently_recording: Set[str] = set()
        self.evicted_streamers:   Set[str] = set()
        # Streamers that have claimed a recording slot (in currently_recording)
        # but are still holding for their Intro Delay period — no yt-dlp
        # process has been launched yet. Guarded by self.lock, same as
        # currently_recording. Used purely so the dashboard can keep showing
        # [● Live] instead of flashing [►  REC] until recording actually starts.
        self.intro_delay_pending: Set[str] = set()
        # Resolution (height, in px) each currently-recording streamer started
        # at, per the checker's --dump-json output. Used by UPGRADE_QUALITY to
        # detect when a source switches to a higher resolution mid-recording.
        # Guarded by self.lock. Cleared when the recording ends.
        self.recording_resolution: Dict[str, int] = {}
        # ffprobe-measured resolution of the on-disk file; falls back to recording_resolution.
        self.display_resolution: Dict[str, int] = {}
        # Epoch the current recording attempt began; gates the recording_resolution fallback.
        self.recording_attempt_started: Dict[str, float] = {}
        # streamer -> path of the file yt-dlp is currently writing; used by the disk-rate graph.
        self.recording_output_paths: Dict[str, str] = {}
        # Streamers with a pending immediate-restart request from the Split popup.
        self.manual_split_requests:  Set[str] = set()
        self.recording_threads:   List[threading.Thread] = []
        self.known_streamers:     Set[str] = set()
        self.trigger_event        = threading.Event()

        # ── Live session tracking ────────────────────────────────────────
        self.session_lock         = threading.Lock()
        self.live_sessions:       Dict[str, "LiveSession"] = {}
        # Persisted `since` epochs, recovered on restart (see mark_live()).
        self._live_since_cache:   Dict[str, float] = _load_live_since_cache(app, config_path)
        # Persisted AUTO_SUFFIX/SPLIT_AFTER continuation state, recovered on restart.
        self._segment_continuation_cache: Dict[str, dict] = _load_segment_continuation_cache(app, config_path)

        # Diagnostic: identifies each monitor reconciliation cycle so live-session
        # transitions can be correlated across checker cycles.
        self._live_check_generation = 0

        self._last_seen_live: Dict[str, float] = {}

        # Streamers disabled via "Skip this stream" — auto-removed from
        # [Block] once the checker sees them go offline.
        self.skip_disabled:       Set[str] = _load_skip_disabled(app, config_path)

        # Dashboard display state (written by monitor thread, read by renderer)
        self.dash_lock            = threading.Lock()
        self.dash_last_live:      Dict[str, float] = _load_last_live_cache(app, config_path)   # streamer -> epoch when recording stopped
        self.dash_next_check_in:  float = 0.0
        self.dash_all_streamers:  List[str] = []
        self.dash_blocked:        Set[str] = set()
        self.dash_log_lines:      deque = deque(maxlen=ACTIVITY_LOG_BUFFER_SIZE)   # recent activity log
        self.dash_debug_lines:    deque = deque(maxlen=DEBUG_LOG_BUFFER_SIZE)      # recent debug-tag log (separate buffer)
        self.dash_stdout_lines:   deque = deque(maxlen=ACTIVITY_LOG_BUFFER_SIZE)   # recent stdout lines
        self.dash_stderr_lines:   deque = deque(maxlen=ACTIVITY_LOG_BUFFER_SIZE)   # recent stderr lines
        # Last "hard" checker-command failure surfaced on the Log tab, or None if healthy.
        self._last_checker_error: Optional[str] = None
        # Same lines, additionally bucketed per-streamer so the STREAMERS
        # panel on the Stdout/Stderr tabs can show one streamer's output in
        # isolation. No liveness-checker output is shown. Buckets are
        # created lazily on first use.
        self.dash_stdout_lines_by_streamer: Dict[str, deque] = {}
        self.dash_stderr_lines_by_streamer: Dict[str, deque] = {}

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

        # Notification cooldown: streamer -> epoch of last notification shown.
        # Shared by both the popup (tkinter/notify-send) and ntfy channels so
        # that they follow the exact same POPUP_COOLDOWN-gated schedule.
        self.notif_last_shown:    Dict[str, float] = {}
        # Whether a "not recording" (disabled / lower-priority) notification
        # has already been shown during the current continuous live session
        # now lives on LiveSession.notif_shown (see was_notif_shown /
        # mark_notif_shown below) — folded in alongside the other
        # per-live-session state instead of being tracked separately.

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

        # Streamer names that have hit the NOTIFY_NO_CONFIRM_FILE deadline
        # (see _check_no_confirm_deadline) — drives the full-screen
        # recording-failure alert in the dashboard. Cleared when the user
        # dismisses the alert.
        self.write_failure_streamers: List[str] = []

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
        try:
            self.app.add_yt_dlp_pid(proc.pid)
        except Exception as e:
            dbg(f"register_proc: {e}")
            pass

    def unregister_proc(self, streamer: str) -> None:
        """Remove a subprocess from the registry (after it exits)."""
        with self._procs_lock:
            proc = self._active_procs.pop(streamer, None)
            if proc:
                try:
                    self.app.remove_yt_dlp_pid(proc.pid)
                except Exception as e:
                    dbg(f"unregister_proc: {e}")
                    pass

    def set_recording_output(self, streamer: str, path: str) -> None:
        """Publish the absolute path of the file *streamer*'s yt-dlp process
        is currently writing (see recording_output_paths)."""
        with self.lock:
            self.recording_output_paths[streamer] = path

    def clear_recording_output(self, streamer: str) -> None:
        """Forget *streamer*'s active recording output path (recording ended)."""
        with self.lock:
            self.recording_output_paths.pop(streamer, None)

    def recording_output_paths_snapshot(self) -> Set[str]:
        """Absolute paths of every file currently being written by yt-dlp,
        across all streamers on this site. Thread-safe copy."""
        with self.lock:
            return set(self.recording_output_paths.values())

    def request_manual_split(self, streamer: str) -> None:
        """Ask record_stream() to force a SPLIT_AFTER split for *streamer*
        right now (used by the File Manager's "Restart the recording instead"
        option). The split reuses the normal SPLIT_AFTER machinery, so the
        current segment is renamed to _partN and recording continues into
        the next part, even when SPLIT_AFTER is configured to 0."""
        with self.lock:
            self.manual_split_requests.add(streamer)

    def kill_proc_for_streamer(self, streamer: str) -> None:
        with self._procs_lock:
            proc = self._active_procs.get(streamer)
        if proc:
            try:
                kill_proc(proc)
            except Exception as e:
                dbg(f"kill_proc_for_streamer: {e}")
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

    def flag_write_failure(self, streamer: str) -> None:
        """Record that *streamer* just hit the NOTIFY_NO_CONFIRM_FILE
        deadline, for display in the full-screen recording-failure alert."""
        with self.dash_lock:
            if streamer not in self.write_failure_streamers:
                self.write_failure_streamers.append(streamer)

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

    # ── Live session tracking ────────────────────────────────────────────
    # See the live_sessions field comment above for why this is the only
    # place that should touch self.live_sessions directly.

    def mark_live(self, streamer: str) -> None:
        """Record that *streamer* is now live, starting a new LiveSession.

        Idempotent — a no-op if *streamer* is already tracked as live, so
        every call site that "notices" a streamer is live (poll loop,
        EventSub callback, LQ start, retry reuse) can call this
        unconditionally instead of re-implementing the
        "if not already tracked" guard itself.

        Recovers the true start time from the persisted cache if this
        process restarted mid-stream, rather than stamping time.time() and
        silently resetting everyone's live duration to zero on restart.
        """
        with self.session_lock:
            if streamer in self.live_sessions:
                session = self.live_sessions[streamer]
                self._last_seen_live[streamer] = time.time()
                dbg(
                    f"[SESSION] mark_live NO-OP streamer={streamer!r} "
                    f"already_live=True since={session.since:.2f} "
                    f"since_age={time.time() - session.since:.1f}s",
                    site_name=streamer,
                )
                return

            _cached_since = self._live_since_cache.get(streamer)
            _since_source = "CACHE" if _cached_since is not None else "NOW"
            since = _cached_since if _cached_since is not None else time.time()

            dbg(
                f"[SESSION] mark_live ENTER streamer={streamer!r} "
                f"since={since:.2f} since_source={_since_source} "
                f"cache_present={_cached_since is not None} "
                f"check_generation={self._live_check_generation}",
                site_name=streamer,
            )

            _cont = self._segment_continuation_cache.get(streamer)
            if _cont:
                self.live_sessions[streamer] = LiveSession(
                    since=since,
                    next_segment_part=_cont.get("next_part", 1),
                    unsuffixed_file=_cont.get("unsuffixed_file"),
                )
            else:
                self.live_sessions[streamer] = LiveSession(since=since)
            snapshot = {s: sess.since for s, sess in self.live_sessions.items()}

            self._last_seen_live[streamer] = time.time()

        with self.lock:
            _still_recording = streamer in self.currently_recording

        dbg(
            f"[SESSION] mark_live COMPLETE streamer={streamer!r} "
            f"since={since:.2f} since_source={_since_source} "
            f"check_generation={self._live_check_generation}",
            site_name=streamer,
        )

        if _still_recording:
            dbg(f"[UPGRADE_QUALITY] mark_live() created a fresh LiveSession "
                f"(quality_upgraded reset to False) while a recording is already "
                f"in progress — likely a transient live_info miss on the prior "
                f"poll cycle", site_name=streamer)
        _save_live_since_cache(self.app, self.config_path, snapshot)

    def mark_offline(self, streamer: str) -> None:
        """Record that *streamer* is no longer live, ending its LiveSession.

        Idempotent — a no-op if *streamer* isn't currently tracked as live.
        Also updates dash_last_live (the existing "recording ended" cache),
        matching the previous behavior of clearing both together.
        """
        dbg(
            f"[SESSION] mark_offline ENTER streamer={streamer!r} "
            f"check_generation={self._live_check_generation}",
            site_name=streamer,
        )

        with self.session_lock:
            if streamer not in self.live_sessions:
                dbg(
                    f"[SESSION] mark_offline NO-OP streamer={streamer!r} "
                    f"reason=not_in_live_sessions "
                    f"live_since_cache_present={streamer in self._live_since_cache} "
                    f"check_generation={self._live_check_generation}",
                    site_name=streamer,
                )
                return

            _session = self.live_sessions[streamer]
            _had_live_since_cache = streamer in self._live_since_cache
            del self.live_sessions[streamer]
            self._live_since_cache.pop(streamer, None)
            self._segment_continuation_cache.pop(streamer, None)
            self._last_seen_live.pop(streamer, None)
            snapshot = {s: sess.since for s, sess in self.live_sessions.items()}
            segment_snapshot = dict(self._segment_continuation_cache)

            _live_session_present_after = streamer in self.live_sessions
            _live_since_cache_present_after = streamer in self._live_since_cache

            dbg(
                f"[SESSION] mark_offline STATE_CLEARED streamer={streamer!r} "
                f"old_since={_session.since:.2f} "
                f"had_live_since_cache={_had_live_since_cache} "
                f"live_session_present_after={_live_session_present_after} "
                f"live_since_cache_present_after={_live_since_cache_present_after} "
                f"check_generation={self._live_check_generation}",
                site_name=streamer,
            )

        with self.lock:
            _still_recording = streamer in self.currently_recording

        if _still_recording:
            # A recording is still actively in progress for this streamer
            # while the checker reported it as not-live this cycle (a
            # transient miss). This tears down the whole LiveSession —
            # including quality_upgraded — even though nothing about the
            # recording itself changed. If mark_live() re-fires shortly
            # after, the once-per-session UPGRADE_QUALITY guard silently
            # resets, letting a second (usually no-op) upgrade check run.
            dbg(f"[UPGRADE_QUALITY] mark_offline() torn down LiveSession while still "
                f"recording — quality_upgraded={_session.quality_upgraded!r} "
                f"live_since={_session.since!r} (this resets the once-per-session guard)",
                site_name=streamer)

        dbg(
            f"[SESSION] mark_offline COMPLETE streamer={streamer!r} "
            f"live_session_present={streamer in self.live_sessions} "
            f"live_since_cache_present={streamer in self._live_since_cache} "
            f"check_generation={self._live_check_generation}",
            site_name=streamer,
        )

        _save_live_since_cache(self.app, self.config_path, snapshot)
        _save_segment_continuation_cache(self.app, self.config_path, segment_snapshot)
        with self.dash_lock:
            self.dash_last_live[streamer] = time.time()
            last_live_snapshot = dict(self.dash_last_live)
        _save_last_live_cache(self.app, self.config_path, last_live_snapshot)

    def is_live(self, streamer: str) -> bool:
        with self.session_lock:
            return streamer in self.live_sessions

    def get_live_since(self, streamer: str) -> Optional[float]:
        """Epoch this streamer's current live session started, or None if
        they're not currently tracked as live."""
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            return session.since if session else None

    def get_live_duration(self, streamer: str) -> Optional[float]:
        """Seconds this streamer has been continuously live, or None if
        they're not currently tracked as live."""
        since = self.get_live_since(streamer)
        return (time.time() - since) if since is not None else None

    def snapshot_live_since(self) -> Dict[str, float]:
        """Return a point-in-time {streamer: since_epoch} copy for the
        dashboard renderer — same shape dash_live_since used to provide."""
        with self.session_lock:
            return {s: sess.since for s, sess in self.live_sessions.items()}

    def get_segment_continuation(self, streamer: str) -> Optional[Tuple[int, Optional[str]]]:
        """Return (next_part, unsuffixed_file) for *streamer*'s current live
        session, or None if the streamer isn't currently tracked as live.

        Used by record_stream() at the top of a new attempt to decide
        whether to resume _partN numbering from a prior attempt (same live
        session) instead of starting fresh at part 1. See LiveSession's
        next_segment_part / unsuffixed_file field comments.
        """
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session is None:
                return None
            return (session.next_segment_part, session.unsuffixed_file)

    def set_segment_continuation(self, streamer: str, next_part: int,
                                  unsuffixed_file: Optional[str]) -> None:
        """Persist the next _partN number (and the path of the most recent
        unsuffixed file, if any, so it can be retroactively renamed once a
        continuation file is confirmed) for *streamer*'s current live
        session. A no-op if the live session already ended (e.g. a race
        between the recording thread exiting and mark_offline() firing) —
        there's nothing meaningful to continue into at that point.

        Also written through to global.json (mirroring live_since) so this
        survives an app restart mid-stream.
        """
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session is None:
                return
            session.next_segment_part = next_part
            session.unsuffixed_file = unsuffixed_file
            self._segment_continuation_cache[streamer] = {
                "next_part": next_part,
                "unsuffixed_file": unsuffixed_file,
            }
            segment_snapshot = dict(self._segment_continuation_cache)
        _save_segment_continuation_cache(self.app, self.config_path, segment_snapshot)

    def was_quality_upgraded(self, streamer: str) -> bool:
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            return bool(session and session.quality_upgraded)

    def mark_quality_upgraded(self, streamer: str) -> None:
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session:
                session.quality_upgraded = True

    def was_blocked_while_live(self, streamer: str) -> bool:
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            return bool(session and session.was_blocked_while_live)

    def mark_blocked_while_live(self, streamer: str) -> None:
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session:
                session.was_blocked_while_live = True

    def clear_blocked_while_live(self, streamer: str) -> None:
        """Consume the was_blocked_while_live flag. Called once the
        blocked->enabled transition has actually been used to refresh the
        enable_anchor, so a *later* block/re-enable within the same live
        session gets treated as its own fresh transition instead of being
        silently absorbed by set_enable_anchor's now-a-no-op guard."""
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session:
                session.was_blocked_while_live = False

    def get_enable_anchor(self, streamer: str) -> Optional[float]:
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            return session.enable_anchor if session else None

    def set_enable_anchor(self, streamer: str, epoch: float) -> None:
        """Set (and refresh) the NOTIFY_NO_CONFIRM_FILE override anchor on
        every blocked->enabled transition. Unlike the old
        set_enable_anchor_if_unset, this always overwrites: each transition
        should get its own fresh 120s grace window, since a manual
        disable/re-enable gap can otherwise leave the previous anchor's
        deadline already in the past by the time the new attempt starts."""
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session:
                session.enable_anchor = epoch

    def get_last_restart_anchor(self, streamer: str) -> Optional[float]:
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            return session.last_restart_anchor if session else None

    def set_last_restart_anchor(self, streamer: str, epoch: float) -> None:
        """Record that *streamer* was just restarted (or will/might restart) (stall recovery, an
        LQ downgrade, a quality-upgrade restart, or a concurrency
        eviction). Used to give the next attempt's NOTIFY_NO_CONFIRM_FILE
        deadline a fresh window, without disturbing `since`/`enable_anchor`
        (which must keep reflecting the real live-session start time).
        Prefer calling this via evict_and_restart() for anything that
        externally evicts an active recording (LQ, quality upgrade,
        concurrency)."""
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session:
                session.last_restart_anchor = epoch

    def clear_last_restart_anchor(self, streamer: str) -> None:
        """Clear the restart anchor once growth is confirmed again, so it
        doesn't linger and affect a later, unrelated attempt."""
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session:
                session.last_restart_anchor = None

    def was_evicted_for_concurrency(self, streamer: str) -> bool:
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            return bool(session and session.evicted_for_concurrency)

    def mark_evicted_for_concurrency(self, streamer: str) -> None:
        """Record that *streamer* was evicted to free a slot for a
        higher-priority streamer. Actual restart time here is unbounded 
        Mirrors was_blocked_while_live's deferred-anchor pattern: this flag is
        consumed at the actual restart point in start_recording_if_needed,
        where last_restart_anchor is refreshed to `now` right then."""
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session:
                session.evicted_for_concurrency = True

    def clear_evicted_for_concurrency(self, streamer: str) -> None:
        """Consume the flag once the streamer actually resumes recording."""
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session:
                session.evicted_for_concurrency = False

    def evict_and_restart(self, streamer: str, refresh_anchor: bool = True) -> None:
        """Evict an actively-recording streamer so it can be restarted
        elsewhere (LQ downgrade, quality-upgrade restart, or concurrency
        eviction — anything that kills a currently-recording streamer with
        the expectation that it (or a replacement recording of it) comes
        back up shortly after).

          1. Flag the streamer as evicted, so record_stream's inner loop
             self-terminates cleanly instead of treating this as a stall
             or failure.
          2. If refresh_anchor (the default), refresh last_restart_anchor
             *before* killing the process, so the next attempt's
             NOTIFY_NO_CONFIRM_FILE deadline is seeded from "now" instead
             of falling back to a stale live_since/enable_anchor and
             firing the write-failure alert almost immediately on the
             retry. This default is only correct when the restart happens
             shortly after eviction (LQ, quality upgrade — both restart
             within seconds). Pass refresh_anchor=False for eviction paths
             where the restart timing is unbounded (concurrency eviction)
             and instead refresh last_restart_anchor at the actual restart
             point — see mark_evicted_for_concurrency().
          3. Kill the ffmpeg/yt-dlp process.

        The stall-detected restart in record_stream() does NOT use this
        method — it's an in-loop self-restart (the recording thread is
        killing and retrying itself, not being evicted by another thread),
        so it has its own teardown sequence and calls
        set_last_restart_anchor() directly.
        """
        with self.lock:
            self.evicted_streamers.add(streamer)
        if refresh_anchor:
            self.set_last_restart_anchor(streamer, time.time())
        self.kill_proc_for_streamer(streamer)

    def was_notif_shown(self, streamer: str) -> bool:
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            return bool(session and session.notif_shown)

    def mark_notif_shown(self, streamer: str) -> None:
        with self.session_lock:
            session = self.live_sessions.get(streamer)
            if session:
                session.notif_shown = True

    def kill_all_procs(self) -> None:
        """Kill every registered yt-dlp process. Called on quit."""
        with self._procs_lock:
            procs = dict(self._active_procs)
        for streamer, proc in procs.items():
            try:
                kill_proc(proc)
            except Exception as e:
                dbg(f"kill_all_procs: {e}")
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
                except Exception as e:
                    dbg(f"get_cached_config: {e}")
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
            self.dash_log_lines.append(line)   # deque(maxlen=...) evicts automatically
        _logger.log_dashboard_line(msg)

    def add_stdout_line(self, line: str, streamer: str = "") -> None:
        with self.dash_lock:
            self.dash_stdout_lines.append(line)
            if streamer:
                buf = self.dash_stdout_lines_by_streamer.get(streamer)
                if buf is None:
                    buf = deque(maxlen=ACTIVITY_LOG_BUFFER_SIZE)
                    self.dash_stdout_lines_by_streamer[streamer] = buf
                buf.append(line)

    def add_stderr_line(self, line: str, streamer: str = "") -> None:
        with self.dash_lock:
            self.dash_stderr_lines.append(line)
            if streamer:
                buf = self.dash_stderr_lines_by_streamer.get(streamer)
                if buf is None:
                    buf = deque(maxlen=ACTIVITY_LOG_BUFFER_SIZE)
                    self.dash_stderr_lines_by_streamer[streamer] = buf
                buf.append(line)

    def stop(self) -> None:
        self._stop_event.set()
        self.trigger_event.set()
        self.kill_all_procs()


# ══════════════════════════════════════════════════════════════════════════════
# Global singletons
# ══════════════════════════════════════════════════════════════════════════════

FFMPEG_ERROR_PATTERNS: List[str] = [
    "timestamp discontinuity",
    "Packet corrupt",
]

# Substrings that indicate the *checker* command itself is broken/misconfigured
# (missing cookies DB, bad binary path, DNS/network failure, permissions, ...)
# as opposed to a normal "this streamer is offline" result. Matching one of
# these means every streamer in the check just silently failed to be
# evaluated, so it's surfaced on the dashboard's Log tab — previously it only
# went to the debug log (off by default) or the raw, easy-to-miss stderr
# pipe view, so a broken checker looked identical to "nobody is live".
_CHECKER_HARD_ERROR_PATTERNS: List[str] = [
    "could not find",
    "cookies database",
    "unsupported browser",
    "permission denied",
    "no such file or directory",
    "not recognized as an internal or external command",
    "command not found",
    "connection refused",
    "network is unreachable",
    "temporary failure in name resolution",
    "name or service not known",
    "certificate verify failed",
]

# Lines from the checker command are stored with these prefixes so draw_stdout_tab
# and draw_stderr_tab can filter them in/out without separate buffers.
_CHECKER_STDOUT_PREFIX: str = "\x00checker\x00"
_CHECKER_STDERR_PREFIX: str = "\x00checker_err\x00"

# Ring-buffer sizes for the per-site dashboard line buffers (see SiteState).
# Activity/stdout/stderr buffers hold a fixed number of lines regardless of
# debug-tag traffic. Debug lines get their own, larger buffer since they are
# high-volume by nature and already fully retained in the debug log file
# when debug logging is enabled — losing old ones from the in-memory Log tab
# view is expected/acceptable, unlike losing real activity lines.
ACTIVITY_LOG_BUFFER_SIZE: int = 200
DEBUG_LOG_BUFFER_SIZE:    int = 1000


def _merge_lines_by_timestamp(a: List[str], b: List[str]) -> List[str]:
    """Merge two chronologically-ordered "[YYYY-MM-DD HH:MM:SS] ..." line
    lists into one combined, still-chronological list.

    Both dash_log_lines and dash_debug_lines are append-only and therefore
    already individually in time order; this does a standard merge-sort
    merge step (O(n+m)) rather than re-sorting everything from scratch.
    """
    def ts_key(line: str) -> str:
        # Lines are "[YYYY-MM-DD HH:MM:SS] ...", so the first 20 chars
        # (including brackets) sort correctly as plain strings.
        return line[:20] if line[:1] == "[" else ""

    merged: List[str] = []
    i = j = 0
    len_a, len_b = len(a), len(b)
    while i < len_a and j < len_b:
        if ts_key(a[i]) <= ts_key(b[j]):
            merged.append(a[i]); i += 1
        else:
            merged.append(b[j]); j += 1
    merged.extend(a[i:])
    merged.extend(b[j:])
    return merged

# ── Ad detection patterns (used by _drain_pipe when AD_ALERTS is enabled) ─────
# Any match updates the per-streamer last-seen timestamp in site.ad_alerts.
# Default mirrors the previous hardcoded three-regex behavior, combined via "|".
_AD_ALERT_PATTERNS_DEFAULT = (
    r'#EXT-X-DISCONTINUITY(?!-SEQUENCE)|(amazon|twitch-ad|/ad/|admanifest|/ads/|/segment/Cv8)|'
    r'#EXT-X-TWITCH-AD|CLASS="twitch-stitched-ad"'
)


def _compile_ad_alert_pattern(cfg: dict) -> Optional["_re.Pattern"]:
    """Compile the site's AD_ALERT_PATTERNS regex, or None if AD_ALERTS is off/invalid."""
    if not cfg.get("ad_alerts", False):
        return None
    raw = cfg.get("ad_alert_patterns") or _AD_ALERT_PATTERNS_DEFAULT
    try:
        return _re.compile(raw, _re.IGNORECASE)
    except _re.error as e:
        dbg(f"[AD] invalid AD_ALERT_PATTERNS regex {raw!r}: {e} — ad alerts disabled")
        return None

# ── LQ (low-quality) downloader bandwidth-saving state ───────────────────────
# AppState.lq_attempted maps (streamer, site_label) → epoch when an
# LQ_Downloader recording was last *attempted* for that streamer. Entries are
# cleared when the streamer goes offline. Any entry whose timestamp is within
# _LQ_RECENT_WINDOW seconds of now is considered "recent" and makes the
# streamer ineligible for another LQ trigger during that online session.
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
# Live notifications (popup + ntfy)
# ══════════════════════════════════════════════════════════════════════════════
#
# Both notification channels — the desktop popup (notify-send/tkinter) and the
# ntfy.sh push notification — represent the exact same "event" (a streamer
# going live, recording or not, and why). They share:
#   • the same message content, built once by _format_live_popup()
#   • the same enable/disable + cooldown + per-session-suppression gate,
#     evaluated once by _maybe_show_live_popup()
# The only thing that differs per channel is whether that channel is enabled
# (POPUP_NOTIFICATIONS vs. NTFY_NOTIFICATIONS / the per-streamer ntfy
# override) and how the message gets delivered.

def _resolve_mount_point(path: str) -> str:
    """Resolve *path* to its actual mount point on Linux.
    """
    if sys.platform != "linux":
        return path

    try:
        real = os.path.realpath(path)
    except Exception as e:
        dbg(f"_resolve_mount_point: {e}")
        real = path

    best = None
    best_len = 0

    try:
        with open("/proc/self/mountinfo") as fh:
            for line in fh:
                # Fields (space-separated):
                #  0  mount-id  1  parent-id  2  major:minor  3  root
                #  4  mount-point  5  mount-options  ...  dash  fs-type  source  super-opts
                parts = line.split()
                if len(parts) < 5:
                    continue
                mount_point = parts[4]
                # Require a real path-boundary match, not just a string
                # prefix — otherwise "/mnt/data2" would incorrectly match
                # a mount point of "/mnt/data".
                if (real == mount_point
                        or real.startswith(mount_point.rstrip("/") + "/")) \
                        and len(mount_point) > best_len:
                    best = mount_point
                    best_len = len(mount_point)
    except Exception as e:
        # /proc not available (non-Linux) — return the original path
        dbg(f"_resolve_mount_point: {e}")
        return path

    return best if best else path


def _safe_disk_usage(path: str):
    """Return ``shutil.disk_usage()`` results for *path*, working around
    the automount / statvfs mismatch on Linux.
    """
    if sys.platform != "linux":
        return shutil.disk_usage(path)

    mount = _resolve_mount_point(path)
    try:
        st = os.statvfs(mount)
        total = st.f_frsize * st.f_blocks
        free  = st.f_frsize * st.f_bfree
        used  = total - free
        return shutil._ntuple_diskusage(total, used, free)
    except Exception as e:
        # Last resort — let shutil try the original path
        dbg(f"_safe_disk_usage: {e}")
        return shutil.disk_usage(path)


def _get_disk_info_string(cfg: dict = None) -> str:
    """Build a short disk-space summary string, e.g. "data 120.5G, ext 8.2G".

    Mirrors the drive-resolution logic used by the dashboard's system panel
    ── Disk ── section (global.conf DISK_DRIVES take precedence, falling
    back to the site's own disk_drives / output_dir) so the popup/ntfy
    notifications always report the exact same disk info shown there.
    Percentage used is intentionally omitted — only free space remains, to
    keep the notification line short.
    """
    try:
        seen_drives: list = []
        seen_drives_set: set = set()

        global_cfg = load_global_config()
        global_drives = global_cfg.get("disk_drives", [])
        for d in global_drives:
            key = os.path.normcase(d)
            if key not in seen_drives_set:
                seen_drives_set.add(key)
                seen_drives.append(d)

        if cfg:
            drives_for_site = cfg.get("disk_drives", [])
            for d in drives_for_site:
                key = os.path.normcase(d)
                if key not in seen_drives_set:
                    seen_drives_set.add(key)
                    seen_drives.append(d)

        if not seen_drives:
            fallback_dir = (cfg or {}).get("output_dir", "/")
            seen_drives = [fallback_dir]

        parts = []
        for drive in seen_drives:
            try:
                usage = _safe_disk_usage(drive)
            except Exception as _disk_exc:
                dbg(f"[DISK] _safe_disk_usage({drive!r}) FAILED (notification): "
                    f"{type(_disk_exc).__name__}: {_disk_exc}")
                continue
            free_gb = usage.free / (1024 ** 3)
            drv_label = os.path.basename(drive.rstrip("/\\")) or drive
            drv_label = drv_label[:6]
            parts.append(f"{drv_label} {free_gb:.1f}G")

        return ", ".join(parts)
    except Exception as _disk_outer_exc:
        dbg(f"[DISK] exception building disk info string for notification: "
            f"{type(_disk_outer_exc).__name__}: {_disk_outer_exc}")
        return ""


def _format_live_popup(streamer: str, is_recording: bool = True,
                       reason: str = "", warning: str = "",
                       site_label: str = "", disk_info: str = "",
                       confirmed: bool = False) -> list:
    """Build the lines of text shown for a "streamer is live" notification.

    Used to build both the popup body (notify-send/tkinter) and the ntfy.sh
    push notification body, so the two channels always show identical
    information: live status (recording / not recording), the site, disk
    space remaining, and — when applicable — the warning and/or the reason
    recording did not start.

    *confirmed* is only meaningful when *is_recording* is True. It marks
    that this notification was held until NOTIFY_CONFIRM_FILE actually
    observed the recording file growing on disk.
    """
    marker = "🔴" if is_recording else "🟡"
    status = "Recording" if is_recording else "Not recording"
    if is_recording and confirmed:
        status += " (confirmed)"
    lines = [
        f"{marker} {streamer} is LIVE",
        f"{marker} {status}",
    ]
    if site_label:
        lines.append(f"Site: {site_label}")
    if warning:
        lines.append(f"Warning: {warning}")
    if reason:
        lines.append(f"Reason: {reason}")
    if disk_info:
        lines.append(f"Disk: {disk_info}")
    return lines


def _show_live_popup(streamer: str, source: str = "poll", popup_timeout: int = 15,
                     is_recording: bool = True, reason: str = "",
                     warning: str = "", site_label: str = "", disk_info: str = "",
                     confirmed: bool = False) -> None:
    dbg(f"[POPUP] enqueue popup streamer={streamer!r} source={source!r} timeout={popup_timeout} is_recording={is_recording} reason={reason!r} warning={warning!r} confirmed={confirmed}")
    def _run():
        popup_lines = _format_live_popup(streamer, is_recording, reason, warning, site_label, disk_info, confirmed=confirmed)
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


def _send_ntfy_notification(streamer: str, site_label: str, is_recording: bool = True,
                            reason: str = "", warning: str = "", disk_info: str = "",
                            confirmed: bool = False) -> None:
    dbg(f"[NTFY] _send_ntfy_notification called: streamer={streamer!r} "
        f"is_recording={is_recording} site_label={site_label!r} reason={reason!r} warning={warning!r} "
        f"disk_info={disk_info!r} confirmed={confirmed}")

    global_cfg = load_global_config()
    topic = global_cfg.get("ntfy_topic", "").strip()
    url = "https://ntfy.sh"
    dbg(f"[NTFY] resolved global config: ntfy_topic={topic!r} ntfy_url={url!r} "
        f"(raw global.conf path={get_global_conf_path()!r})")

    if not topic:
        dbg("[NTFY] No ntfy_topic configured (NTFY_TOPIC blank in global.conf [General]); "
            "skipping notification.")
        return

    full_url = f"{url}/{topic}"

    # Same content the popup shows: LIVE/recording status, site, disk space
    # remaining, and the warning/reason when applicable. The "Title" header
    # must stay ASCII — emoji markers live in the body instead, where UTF-8
    # is safe.
    popup_lines = _format_live_popup(streamer, is_recording, reason, warning, site_label, disk_info,
                                     confirmed=confirmed)
    title = "jj-dlp"
    body = "\n".join(popup_lines)

    headers = {
        "Title": title,
        "Priority": "high" if is_recording else "default",
        "Tags": "red_circle,record" if is_recording else "yellow_circle",
    }

    dbg(f"[NTFY] prepared request: url={full_url!r} title={title!r} "
        f"body={body!r} headers={headers!r}")

    def _post():
        import urllib.request
        import urllib.error
        dbg(f"[NTFY] POST thread started for streamer={streamer!r} -> {full_url!r}")
        try:
            req = urllib.request.Request(
                full_url,
                data=body.encode("utf-8"),
                headers=headers,
                method="POST"
            )
            dbg(f"[NTFY] request object built; opening connection (timeout=10s)")
            with urllib.request.urlopen(req, timeout=10) as response:
                res_code = response.getcode()
                res_headers = dict(response.getheaders())
                try:
                    res_body = response.read().decode("utf-8", errors="replace")
                except Exception as read_e:
                    dbg(f"_post: {read_e}")
                    res_body = f"<could not read response body: {read_e}>"
                dbg(f"[NTFY] Notification sent successfully for {streamer}. "
                    f"Status: {res_code} response_headers={res_headers!r} body={res_body!r}")
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception as read_e:
                dbg(f"_post: {read_e}")
                err_body = "<unreadable>"
            dbg(f"[NTFY] HTTP error sending notification for {streamer}: "
                f"status={e.code} reason={e.reason!r} body={err_body!r} url={full_url!r}")
        except urllib.error.URLError as e:
            dbg(f"[NTFY] Failed to send notification for {streamer}: "
                f"reason={e.reason!r} url={full_url!r}")
        except Exception as e:
            dbg(f"[NTFY] Unexpected error sending notification for {streamer}: "
                f"{type(e).__name__}: {e} (url={full_url!r})")

    dbg(f"[NTFY] spawning background send thread 'ntfy-{streamer}'")
    threading.Thread(target=_post, daemon=True, name=f"ntfy-{streamer}").start()


def _resolve_ntfy_enabled(app: "AppState", streamer: str, site_label: str, cfg: dict) -> bool:
    """Resolve whether the ntfy channel is enabled for *streamer*.

    Per-streamer entries in global.json's "priorities" section can override
    the site's NTFY_NOTIFICATIONS setting on a per-streamer basis (set via
    the dashboard's NotificationSettingsPopup). Falls back to the site
    config's ntfy_notifications value when no such override exists.
    """
    streamer_notif = None
    try:
        with app.global_json_lock:
            gdata = app.load_global_json()
        config_id = app.get_config_id()
        entries = gdata.get("priorities", {}).get(config_id, {}).get("entries", [])
        dbg(f"[NTFY] config_id={config_id!r} found {len(entries)} priority entries "
            f"for this config set")
        for e in entries:
            if e.get("streamer") == streamer and e.get("site") == site_label:
                streamer_notif = e.get("notifications_enabled")
                break
        dbg(f"[NTFY] streamer-level override lookup result: {streamer_notif!r} "
            f"(None means no per-streamer entry / fall back to site config)")
    except Exception as ex:
        dbg(f"[NTFY] Error reading global.json for streamer-level setting: "
            f"{type(ex).__name__}: {ex}")

    if streamer_notif is not None:
        dbg(f"[NTFY] using streamer-level override: notifications_enabled={streamer_notif}")
        return bool(streamer_notif)

    enabled = cfg.get("ntfy_notifications", True)
    dbg(f"[NTFY] no streamer-level override; using site config "
        f"ntfy_notifications={enabled}")
    return bool(enabled)


def _maybe_show_live_popup(app: "AppState", streamer: str, cfg: dict, site: "SiteState",
                           show_popup: bool = True, source: str = "poll",
                           is_recording: bool = True, reason: str = "",
                           warning: str = "", confirmed: bool = False) -> None:
    """Single gatekeeper for both live-notification channels (popup + ntfy).

    Both channels represent the same underlying event, so they follow the
    exact same path here: resolve whether each channel is enabled, then
    apply one shared per-session-suppression check and one shared
    POPUP_COOLDOWN window that gates *both* channels identically. Only the
    per-channel enablement check differs (POPUP_NOTIFICATIONS vs.
    NTFY_NOTIFICATIONS / its per-streamer override).
    """
    site_label = cfg.get("site_label", os.path.basename(site.config_path))

    dbg(f"[NOTIFY] source={source!r} streamer={streamer!r} is_recording={is_recording} "
        f"confirmed={confirmed} (NOTIFY_CONFIRM_FILE gate: {'confirmed-file notification' if confirmed else 'not a confirm_file notification'})")

    popup_enabled = show_popup and cfg.get("popup_notifications", True)
    dbg(f"[NOTIFY] popup channel: show_popup={show_popup} "
        f"popup_notifications={cfg.get('popup_notifications', True)} -> enabled={popup_enabled}")

    ntfy_enabled = _resolve_ntfy_enabled(app, streamer, site_label, cfg)
    dbg(f"[NOTIFY] ntfy channel: enabled={ntfy_enabled}")

    if not popup_enabled and not ntfy_enabled:
        dbg(f"[NOTIFY] both channels disabled for streamer={streamer!r}; nothing to do")
        return

    # Streamers that are NOT being recorded (disabled / lower-priority) get
    # re-passed to this function on every single poll for as long as they
    # remain live, since nothing else about their state changes to exclude
    # them from the caller's candidate list. Relying on the cooldown alone
    # then means notifications keep re-appearing every popup_cooldown
    # minutes for the entire time they're live. Instead, only notify once
    # per continuous live session; this resets automatically when the
    # streamer's LiveSession is torn down on mark_offline(). This applies
    # to both channels identically.
    if not is_recording and site.was_notif_shown(streamer):
        dbg(f"[NOTIFY] suppressed - already shown this live session for streamer={streamer!r} reason={reason!r}")
        return

    cooldown_secs = cfg.get("popup_cooldown", 30) * 60
    last_shown    = site.notif_last_shown.get(streamer, 0)
    elapsed       = time.time() - last_shown
    if elapsed < cooldown_secs:
        dbg(f"[NOTIFY] suppressed by cooldown for streamer={streamer!r} elapsed={elapsed:.1f}s required={cooldown_secs}s")
        return

    dbg(f"[NOTIFY] allowed by cooldown for streamer={streamer!r} elapsed={elapsed:.1f}s cooldown={cooldown_secs}s; "
        f"dispatching enabled channels (popup={popup_enabled} ntfy={ntfy_enabled})")

    # Same disk info shown on the dashboard's system panel, computed once
    # and shared by both channels.
    disk_info = _get_disk_info_string(cfg)

    if popup_enabled:
        _show_live_popup(streamer, source=source,
                         popup_timeout=cfg.get("popup_timeout", 15),
                         is_recording=is_recording,
                         reason=reason,
                         warning=warning,
                         site_label=site_label,
                         disk_info=disk_info,
                         confirmed=confirmed)

    if ntfy_enabled:
        _send_ntfy_notification(streamer, site_label,
                                is_recording=is_recording,
                                reason=reason,
                                warning=warning,
                                disk_info=disk_info,
                                confirmed=confirmed)

    site.notif_last_shown[streamer] = time.time()
    if not is_recording:
        site.mark_notif_shown(streamer)


# ══════════════════════════════════════════════════════════════════════════════
# Config file editor
# ══════════════════════════════════════════════════════════════════════════════

def _modify_config_streamer(config_path: str, username: str, action: str) -> str:
    username = username.strip().lower()
    if not username:
        return "No username provided."

    # Locked so the scheduler, web UI, and management overlay can't race
    # each other's read-modify-write of this file.
    with get_config_file_lock(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            dbg(f"_modify_config_streamer: {e}")
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
            dbg(f"_add_to_section: {e}")
            return f"ERROR writing config: {e}"

        return "  ".join(messages)


# ══════════════════════════════════════════════════════════════════════════════
# yt-dlp subprocess helpers
# ══════════════════════════════════════════════════════════════════════════════

def open_log_streams(cfg: dict, streamer: str):
    log_out_fp = log_err_fp = None
    if cfg.get("logging"):
        out_path, err_path = get_log_file_paths(cfg, streamer)
        try:
            dir_part = os.path.dirname(out_path)
            if dir_part:
                os.makedirs(dir_part, exist_ok=True)
        except Exception as e:
            dbg(f"open_log_streams: {e}")
            pass
        try:
            log_out_fp = open(out_path, "a", encoding="utf-8")
        except Exception as e:
            dbg(f"open_log_streams: {e}")
            pass
        try:
            log_err_fp = log_out_fp if err_path == out_path else open(err_path, "a", encoding="utf-8")
        except Exception as e:
            dbg(f"open_log_streams: {e}")
            pass

    def _close():
        for fp in {log_out_fp, log_err_fp}:
            try:
                if fp is not None:
                    fp.close()
            except Exception as e:
                dbg(f"_close: {e}")
                pass

    return subprocess.PIPE, subprocess.PIPE, _close, log_out_fp, log_err_fp


def _drain_pipe(app: "AppState", pipe, log_fp, pipe_type: str,
                ffmpeg_error_counter=None, ffmpeg_error_event=None,
                streamer: str = "", site: Optional[SiteState] = None,
                ad_alert_pattern=None) -> None:
    """Drain one pipe (stdout or stderr) from a yt-dlp subprocess."""
    dbg(f"[DRAIN] thread started pipe_type={pipe_type!r} streamer={streamer!r} pipe={pipe!r}")
    line_count = 0

    def _read_chunk() -> bytes:
        # read1() returns as soon as *any* data is available from the OS
        # pipe buffer (at most one underlying read syscall), instead of
        # blocking until a full line — that's essential here since yt-dlp's
        # progress updates end in a bare '\r' with no '\n' at all. Iterating
        # the pipe object directly (`for raw in pipe`) uses readline()
        # under the hood, which only splits on '\n' and would sit blocked
        # for the entire download, never surfacing progress updates until
        # something else finally wrote a real newline (or the process
        # exited and flushed everything at once).
        if hasattr(pipe, "read1"):
            return pipe.read1(4096)
        return pipe.read(4096)

    try:
        buf = b""
        while True:
            chunk = _read_chunk()
            if not chunk:
                break
            buf += chunk

            # Emit every complete line/update as soon as its terminator
            # ('\r', '\n', or '\r\n') shows up in the buffer.
            while True:
                idx_r = buf.find(b"\r")
                idx_n = buf.find(b"\n")
                if idx_r == -1 and idx_n == -1:
                    break
                if idx_r == -1:
                    idx, sep_len = idx_n, 1
                elif idx_n == -1:
                    idx, sep_len = idx_r, 1
                elif idx_r < idx_n:
                    idx = idx_r
                    sep_len = 2 if idx_n == idx_r + 1 else 1   # collapse '\r\n'
                else:
                    idx, sep_len = idx_n, 1

                raw_line = buf[:idx]
                buf = buf[idx + sep_len:]

                # On Windows, yt-dlp.exe writes stdout in the active code
                # page (e.g. cp1252), not UTF-8. Use the locale encoding so
                # that cp1252-representable non-ASCII characters (accents,
                # etc.) survive; emoji/CJK that can't be represented are
                # already replaced with '?' by yt-dlp itself and are
                # unrecoverable from stdout — the sidecar technique
                # handles those.
                line = raw_line.decode(encoding=locale.getpreferredencoding(), errors="replace")
                if not line:
                    continue

                line_count += 1
                if line_count <= 3:
                    dbg(f"[DRAIN] pipe_type={pipe_type!r} streamer={streamer!r} line#{line_count}: {line[:200]!r}")

                if log_fp is not None:
                    try:
                        log_fp.write(line + "\n")
                        log_fp.flush()
                    except Exception as e:
                        dbg(f"_read_chunk: {e}")
                        pass
                if site is not None:
                    if pipe_type == "stdout":
                        site.add_stdout_line(line, streamer=streamer)
                    elif pipe_type == "stderr":
                        site.add_stderr_line(line, streamer=streamer)
                if (ffmpeg_error_counter is not None and ffmpeg_error_event is not None
                        and app.ffmpeg_error_restart_threshold > 0 and not ffmpeg_error_event.is_set()):
                    line_lower = line.lower()
                    for pattern in FFMPEG_ERROR_PATTERNS:
                        if pattern.lower() in line_lower:
                            ffmpeg_error_counter[0] += 1
                            if site is not None and streamer:
                                site.set_ffmpeg_error_count(streamer, ffmpeg_error_counter[0])
                            if ffmpeg_error_counter[0] >= app.ffmpeg_error_restart_threshold:
                                ffmpeg_error_event.set()
                            break

                if ad_alert_pattern is not None and site is not None and streamer:
                    if ad_alert_pattern.search(line):
                        site.update_ad_alert(streamer)
                        dbg(f"[AD] signal detected streamer={streamer!r} "
                            f"pipe={pipe_type!r}: {line[:120]!r}",
                            site_name=streamer)

        # EOF — flush any trailing partial line that never got a terminator.
        if buf:
            line = buf.decode(encoding=locale.getpreferredencoding(), errors="replace")
            if line:
                line_count += 1
                if log_fp is not None:
                    try:
                        log_fp.write(line + "\n")
                        log_fp.flush()
                    except Exception as e:
                        dbg(f"_read_chunk: {e}")
                        pass
                if site is not None:
                    if pipe_type == "stdout":
                        site.add_stdout_line(line, streamer=streamer)
                    elif pipe_type == "stderr":
                        site.add_stderr_line(line, streamer=streamer)
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
            first, second = int(m.group(1)), int(m.group(2))
            # Normally "WIDTHxHEIGHT" and the smaller number (the height)
            # is the meaningful "Xp" quality figure. Portrait streams (e.g.
            # TikTok, commonly "720x1280") report width < height, so the
            # first number is actually the smaller/quality-relevant one.
            # Just take the smaller of the two either way.
            return min(first, second)

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
    # [● Live] ↔ [DIS]. Recording is suppressed downstream in
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

    # Surface a broken checker command on the dashboard's Log tab. Without
    # this, something like a missing browser-cookies database makes the
    # checker fail on every single streamer, every cycle, forever — but
    # get_live_streamers() just returns an empty {} either way, so the
    # dashboard looks identical to "nobody is live" and the failure never
    # shows up anywhere the user is likely to look.
    if site is not None and result.stderr:
        _err_lower = result.stderr.lower()
        if any(pat in _err_lower for pat in _CHECKER_HARD_ERROR_PATTERNS):
            _err_first_line = next(
                (ln.strip() for ln in result.stderr.splitlines() if ln.strip()), ""
            )[:300]
            if _err_first_line != site._last_checker_error:
                site._last_checker_error = _err_first_line
                site.log_line(
                    f"[!] CHECKER FAILED — liveness checks are not working: {_err_first_line}"
                )
                if "cookies database" in _err_lower or "could not find" in _err_lower:
                    browser = str(load_config(site.config_path).get("browser", "firefox")).strip().lower()
                    if browser and browser != "disabled":
                        site.log_line(
                            f"[!] Fix: open {browser} & ensure you are logged in to the site(s), or "
                            f"edit {os.path.basename(site.config_path)} and set BROWSER to a different "
                            'browser (or set COOKIES_FROM_BROWSER = false in [Checker]/[Downloader] to '
                            "disable cookies entirely)."
                        )
                    else:
                        site.log_line(
                            f"[!] Fix: edit {os.path.basename(site.config_path)} and set BROWSER to a "
                            "valid browser."
                        )
        elif site._last_checker_error is not None:
            site._last_checker_error = None
            site.log_line("Checker command is working again.")

    if cfg["logging"]:
        checker_path = get_checker_log_path(cfg)
        try:
            if result.stdout:
                dir_part = os.path.dirname(checker_path)
                if dir_part:
                    os.makedirs(dir_part, exist_ok=True)
                with open(checker_path, "a", encoding="utf-8") as _lf:
                    _lf.write(result.stdout)
        except Exception as e:
            dbg(f"get_live_streamers: {e}")
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
                except Exception as e:
                    dbg(f"get_live_streamers: {e}")
                    streamer = url.rstrip("/").split("/")[-1].lstrip("@").lower().strip()
                if streamer:
                    live[streamer] = _extract_resolution_height(info)
        except Exception as e:
            dbg(f"get_live_streamers: {e}")
            pass
    return live


def _resolve_ffprobe_path(app: "AppState") -> Optional[str]:
    """Locate the ffprobe binary, reusing ffmpeg's already-verified location."""
    if app.ffprobe_path_cache is not None:
        return app.ffprobe_path_cache or None

    found, ffmpeg_path = check_ffmpeg()
    if found and ffmpeg_path:
        ffmpeg_dir = os.path.dirname(ffmpeg_path)
        ffmpeg_base = os.path.basename(ffmpeg_path)
        probe_base = ffmpeg_base.replace("ffmpeg", "ffprobe")
        candidate = os.path.join(ffmpeg_dir, probe_base)
        if os.path.isfile(candidate):
            app.ffprobe_path_cache = candidate
            dbg(f"[QUALITY] resolved ffprobe next to ffmpeg: {candidate!r}")
            return candidate

    which_result = shutil.which("ffprobe")
    if which_result:
        app.ffprobe_path_cache = which_result
        dbg(f"[QUALITY] resolved ffprobe via PATH: {which_result!r}")
        return which_result

    app.ffprobe_path_cache = False
    dbg("[QUALITY] ffprobe not found (checked next to ffmpeg and on PATH)")
    return None


def probe_file_height(app: "AppState", filepath: str) -> Optional[int]:
    """Return the actual video height (px) of *filepath* via ffprobe, or
    None on any failure (ffprobe missing, file missing, timeout, bad/empty
    output, etc). Callers should treat None as "couldn't determine it" and
    simply not display a quality — never raise.

    Only reads the file's stream headers (not the whole file), so this is
    cheap to call even while the file is actively being written to.
    """
    if not filepath or not os.path.isfile(filepath):
        return None

    ffprobe_path = _resolve_ffprobe_path(app)
    if not ffprobe_path:
        return None

    cmd = [
        ffprobe_path, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        filepath,
    ]
    try:
        _run_kwargs: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=5)
        if sys.platform == "win32":
            _run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(cmd, **_run_kwargs)
        out = (result.stdout or "").strip()
        if not out:
            return None
        # First line, fields "width,height" — ffprobe can print multiple
        # lines if the container somehow has more than one video stream.
        first_line = out.splitlines()[0].strip()
        parts = first_line.split(",")
        width = int(parts[0])
        height = int(parts[1])
        # Use the smaller dimension as the "Xp" quality figure. Landscape
        # video stores width > height, so height is normally the right
        # figure; portrait video (e.g. TikTok, stored 720x1280) has
        # width < height, so width is the meaningful one there.
        result_px = min(width, height)
        return result_px if result_px > 0 else None
    except Exception as e:
        dbg(f"[QUALITY] ffprobe probe failed for {filepath!r}: {type(e).__name__}: {e}")
        return None


def get_streamer_file_size(output_dir, streamer, cfg=None,
                           last_growth_time=None, stall_timeout=None,
                           stall_check_interval=None, proc_start_time=None,
known_filename=None):
    try:
        filename = known_filename
        size = os.path.getsize(filename) if filename else 0
        size = _simulation.maybe_freeze_stall_size(
            filename, size, last_growth_time, stall_timeout, streamer)
        # last_growth_time is only passed non-None by the caller once
        # growth_seen has flipped true (see the call site's comment), so it
        # doubles as the "has this file shown real growth yet" signal the
        # collapse simulation needs — no separate growth_seen param required.
        size = _simulation.maybe_collapse_stall_size(
            filename, size, last_growth_time is not None, streamer)
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
                _simulation.maybe_latch_stall_permanent(streamer)
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


def _scan_directory_for_active_file(output_dir: str, streamer: str,
                                    proc_start_time: Optional[float] = None,
                                    growth_wait: float = 2.0) -> Optional[str]:
    """Last-ditch scan for the file yt-dlp is writing, used when the filename
    sidecar never resolved a path. Matches by streamer name only (not an
    exact filename), then confirms the match is actively growing before
    returning it."""
    dbg(f"[STALL] directory scan: checking {output_dir!r} for streamer={streamer!r}")
    if not os.path.isdir(output_dir):
        return None
    candidates = []
    for root, _dirs, files in os.walk(output_dir):
        for fname in files:
            if streamer.lower() not in fname.lower():
                continue
            fpath = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(fpath)
            except OSError:
                continue
            if proc_start_time is not None and mtime < proc_start_time - 5:
                continue
            candidates.append(fpath)
    if not candidates:
        return None
    dbg(f"[STALL] directory scan: {len(candidates)} candidate(s) match "
        f"streamer={streamer!r}", site_name=streamer)
    # Cap and prioritize by recency so a huge/ambiguous match set (a busy
    # shared OUTPUT_DIR, an overlapping name substring) doesn't turn into an
    # unbounded scan or an arbitrary pick — the real file is always among
    # the most recently touched.
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    candidates = candidates[:50]
    sizes_before = {}
    for fpath in candidates:
        try:
            sizes_before[fpath] = os.path.getsize(fpath)
        except OSError:
            pass
    time.sleep(growth_wait)
    grown = []
    for fpath in candidates:
        try:
            size_after = os.path.getsize(fpath)
        except OSError:
            continue
        if size_after > sizes_before.get(fpath, -1):
            grown.append(fpath)
    if not grown:
        return None
    if len(grown) > 1:
        dbg(f"[STALL] directory scan: {len(grown)} candidates are all growing "
            f"for streamer={streamer!r} — picking the most recently modified",
            site_name=streamer)
    # Already sorted newest-first, so the first growing entry is the best pick.
    return grown[0]

def _launch_lq_recording(app: "AppState", streamer: str, cfg: dict, site: "SiteState",
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
    site.mark_live(streamer)

    site.log_line(f"LQ recording starting for {streamer}")
    dbg(f"[LQ] Launching LQ record_stream for {streamer}")
    t = threading.Thread(
        target=record_stream,
        args=(app, streamer, cfg, site),
        kwargs={"use_lq": True},
        daemon=True,
        name=f"lq-rec-{streamer}",
    )
    t.start()
    site.recording_threads.append(t)


def _maybe_trigger_lq(app: "AppState", triggering_site: "SiteState", triggering_streamer: str) -> None:
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
    for s in app.sites:
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
    with app.global_json_lock:
        global_data = app.load_global_json()
    config_id = app.get_config_id()
    saved_entries = (global_data.get("priorities", {})
                                .get(config_id, {})
                                .get("entries", []))
    priority_map: Dict[Tuple[str, str], dict] = {}
    for e in saved_entries:
        key = (e.get("streamer", ""), e.get("site", ""))
        priority_map[key] = {
            "priority":      e.get("priority", 999999),
            "bypass":        e.get("bypass", False),
            "split_mode":    e.get("split_mode"),        # None = inherit (or legacy data)
            "split_enabled": e.get("split_enabled", False),  # legacy fallback
            "split_after":   e.get("split_after", 0),
            "auto_suffix_mode": e.get("auto_suffix_mode"),  # None = inherit
        }

    # ── Condition 2: find eligible candidates ─────────────────────────────────
    candidates = []
    for s in app.sites:
        try:
            s_cfg = s.get_cached_config()
        except Exception as e:
            dbg(f"_maybe_trigger_lq: {e}")
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
            with app.lq_attempted_lock:
                attempt_ts = app.lq_attempted.get(key, 0.0)
            if now - attempt_ts < _LQ_RECENT_WINDOW:
                dbg(f"[LQ] Skipping {st} — LQ attempted {now - attempt_ts:.0f}s ago (window={_LQ_RECENT_WINDOW}s)")
                continue
            candidates.append({
                "streamer":  st,
                "site":      s,
                "site_label": s_label,
                "priority":  info.get("priority", 999999),
                "cfg":       _resolve_auto_suffix(_resolve_split_after(s_cfg, info), info),
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
    with app.lq_attempted_lock:
        app.lq_attempted[(tgt_str, tgt_label)] = now

    # Evict the current recording.
    tgt_site.evict_and_restart(tgt_str)

    # Launch the LQ restart in a background thread (waits for eviction to clear).
    threading.Thread(
        target=_launch_lq_recording,
        args=(app, tgt_str, tgt_cfg, tgt_site, tgt_label),
        daemon=True,
        name=f"lq-launch-{tgt_str}",
    ).start()


def _refresh_restart_anchor_if_growing(site: "SiteState", streamer: str,
                                        growth_seen: bool, reason: str) -> None:
    """Refresh last_restart_anchor for an in-loop self-restart (stall
    recovery, ffmpeg-error threshold, or any future restart branch).

    Only refreshes when growth_seen is True — otherwise this attempt never
    confirmed a write, and NOTIFY_NO_CONFIRM_FILE should still fire.
    In-loop self-restarts only; external evictions should use
    SiteState.evict_and_restart() instead.
    """
    if not growth_seen:
        return
    site.set_last_restart_anchor(streamer, time.time())
    dbg(f"[RESTART_ANCHOR] refreshed for {streamer!r} (reason={reason})",
        site_name=streamer)


def _resolve_split_after(cfg: dict, entry_info: dict) -> dict:
    """Return a cfg dict to use for a single streamer, applying that
    streamer's per-streamer Split override (set via the SPLIT settings
    popup) on top of the site's SPLIT_AFTER config value.

    entry_info is the priorities[...][entries] dict-like info for the
    streamer. Three states, driven by "split_mode":
      - "inherit" (or key absent) — no override; the site's SPLIT_AFTER is
        left untouched and the *same* cfg object is returned (no copy).
      - "on"  — override with entry_info["split_after"] minutes (only
        applied if > 0).
      - "off" — force splitting off for this streamer regardless of the
        site's SPLIT_AFTER.
    For backward compatibility with data written by the older two-state
    popup (no "split_mode" key), an entry with "split_enabled": true and a
    positive "split_after" is treated the same as "on"; anything else is
    treated as "inherit" (there was no way to force splitting off before).

    The override never affects other streamers sharing the same site config.
    """
    if not entry_info:
        return cfg
    try:
        split_after = int(entry_info.get("split_after", 0) or 0)
    except (TypeError, ValueError):
        split_after = 0

    mode = entry_info.get("split_mode")
    if mode not in ("on", "off"):
        # Legacy fallback (pre-tri-state data).
        legacy_enabled = bool(entry_info.get("split_enabled", False))
        mode = "on" if (legacy_enabled and split_after > 0) else "inherit"

    if mode == "inherit":
        return cfg
    if mode == "off":
        overridden = dict(cfg)
        overridden["split_after"] = 0
        return overridden
    # mode == "on"
    if split_after <= 0:
        return cfg
    overridden = dict(cfg)
    overridden["split_after"] = split_after
    return overridden


def _resolve_auto_suffix(cfg: dict, entry_info: dict) -> dict:
    """Return a cfg dict to use for a single streamer, applying that
    streamer's per-streamer Auto-Suffix override (set via the Auto-Suffix
    settings popup) on top of the site's AUTO_SUFFIX config value.

    Mirrors _resolve_split_after()'s tri-state handling. entry_info is the
    priorities[...][entries] dict-like info for the streamer, driven by
    "auto_suffix_mode":
      - "inherit" (or key absent) — no override; the site's AUTO_SUFFIX is
        left untouched and the *same* cfg object is returned (no copy).
      - "on"  — force AUTO_SUFFIX on for this streamer.
      - "off" — force AUTO_SUFFIX off for this streamer.

    The override never affects other streamers sharing the same site config.
    """
    if not entry_info:
        return cfg
    mode = entry_info.get("auto_suffix_mode")
    if mode not in ("on", "off"):
        return cfg
    overridden = dict(cfg)
    overridden["auto_suffix"] = (mode == "on")
    return overridden


def _resolve_output_dir(cfg: dict, entry_info: dict) -> dict:
    """Return a cfg dict to use for a single streamer, applying that
    streamer's per-streamer Output Directory override (set via the Output
    Directory settings popup) on top of the site's OUTPUT_DIR / global
    SUBFOLDERS config values.

    entry_info is the priorities[...][entries] dict-like info for the
    streamer. Two independent overrides, both optional:

      - "output_dir_mode" — "inherit" (or key absent) leaves the global
        SUBFOLDERS mode untouched; any of ("streamer-only", "site-only",
        "streamer-site", "site-streamer", "off") forces that nesting mode
        for this streamer via cfg["subfolders_mode_override"], which
        record_stream() prefers over the global SUBFOLDERS setting when
        present.
      - "output_dir_custom_enabled" (bool) + "output_dir_custom_path" —
        when enabled and non-empty, replaces cfg["output_dir"] for this
        streamer only, before the subfolder nesting is applied.

    The override never affects other streamers sharing the same site
    config, since a copy of cfg is only made when there's something to
    override (mirrors _resolve_split_after() / _resolve_auto_suffix()).
    """
    if not entry_info:
        return cfg
    mode = entry_info.get("output_dir_mode")
    custom_enabled = bool(entry_info.get("output_dir_custom_enabled", False))
    custom_path = str(entry_info.get("output_dir_custom_path", "") or "").strip()

    if mode not in ("streamer-only", "site-only", "streamer-site", "site-streamer", "off") \
            and not (custom_enabled and custom_path):
        return cfg

    overridden = dict(cfg)
    if mode in ("streamer-only", "site-only", "streamer-site", "site-streamer", "off"):
        overridden["subfolders_mode_override"] = mode
    if custom_enabled and custom_path:
        if not os.path.isabs(custom_path):
            custom_path = os.path.abspath(custom_path)
        overridden["output_dir"] = custom_path
    return overridden


def _resolve_intro_delay(cfg: dict, entry_info: dict) -> dict:
    """Return a cfg dict to use for a single streamer, applying that
    streamer's per-streamer Intro Delay override (set via the SETTINGS
    popup) on top of *cfg*.
    """
    if not entry_info or not entry_info.get("intro_delay_enabled", False):
        return cfg
    try:
        minutes = int(entry_info.get("intro_delay_minutes", 0) or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return cfg
    overridden = dict(cfg)
    overridden["intro_delay_enabled"] = True
    overridden["intro_delay_minutes"] = minutes
    overridden["intro_delay_split"] = bool(entry_info.get("intro_delay_split", False))
    return overridden


# ══════════════════════════════════════════════════════════════════════════
# record_stream() helpers
#
# record_stream() used to be a single ~1230-line function. The pieces below
# are straight-line "extract method" splits of that function: each one is a
# self-contained step (attempt setup, process launch, teardown, ...) with no
# behavior changes. The two nested loops that actually drive an attempt
# (retry/restart, and the per-second stall/split check) are left in
# record_stream() itself, since their break/continue/return statements are
# what make them loops in the first place.
# ══════════════════════════════════════════════════════════════════════════

def _resolve_recording_output_dir(cfg: dict, streamer: str) -> str:
    """Apply the SUBFOLDERS mode (or per-streamer override) to OUTPUT_DIR and create it."""
    output_dir = cfg["output_dir"]
    _global_cfg_rs = load_global_config()
    _subfolders_mode = cfg.get("subfolders_mode_override") or _global_cfg_rs.get("subfolders", "off")
    _site_label = cfg.get("site_label", "")
    if _subfolders_mode == "streamer-only":
        output_dir = os.path.join(output_dir, streamer)
    elif _subfolders_mode == "site-only":
        output_dir = os.path.join(output_dir, _site_label)
    elif _subfolders_mode == "streamer-site":
        output_dir = os.path.join(output_dir, streamer, _site_label)
    elif _subfolders_mode == "site-streamer":
        output_dir = os.path.join(output_dir, _site_label, streamer)
    # "off" (or any unrecognized value) leaves output_dir unchanged.
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def _init_continuity_settings(cfg: dict) -> tuple:
    """Resolve SPLIT_AFTER/AUTO_SUFFIX into (split_after_minutes, split_after_seconds, auto_suffix_enabled, continuity_active)."""
    split_after_minutes = max(0, cfg.get("split_after", 0))
    split_after_seconds = split_after_minutes * 60
    auto_suffix_enabled = bool(cfg.get("auto_suffix", True))
    continuity_active = (split_after_minutes > 0) or auto_suffix_enabled
    return split_after_minutes, split_after_seconds, auto_suffix_enabled, continuity_active


def _apply_intro_delay(app: "AppState", streamer: str, cfg: dict, site: "SiteState",
                        show_popup: bool, eviction_warning: str,
                        notify_confirm_file: bool,
                        split_after_minutes: int, split_after_seconds: int):
    """Block for INTRO_DELAY if configured. Returns None if the streamer was
    stopped/evicted during the wait (caller must return immediately without
    touching the finally-block bookkeeping, matching the original inline
    behavior), otherwise (split_after_minutes, split_after_seconds,
    intro_delay_disable_after_split, no_confirm_grace_seconds)."""
    intro_delay_enabled = bool(cfg.get("intro_delay_enabled", False))
    intro_delay_minutes = max(0, cfg.get("intro_delay_minutes", 0))
    intro_delay_split = bool(cfg.get("intro_delay_split", False))
    intro_delay_disable_after_split = False
    no_confirm_grace_seconds = 0.0

    if intro_delay_enabled and intro_delay_minutes > 0 and intro_delay_split:
        split_after_minutes = intro_delay_minutes
        split_after_seconds = split_after_minutes * 60
        intro_delay_disable_after_split = True
        dbg(f"[INTRO_DELAY] streamer={streamer!r} split mode: first split forced "
            f"at {intro_delay_minutes}m, further splitting disabled afterward")
    elif intro_delay_enabled and intro_delay_minutes > 0:
        _intro_delay_anchor = site.get_live_since(streamer) or time.time()
        _delay_deadline = _intro_delay_anchor + intro_delay_minutes * 60
        _delay_remaining = max(0.0, _delay_deadline - time.time())
        dbg(f"[INTRO_DELAY] streamer={streamer!r} delay mode: configured "
            f"{intro_delay_minutes}m from live-since, "
            f"{_delay_remaining:.0f}s remaining")
        with site.lock:
            site.intro_delay_pending.add(streamer)
        while time.time() < _delay_deadline:
            if site._stop_event.is_set() or streamer in site.evicted_streamers:
                dbg(f"[INTRO_DELAY] streamer={streamer!r} aborted during delay "
                    f"(stop_event or evicted)")
                with site.lock:
                    site.currently_recording.discard(streamer)
                    site.intro_delay_pending.discard(streamer)
                return None
            site._stop_event.wait(timeout=min(1.0, _delay_deadline - time.time()))
        with site.lock:
            site.intro_delay_pending.discard(streamer)
        no_confirm_grace_seconds = intro_delay_minutes * 60
        dbg(f"[INTRO_DELAY] streamer={streamer!r} delay elapsed — starting recording")
        site.notif_last_shown[streamer] = 0
        if not notify_confirm_file:
            _maybe_show_live_popup(app, streamer, cfg, site, show_popup=show_popup,
                                   source="intro_delay", is_recording=True,
                                   warning=eviction_warning)

    return split_after_minutes, split_after_seconds, intro_delay_disable_after_split, no_confirm_grace_seconds


def _record_lq_attempt(app: "AppState", streamer: str, cfg: dict, site: "SiteState", use_lq: bool) -> None:
    """Record an LQ attempt immediately so a re-entrant LQ trigger can't target this streamer again."""
    if not use_lq:
        return
    _lq_site_label = cfg.get("site_label", os.path.basename(site.config_path))
    with app.lq_attempted_lock:
        app.lq_attempted[(streamer, _lq_site_label)] = time.time()
    dbg(f"[LQ] LQ attempt recorded for {streamer} on {_lq_site_label}")


def _resume_segment_continuity(site: "SiteState", streamer: str, continuity_active: bool,
                                segment_num: int, pending_rename_file):
    """Pick up cross-thread AUTO_SUFFIX/SPLIT_AFTER continuity, if any."""
    if continuity_active:
        _cont = site.get_segment_continuation(streamer)
        if _cont and _cont[0] > 1:
            segment_num, pending_rename_file = _cont
            dbg(f"[SPLIT][record_stream] resuming cross-thread continuity for "
                f"streamer={streamer!r}: segment_num={segment_num} "
                f"pending_rename={pending_rename_file!r}")
    return segment_num, pending_rename_file


def _bump_segment_for_inplace_restart(site: "SiteState", streamer: str,
                                       segment_num: int, active_file):
    """Advance segment_num for an in-thread restart (ffmpeg-error/stall/normal-exit) and persist it."""
    _prev_segment_num = segment_num
    segment_num = _prev_segment_num + 1
    pending_rename_file = active_file if (_prev_segment_num == 1 and active_file) else None
    dbg(f"[SPLIT][record_stream] in-thread restart continuity for "
        f"streamer={streamer!r}: segment_num {_prev_segment_num} -> "
        f"{segment_num} pending_rename={pending_rename_file!r}")
    site.set_segment_continuation(streamer, segment_num, pending_rename_file)
    return segment_num, pending_rename_file


def _build_segment_output_paths(cfg: dict, output_dir: str, continuity_active: bool, segment_num: int):
    """Resolve this attempt's output template/path, adding the _partN suffix if applicable."""
    current_output_tmpl = cfg["output_tmpl"]
    # For segment 1 we intentionally omit the _part1 suffix — it will be
    # retroactively added (via rename) only if a second part is ever created.
    if continuity_active and segment_num > 1:
        current_output_tmpl = add_segment_suffix_to_tmpl(current_output_tmpl, segment_num)
    output_path = _simulation.get_write_failure_output_path(output_dir, current_output_tmpl)
    return current_output_tmpl, output_path


def _select_downloader_cmd(cfg: dict, use_lq: bool, streamer: str) -> list:
    """Pick the LQ downloader command when requested, falling back to the normal one."""
    active_dl_cmd = cfg["downloader_cmd"]
    if use_lq:
        _lq_cmd = cfg.get("lq_downloader_cmd", [])
        if _lq_cmd:
            active_dl_cmd = _lq_cmd
        else:
            dbg(f"[LQ] use_lq=True but lq_downloader_cmd is empty — falling back to normal downloader for {streamer}")
    return active_dl_cmd


def _build_sidecar_path(output_dir: str, streamer: str) -> str:
    """Build a unique per-attempt path for yt-dlp's --print-to-file resolved-filename sidecar."""
    _sidecar_token = _re.sub(r"[^A-Za-z0-9_.-]", "_", streamer) or "streamer"
    return os.path.join(output_dir, f".jjdlp_filename_{_sidecar_token}_{uuid.uuid4().hex}.tmp")


def _launch_yt_dlp_attempt(app: "AppState", cmd: list, out_target, err_target, close_logs,
                            log_out_fp, log_err_fp,
                            cfg: dict, site: "SiteState", streamer: str):
    """Popen the downloader and start its stdout/stderr drain threads.
    Returns None on failure (already logged and close_logs() called), else
    (proc, proc_start_time, ffmpeg_error_counter, ffmpeg_error_event)."""
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
        ffmpeg_error_event = threading.Event()
        site.clear_ffmpeg_error_count(streamer)
        site.clear_stall_since(streamer)

        threading.Thread(
            target=_drain_pipe,
            args=(app, proc.stdout, log_out_fp, "stdout"),
            kwargs={
                "ffmpeg_error_counter": ffmpeg_error_counter,
                "ffmpeg_error_event": ffmpeg_error_event,
                "streamer": streamer,
                "site": site,
                "ad_alert_pattern": _compile_ad_alert_pattern(cfg),
            },
            daemon=True
        ).start()

        threading.Thread(
            target=_drain_pipe,
            args=(app, proc.stderr, log_err_fp, "stderr"),
            kwargs={
                "ffmpeg_error_counter": ffmpeg_error_counter,
                "ffmpeg_error_event": ffmpeg_error_event,
                "streamer": streamer,
                "site": site,
                "ad_alert_pattern": _compile_ad_alert_pattern(cfg),
            },
            daemon=True
        ).start()

        return proc, proc_start_time, ffmpeg_error_counter, ffmpeg_error_event

    except Exception as e:
        site.log_line(f"Failed to start yt-dlp for {streamer}: {e}")
        try:
            close_logs()
        except Exception as e2:
            dbg(f"_launch_yt_dlp_attempt: close_logs() failed for {streamer!r}: {e2}")
        return None


def _resolve_active_recording_file(proc, sidecar_path: str, output_dir: str, streamer: str):
    """3-tier fallback to find the file yt-dlp is actually writing to:
    1. the --print-to-file sidecar (UTF-8, avoids console encoding issues),
    2. (handled live elsewhere via _drain_pipe), 3. left to the caller.
    Waits up to 15s for the sidecar, then up to 5s for the resolved file to
    appear on disk. Returns the path, or None if it couldn't be resolved."""
    _FILENAME_WAIT_TIMEOUT = 15.0
    active_file = None
    _sidecar_deadline = time.time() + _FILENAME_WAIT_TIMEOUT
    while time.time() < _sidecar_deadline:
        if os.path.isfile(sidecar_path):
            try:
                with open(sidecar_path, "r", encoding="utf-8") as _sf:
                    # --print-to-file appends rather than overwrites, so
                    # the sidecar may have multiple lines (retries,
                    # format re-selection, and/or multiple timings).
                    # Scan from bottom to top, preferring absolute
                    # paths (%(filepath)s result) over relative ones
                    # (raw output-template result).
                    _sc_lines = [ln.strip() for ln in _sf.read().splitlines() if ln.strip()]
                raw_abs = None
                raw_rel = ""
                for ln in reversed(_sc_lines):
                    if not ln or ln == "NA":
                        continue
                    if os.path.isabs(ln):
                        raw_abs = ln  # prefer absolute (%(filepath)s)
                    else:
                        if not raw_rel:
                            raw_rel = ln  # keep first (last) relative
                raw_dest = raw_abs if raw_abs is not None else raw_rel
            except Exception as _sc_read_err:
                dbg(f"[STALL] error reading filename sidecar {sidecar_path!r}: "
                    f"{_sc_read_err!r}", site_name=streamer)
                raw_dest = ""
            # The sidecar has served its purpose the moment we've read
            # it — remove it right away rather than waiting for
            # end-of-recording cleanup.
            try:
                os.remove(sidecar_path)
            except OSError:
                pass

            if raw_dest:
                if os.path.isabs(raw_dest):
                    candidate = raw_dest
                else:
                    # This is the raw output template result (e.g.
                    # from "%(uploader)s %(title)s ...").  yt-dlp
                    # resolves the template but does NOT apply
                    # filesystem sanitization — that happens later
                    # when the file is created.  Replicate the
                    # Windows sanitization here so the candidate
                    # path matches the actual file on disk.
                    _sanitized = _re.sub(r'[<>:"/\\|?*]', '_', raw_dest)
                    _sanitized = _re.sub(r'[\x00-\x1f\x7f]', '', _sanitized)
                    _sanitized = _sanitized.strip(' .')
                    candidate = os.path.join(output_dir, _sanitized)
                # The sidecar is written at before_dl, but the actual
                # output file may not exist yet (yt-dlp opens it
                # slightly after the hook fires).  Wait a few seconds
                # for the file to appear before giving up.
                _file_deadline = time.time() + 5.0
                while time.time() < _file_deadline:
                    if os.path.exists(candidate):
                        active_file = candidate
                        dbg(f"[STALL] resolved active_file from filename sidecar: "
                            f"{active_file!r}", site_name=streamer)
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.25)
                if not active_file:
                    dbg(f"[STALL] sidecar candidate {candidate!r} (from {raw_dest!r}) "
                        f"does not exist after 5s, discarding.", site_name=streamer)
            break

        if proc.poll() is not None:
            # Process already exited without ever writing the sidecar —
            # no point waiting out the rest of the timeout.
            dbg(f"[STALL] proc exited before writing filename sidecar "
                f"(returncode={proc.returncode})", site_name=streamer)
            break

        time.sleep(0.25)
    else:
        dbg(f"[STALL] timed out after {_FILENAME_WAIT_TIMEOUT}s waiting for "
            f"filename sidecar {sidecar_path!r}", site_name=streamer)

    # Best-effort cleanup: if we broke out early (timeout / proc exit)
    # before the sidecar ever appeared, make sure nothing is left behind.
    if not active_file:
        try:
            if os.path.isfile(sidecar_path):
                os.remove(sidecar_path)
        except OSError:
            pass

    return active_file


def _publish_active_file_and_finalize_rename(site: "SiteState", streamer: str, active_file,
                                              pending_rename_file, segment_num: int):
    """Publish active_file for the disk-rate graph and, if a prior attempt's
    file is still waiting on a retroactive _partN rename, apply it now that
    this attempt's file has resolved. Returns the (possibly cleared)
    pending_rename_file."""
    if not active_file:
        return pending_rename_file

    site.set_recording_output(streamer, active_file)

    if (pending_rename_file and pending_rename_file != active_file
            and os.path.isfile(pending_rename_file)):
        _prev_part_path = add_segment_suffix_to_tmpl(pending_rename_file, segment_num - 1)
        try:
            os.rename(pending_rename_file, _prev_part_path)
            site.log_line(f"Renamed previous segment to: {os.path.basename(_prev_part_path)}")
            dbg(f"[SPLIT][record_stream] renamed continuation segment: "
                f"{pending_rename_file!r} -> {_prev_part_path!r}")
        except Exception as _ren_err:
            dbg(f"[SPLIT][record_stream] rename of continuation segment "
                f"FAILED: {_ren_err!r}")
        pending_rename_file = None

    return pending_rename_file


def _compute_no_confirm_deadline(site: "SiteState", streamer: str, stall_timeout: float,
                                  no_confirm_grace_seconds: float) -> float:
    """Compute the NOTIFY_NO_CONFIRM_FILE deadline for this attempt (see original
    comment: anchored to live-since/enable-anchor/last-restart-anchor, floored
    at this process's own start time, plus any Intro Delay grace period)."""
    _no_confirm_anchor_val = site.get_enable_anchor(streamer)
    _no_confirm_anchor_src = "enable_anchor"
    if _no_confirm_anchor_val is None:
        _no_confirm_anchor_val = site.get_live_since(streamer) or time.time()
        _no_confirm_anchor_src = "live_since"
    _last_restart_anchor = site.get_last_restart_anchor(streamer)
    if _last_restart_anchor is not None and _last_restart_anchor > _no_confirm_anchor_val:
        _no_confirm_anchor_val = _last_restart_anchor
        _no_confirm_anchor_src = "last_restart_anchor"
    if _no_confirm_anchor_val < _SCRIPT_START_TIME:
        dbg(f"[NOTIFY] NOTIFY_NO_CONFIRM_FILE: {_no_confirm_anchor_src} "
            f"({_no_confirm_anchor_val:.2f}) predates this process's start "
            f"({_SCRIPT_START_TIME:.2f}) — flooring anchor at process start "
            f"for streamer={streamer!r}",
            site_name=streamer)
        _no_confirm_anchor_val = _SCRIPT_START_TIME
        _no_confirm_anchor_src += "+floored_at_process_start"
    _no_confirm_deadline = _no_confirm_anchor_val + stall_timeout + no_confirm_grace_seconds
    dbg(f"[NOTIFY] NOTIFY_NO_CONFIRM_FILE: confirmation deadline for "
        f"streamer={streamer!r} = {_no_confirm_anchor_src}+{stall_timeout}s"
        f"{f'+{no_confirm_grace_seconds:.0f}s intro-delay grace' if no_confirm_grace_seconds else ''} "
        f"({_no_confirm_deadline:.2f}), live_since={site.get_live_since(streamer)}, "
        f"enable_anchor={site.get_enable_anchor(streamer)}",
        site_name=streamer)
    return _no_confirm_deadline


def _teardown_attempt(site: "SiteState", streamer: str, proc, close_logs, *,
                       kill: bool = True, wait_after_kill: bool = False,
                       clear_stall: bool = False, clear_ffmpeg_error: bool = False,
                       clear_ad_alert: bool = False) -> None:
    """End a recording attempt: optionally kill+wait the process, unregister
    it, clear the requested per-streamer flags, and close the log files.
    Every normal-path recording teardown in record_stream funnels through
    here so the cleanup can't drift between call sites."""
    if kill and proc is not None:
        kill_proc(proc)
        if wait_after_kill:
            proc.wait()
    site.unregister_proc(streamer)
    if clear_stall:
        site.clear_stall_since(streamer)
    if clear_ffmpeg_error:
        site.clear_ffmpeg_error_count(streamer)
    if clear_ad_alert:
        site.clear_ad_alert(streamer)
    try:
        close_logs()
    except Exception as e:
        dbg(f"_teardown_attempt: close_logs() failed for {streamer!r}: {e}")


def _update_measured_quality(app: "AppState", site: "SiteState", streamer: str, active_file, file_error: bool) -> None:
    """Measure the on-disk resolution via ffprobe and publish it for the dashboard."""
    if file_error:
        with site.lock:
            site.display_resolution.pop(streamer, None)
        dbg(f"[QUALITY] file_error={file_error!r} - clearing measured resolution", site_name=streamer)
        return
    _measured_height = probe_file_height(app, active_file)
    with site.lock:
        _prev_measured = site.display_resolution.get(streamer)
        if _measured_height is not None:
            site.display_resolution[streamer] = _measured_height
        else:
            site.display_resolution.pop(streamer, None)
    dbg(f"[QUALITY] measured_height={_measured_height!r}p "
        f"(prev={_prev_measured!r}p) recording_resolution_baseline={site.recording_resolution.get(streamer)!r}p "
        f"file={active_file!r}", site_name=streamer)


def _spawn_and_verify_split_segment(app: "AppState", next_cmd: list, cfg: dict, next_out_target, next_err_target,
                                     next_log_out_fp, next_log_err_fp,
                                     output_dir: str, streamer: str, site: "SiteState", part_suffix: str):
    """Launch the next segment's yt-dlp process, wait for its exact output
    file to appear, then confirm it's growing. Returns
    (next_proc, next_proc_start_time, next_file, split_success)."""
    _next_popen_kwargs: dict = dict(stdout=next_out_target, stderr=next_err_target)
    if sys.platform != "win32":
        # Same process-group isolation as the primary Popen.
        _next_popen_kwargs["start_new_session"] = True
    dbg(f"[POPEN] streamer={streamer!r} split cmd={cmd_display_str(next_cmd)!r}")
    next_proc = subprocess.Popen(next_cmd, **_next_popen_kwargs)

    next_proc_start_time = time.time()
    dbg(f"[SPLIT][record_stream] next_proc started pid={next_proc.pid} "
        f"next_proc_start_time={next_proc_start_time:.3f}")

    threading.Thread(
        target=_drain_pipe,
        args=(app, next_proc.stdout, next_log_out_fp, "stdout"),
        kwargs={
            "streamer": streamer,
            "site": site,
            "ad_alert_pattern": _compile_ad_alert_pattern(cfg),
        },
        daemon=True
    ).start()

    threading.Thread(
        target=_drain_pipe,
        args=(app, next_proc.stderr, next_log_err_fp, "stderr"),
        kwargs={
            "streamer": streamer,
            "site": site,
            "ad_alert_pattern": _compile_ad_alert_pattern(cfg),
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

    return next_proc, next_proc_start_time, next_file, split_success


def _clear_lq_attempt_on_offline(app: "AppState", streamer: str, cfg: dict, site: "SiteState") -> None:
    """Clear the LQ-attempted marker once a streamer goes offline, so the
    next live session is eligible for the normal downloader again."""
    _offline_site_label = cfg.get("site_label", os.path.basename(site.config_path))
    with app.lq_attempted_lock:
        app.lq_attempted.pop((streamer, _offline_site_label), None)


def _update_last_live_cache(app: "AppState", site: "SiteState", streamer: str) -> None:
    """Record that this streamer just finished a live session, for the last-live cache."""
    with site.dash_lock:
        site.dash_last_live[streamer] = time.time()
        _last_live_snapshot = dict(site.dash_last_live)
    _save_last_live_cache(app, site.config_path, _last_live_snapshot)


def record_stream(app: "AppState", streamer: str, cfg: dict, site: "SiteState",
                  use_lq: bool = False, show_popup: bool = True,
                  eviction_warning: str = "") -> None:
    channel_url = cfg["site_tmpl"].format(username=streamer)
    output_dir = _resolve_recording_output_dir(cfg, streamer)

    split_after_minutes, split_after_seconds, auto_suffix_enabled, continuity_active = \
        _init_continuity_settings(cfg)

    _global_cfg_nc = load_global_config()
    notify_confirm_file = _global_cfg_nc.get("notify_confirm_file", True)
    notify_no_confirm_file = _global_cfg_nc.get("notify_no_confirm_file", False)
    initial_notification_sent = not notify_confirm_file
    dbg(f"[NOTIFY] NOTIFY_CONFIRM_FILE={notify_confirm_file} NOTIFY_NO_CONFIRM_FILE={notify_no_confirm_file} "
        f"for streamer={streamer!r} — "
        f"initial_notification_sent={initial_notification_sent} "
        f"({'live notification already fired, nothing held back' if initial_notification_sent else 'live notification held until file growth is confirmed'})",
        site_name=streamer)

    # Intro Delay may block for a while, and returns None if the streamer
    # was stopped/evicted during the wait — mirrors the original inline
    # `return` (before the try/finally below), so the finally-block
    # bookkeeping is deliberately skipped here too, same as before.
    _intro_result = _apply_intro_delay(
        app, streamer, cfg, site, show_popup, eviction_warning, notify_confirm_file,
        split_after_minutes, split_after_seconds)
    if _intro_result is None:
        return
    split_after_minutes, split_after_seconds, _intro_delay_disable_after_split, _no_confirm_grace_seconds = _intro_result

    dbg(f"[SPLIT][record_stream] ENTER streamer={streamer!r} "
        f"split_after_minutes={split_after_minutes} split_after_seconds={split_after_seconds} "
        f"output_dir={output_dir!r}")

    site.log_line(f"Recording started: {streamer}" + (" [LQ]" if use_lq else ""))

    # Record the attempt immediately so that re-entrant LQ triggers during this
    # session cannot target this streamer again (even if the proc hasn't opened yet).
    _record_lq_attempt(app, streamer, cfg, site, use_lq)

    proc = None
    close_logs = lambda: None
    segment_num = 1
    active_file = None   # pre-declared so the `finally` block can always
                          # reference it, even if the thread exits before the
                          # first outer-loop iteration ever sets it.
    # Backoff for split attempts: after a failed split (e.g. couldn't find/
    # confirm the new segment file), don't retry every second — wait a bit
    # so a persistent problem doesn't spawn a fresh yt-dlp probe process
    # every loop iteration. 0.0 means "no cooldown in effect yet".
    _split_retry_cooldown_seconds = 60.0
    next_split_retry_time = 0.0

    # If a previous record_stream() attempt for this same live session (a
    # different thread call — e.g. before an eviction) left off mid-way
    # through a part sequence, pick up where it left off instead of
    # starting back at part 1. See SiteState.get_segment_continuation().
    _pending_rename_file = None
    segment_num, _pending_rename_file = _resume_segment_continuity(
        site, streamer, continuity_active, segment_num, _pending_rename_file)

    _outer_iteration = 0

    try:
        while True:
            if site._stop_event.is_set() or streamer in site.evicted_streamers:
                break

            _outer_iteration += 1
            if _outer_iteration > 1 and continuity_active:
                # Restarting within this same thread (ffmpeg-error threshold,
                # stall recovery, or a normal yt-dlp exit while still live) —
                # this new attempt is another part of the same recording.
                segment_num, _pending_rename_file = _bump_segment_for_inplace_restart(
                    site, streamer, segment_num, active_file)

            current_output_tmpl, output_path = _build_segment_output_paths(
                cfg, output_dir, continuity_active, segment_num)

            _active_dl_cmd = _select_downloader_cmd(cfg, use_lq, streamer)

            # yt-dlp writes the resolved filename into this sidecar file (in
            # UTF-8) via --print-to-file, so we never have to reconstruct the
            # filename from possibly-mangled stdout text (e.g. emoji/CJK
            # characters getting corrupted by console code-page issues on
            # Windows). One unique file per attempt, written to OUTPUT_DIR,
            # deleted as soon as we've read it.
            _sidecar_path = _build_sidecar_path(output_dir, streamer)

            cmd = build_yt_dlp_command(
                cfg["yt_dlp_path"],
                _active_dl_cmd,
                ["-o", output_path,
                 "--print-to-file", "before_dl:%(filepath)s", _sidecar_path,
                 "--print-to-file", "%(filepath)s", _sidecar_path,
                 "--print-to-file", current_output_tmpl, _sidecar_path,
                 channel_url]
            )
            cmd = _simulation.maybe_strip_sidecar_args(cmd, _sidecar_path, streamer)

            out_target, err_target, close_logs, log_out_fp, log_err_fp = open_log_streams(cfg, streamer)

            _launch_result = _launch_yt_dlp_attempt(
                app, cmd, out_target, err_target, close_logs, log_out_fp, log_err_fp,
                cfg, site, streamer)
            if _launch_result is None:
                break
            proc, proc_start_time, ffmpeg_error_counter, ffmpeg_error_event = _launch_result

            # Resolve the file this attempt is actually writing to (sidecar-based,
            # with a bounded wait) and publish/finalize any pending rename.
            active_file = _resolve_active_recording_file(proc, _sidecar_path, output_dir, streamer)
            _pending_rename_file = _publish_active_file_and_finalize_rename(
                site, streamer, active_file, _pending_rename_file, segment_num)

            # Simulation hook: optionally inject a wrong filename so that
            # jj‑dlp looks for a non‑existent file while yt‑dlp writes normally.
            active_file = _simulation.maybe_inject_wrong_filename(active_file, streamer)

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
            # NOTIFY_NO_CONFIRM_FILE tracking: see _compute_no_confirm_deadline()
            # for the anchor-selection reasoning.
            _no_confirm_deadline = _compute_no_confirm_deadline(
                site, streamer, stall_timeout, _no_confirm_grace_seconds)
            _no_confirm_warned = False

            def _check_no_confirm_deadline():
                # Fires the NOTIFY_NO_CONFIRM_FILE warning as soon as the
                # deadline has passed, independent of stall_check_interval
                # and independent of whether proc is still alive when it's
                # called. This must NOT be gated behind a "proc survived a
                # full stall_check_interval" condition: _SIMULATE_WRITE_FAILURE
                # (and real write failures) can make yt-dlp exit in well
                # under stall_check_interval on every retry, which previously
                # meant this check was never reached at all.
                nonlocal _no_confirm_warned, active_file
                # FIX: guard so this function never fires for a streamer that's
                # being evicted or the app is shutting down, no matter which
                # call site invokes it.
                if site._stop_event.is_set() or streamer in site.evicted_streamers:
                    return
                if (notify_no_confirm_file
                        and not _no_confirm_warned
                        and not growth_seen
                        and time.time() >= _no_confirm_deadline):
                    _no_confirm_warned = True

                    if not active_file:
                        _scanned_file = _scan_directory_for_active_file(
                            output_dir, streamer, proc_start_time)
                        if _scanned_file:
                            active_file = _scanned_file
                            site.set_recording_output(streamer, active_file)
                            site.log_line(
                                f"Info: located recording file for {streamer} via "
                                f"directory scan: {os.path.basename(active_file)}"
                            )
                            dbg(f"[STALL] directory scan recovered active_file="
                                f"{active_file!r}", site_name=streamer)
                            return

                    if active_file:
                        _nc_size, _, _, _nc_file_error = get_streamer_file_size(
                            output_dir, streamer, cfg=cfg,
                            proc_start_time=proc_start_time,
                            known_filename=active_file,
                        )
                    else:
                        _nc_size, _nc_file_error = 0, True
                    dbg(f"[NOTIFY] NOTIFY_NO_CONFIRM_FILE: file not confirmed for "
                        f"streamer={streamer!r} within {int(stall_timeout)}s "
                        f"(deadline={_no_confirm_deadline:.2f}) — sending warning; "
                        f"attempt_age={time.time() - recording_start_time:.1f}s "
                        f"active_file={active_file!r} last_size={last_size} "
                        f"cur_size={_nc_size} file_error={_nc_file_error} "
                        f"growth_seen={growth_seen}",
                        site_name=streamer)
                    _maybe_show_live_popup(
                        app, streamer, cfg, site, show_popup=show_popup,
                        source="no_confirm_file", is_recording=False,
                        warning=(f"The recording file could not be confirmed within "
                                 f"{int(stall_timeout)}s — the start may have failed."),
                        confirmed=False)
                    _log_filename = os.path.basename(active_file) if active_file else "<unknown>"
                    site.log_line(
                        f"Warning: {_log_filename} could not be confirmed within "
                        f"{int(stall_timeout)} seconds."
                    )
                    # Also drives the dashboard's full-screen recording-
                    # failure alert (see App.draw_write_failure_alert).
                    site.flag_write_failure(streamer)

            seconds_since_check  = 0
            _split_log_counter   = 0  # throttle periodic split-timer dbg lines
            # Whether we've ever observed this file grow. The stall checker is
            # only allowed to run (and potentially restart the recording) once
            # growth has actually been seen at least once.
            growth_seen = False
            # Set once we've already warned the Log tab about a missing/unreadable
            # recording file, so we don't spam the same warning every stall-check
            # cycle. Reset whenever the file is found again or a new attempt starts.
            filename_error_warned = False
            dbg(f"[STALL] init: stall_timeout={stall_timeout}s "
                f"stall_check_interval={stall_check_interval}s "
                f"last_size={last_size} last_growth_time={last_growth_time:.2f} "
                f"growth_seen={growth_seen}",
                site_name=streamer)

            dbg(f"[SPLIT][record_stream] inner loop starting: streamer={streamer!r} "
                f"segment_num={segment_num} pid={proc.pid} "
                f"split_after_seconds={split_after_seconds} "
                f"stall_check_interval={stall_check_interval} stall_timeout={stall_timeout}")

            while proc.poll() is None:

                if site._stop_event.is_set() or streamer in site.evicted_streamers:
                    _teardown_attempt(site, streamer, proc, close_logs, wait_after_kill=True)
                    return

                _t0 = time.time()
                current_cfg = site.get_cached_config()
                _load_cfg_ms = (time.time() - _t0) * 1000
                if _split_log_counter % 30 == 0:
                    dbg(f"[PERF][record_stream/inner] get_cached_config took {_load_cfg_ms:.2f}ms streamer={streamer!r}")

                if streamer in current_cfg["blocked"]:
                    # NOTE: deliberately mirrors the stop_event/evicted branch
                    # above — kill and return immediately, and let the shared
                    # `finally` block below do currently_recording.discard(),
                    # the AUTO_SUFFIX/SPLIT_AFTER set_segment_continuation()
                    # write, and the single cooldown wait, in that order.
                    site.log_line(f"Recording STOPPED (blocked) -> {streamer}")
                    _teardown_attempt(site, streamer, proc, close_logs,
                                       clear_stall=True, clear_ffmpeg_error=True)
                    return

                # LQ-restart simulation (DEBUG): injects simulated ffmpeg-error
                # counts so the real LQ machinery can be exercised without a
                # genuinely degraded stream. See _SIMULATE_LQ_RESTART up top.
                # Placed just before the event check below so a simulated
                # threshold hit is caught on the very next loop iteration.
                _simulation._maybe_simulate_lq_errors(
                    streamer, site, use_lq, growth_seen,
                    ffmpeg_error_counter, ffmpeg_error_event,
                    app.ffmpeg_error_restart_threshold)

                if ffmpeg_error_event.is_set():
                    site.log_line(f"ffmpeg error threshold reached for {streamer} — restarting")

                    # Same reasoning as stall-detected below: this attempt
                    # was recording, so give the next attempt's deadline a
                    # fresh window instead of a stale live_since/enable_anchor.
                    _refresh_restart_anchor_if_growing(
                        site, streamer, growth_seen, reason="ffmpeg_error_threshold")

                    _teardown_attempt(site, streamer, proc, close_logs, clear_ad_alert=True)

                    # LQ bandwidth-saving trigger (non-LQ recordings only):
                    # only trigger LQ for normal recordings; if a LQ recording
                    # itself hits the threshold we just let it restart normally.
                    if not use_lq:
                        _maybe_trigger_lq(app, site, streamer)

                    time.sleep(5)
                    break

                manual_split = False
                with site.lock:
                    if streamer in site.manual_split_requests:
                        site.manual_split_requests.discard(streamer)
                        manual_split = True

                if split_after_seconds > 0 or manual_split:
                    elapsed = time.time() - recording_start_time
                    _split_log_counter += 1
                    if _split_log_counter % 30 == 0:  # log roughly every 30s
                        dbg(f"[SPLIT][record_stream] split timer: streamer={streamer!r} "
                            f"segment={segment_num} elapsed={elapsed:.1f}s / "
                            f"split_after_seconds={split_after_seconds}s "
                            f"remaining={max(0, split_after_seconds - elapsed):.1f}s")

                    if manual_split or (
                            elapsed >= split_after_seconds
                            and time.time() >= next_split_retry_time):
                        if manual_split:
                            site.log_line(
                                f"Manual split requested for {streamer} — starting part {segment_num + 1}"
                            )
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

                        dbg(f"[SPLIT][record_stream] SPLIT_AFTER reached for {streamer} — "
                            f"starting part {next_segment_num}")

                        next_cmd = build_yt_dlp_command(
                            cfg["yt_dlp_path"],
                            cfg["downloader_cmd"],
                            ["-o", next_output_path, channel_url]
                        )

                        next_out_target, next_err_target, next_close_logs, next_log_out_fp, next_log_err_fp = open_log_streams(cfg, streamer)

                        try:
                            part_suffix = f"_part{next_segment_num}"
                            next_proc, next_proc_start_time, next_file, split_success = _spawn_and_verify_split_segment(
                                app, next_cmd, cfg, next_out_target, next_err_target,
                                next_log_out_fp, next_log_err_fp,
                                output_dir, streamer, site, part_suffix)

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
                                        dbg(f"[SPLIT][record_stream] renamed first segment to: "
                                            f"{os.path.basename(_part1_path)} "
                                            f"({active_file!r} -> {_part1_path!r})")
                                    except Exception as _ren_err:
                                        dbg(f"[SPLIT][record_stream] rename of first segment FAILED: "
                                            f"{_ren_err!r}")

                                site.unregister_proc(streamer)
                                try:
                                    close_logs()
                                except Exception as e:
                                    dbg(f"record_stream: close_logs() failed for {streamer!r} during split switch: {e}")

                                proc = next_proc
                                close_logs = next_close_logs
                                proc_start_time = next_proc_start_time
                                active_file = next_file
                                # Republish the switched-to segment as the
                                # active recording output so the disk-rate
                                # graph follows the new file (the previous
                                # segment has stopped growing).
                                if active_file:
                                    site.set_recording_output(streamer, active_file)
                                # Use next_proc_start_time (not time.time()) so the
                                # split timer accounts for time already spent verifying
                                # the new file. time.time() here would let each segment
                                # silently overrun SPLIT_AFTER by the verification delay.
                                recording_start_time = next_proc_start_time
                                segment_num = next_segment_num
                                # Persist immediately rather than waiting for this thread's finally: block.
                                site.set_segment_continuation(streamer, segment_num, None)

                                if _intro_delay_disable_after_split:
                                    split_after_seconds = 0
                                    _intro_delay_disable_after_split = False
                                    dbg(f"[INTRO_DELAY] streamer={streamer!r} intro split "
                                        f"confirmed — splitting disabled for remainder of stream")

                                site.register_proc(streamer, proc)

                                ffmpeg_error_counter = [0]
                                ffmpeg_error_event   = threading.Event()
                                site.clear_ffmpeg_error_count(streamer)
                                site.clear_stall_since(streamer)

                                last_size = 0
                                last_growth_time = time.time()
                                next_split_retry_time = 0.0
                                # New segment is a new file — it hasn't grown yet,
                                # so the stall checker must re-earn the right to run.
                                growth_seen = False
                                # Re-arm the NOTIFY_NO_CONFIRM_FILE deadline for
                                # the new segment.
                                _no_confirm_deadline = time.time() + stall_timeout
                                _no_confirm_warned   = False

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
                            except Exception as e:
                                dbg(f"record_stream: next_close_logs() failed for {streamer!r} after failed split: {e}")

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

                # Checked every second (not gated behind stall_check_interval)
                # so a fast-failing recording attempt can't prevent this from
                # ever firing. See _check_no_confirm_deadline() above.
                _check_no_confirm_deadline()

                if seconds_since_check >= stall_check_interval:
                    seconds_since_check = 0
                    dbg(f"[STALL] check cycle: elapsed_since_growth="
                        f"{time.time() - last_growth_time:.2f}s growth_seen={growth_seen}",
                        site_name=streamer)
                    current_size, stall_detected, _, file_error = get_streamer_file_size(
                        output_dir,
                        streamer,
                        cfg=cfg,
                        proc_start_time=proc_start_time,
                        # Only arm the stall checker once this file has grown at
                        # least once. get_streamer_file_size() only computes
                        # stall_detected when both last_growth_time and
                        # stall_timeout are provided, so withholding stall_timeout
                        # here is what keeps the checker from starting on a file
                        # that has never shown growth.
                        last_growth_time=last_growth_time if growth_seen else None,
                        stall_timeout=stall_timeout if growth_seen else None,
                        stall_check_interval=stall_check_interval,
                        known_filename=active_file,
                    )

                    # Dashboard quality display (independent of stall logic):
                    # measure the actual on-disk resolution via ffprobe, reusing
                    # the same active_file the stall checker just used above.
                    _update_measured_quality(app, site, streamer, active_file, file_error)

                    if file_error:
                        # We couldn't even locate/read the recording file this
                        # cycle (e.g. active_file points at a filename that
                        # doesn't exist). Note: This only fires if yt-dlp stays alive
                        # for the duration of STALL_TIMEOUT without producing a
                        # file.
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

                        # Growth was already confirmed (that's what makes
                        # this a "stall" not a never-confirmed start), so
                        # give the next attempt's deadline a fresh window.
                        _refresh_restart_anchor_if_growing(
                            site, streamer, growth_seen, reason="stall_detected")

                        _teardown_attempt(site, streamer, proc, close_logs,
                                           clear_stall=True, clear_ad_alert=True)
                        time.sleep(5)
                        break

                    elif current_size < last_size and growth_seen:
                        # File size went BACKWARDS since the last poll — the
                        # file was truncated/reopened (e.g. yt-dlp reopening
                        # the output file from byte 0 after a live-stream
                        # reconnect instead of resuming/appending).
                        dbg(f"[STALL] COLLAPSE DETECTED: size dropped "
                            f"{last_size} -> {current_size} "
                            f"(-{last_size - current_size} bytes) — file was "
                            f"likely truncated/reopened on reconnect",
                            site_name=streamer)
                        site.log_line(
                            f"Warning: recording file for {streamer} shrank "
                            f"from {last_size} to {current_size} bytes "
                            f"(likely truncated on reconnect) — some footage "
                            f"may have been lost"
                        )
                        # Re-sync the comparison baseline to this poll's size
                        # so the next poll compares against reality. Leave
                        # last_growth_time / stall_since untouched — a shrink
                        # isn't a stall, so it shouldn't affect the genuine
                        # stall timer; if the file also stops growing after
                        # this, the existing NO GROWTH branch will catch that
                        # on its own on a later poll.
                        filename_error_warned = False
                        last_size = current_size

                    elif current_size > last_size:
                        filename_error_warned = False
                        if not growth_seen:
                            growth_seen = True
                            site.clear_last_restart_anchor(streamer)
                            dbg(f"[STALL] first growth observed for this file — "
                                f"stall checker is now armed", site_name=streamer)
                            if not initial_notification_sent:
                                dbg(f"[NOTIFY] NOTIFY_CONFIRM_FILE: file growth confirmed for "
                                    f"streamer={streamer!r} — sending held-back live notification",
                                    site_name=streamer)
                                _maybe_show_live_popup(app, streamer, cfg, site, show_popup=show_popup,
                                                       source="confirm_file", is_recording=True,
                                                       warning=eviction_warning, confirmed=True)
                                initial_notification_sent = True
                        dbg(f"[STALL] grew: {last_size} -> {current_size} "
                            f"(+{current_size - last_size} bytes), resetting timer",
                            site_name=streamer)
                        last_size = current_size
                        last_growth_time = time.time()
                        site.clear_stall_since(streamer)
                    elif not growth_seen:
                        # No growth yet and none has ever been seen for this file —
                        # the stall checker hasn't started, so there's nothing to
                        # flag as stalled. Just wait for the first sign of growth.
                        filename_error_warned = False
                        dbg(f"[STALL] no growth yet, but stall checker not armed "
                            f"(no growth seen for {active_file!r} yet) — skipping stall "
                            f"detection", site_name=streamer)
                        # NOTIFY_NO_CONFIRM_FILE is now checked every second via
                        # _check_no_confirm_deadline() above, not here — see
                        # that function for why it can't be gated behind this
                        # stall_check_interval-only branch.
                    else:
                        filename_error_warned = False
                        dbg(f"[STALL] NO GROWTH: size={current_size} "
                            f"stall_since={time.time() - last_growth_time:.2f}s",
                            site_name=streamer)
                        site.set_stall_since(streamer, last_growth_time)
                        # Re-sync the comparison baseline to this poll's reading.
                        # last_size is a rolling "previous poll" value, not an
                        # all-time high water mark: if it stayed a permanent
                        # ceiling, a single spuriously-low os.path.getsize()
                        # sample (observed under brief heavy CPU/disk load
                        # could permanently poison it.
                        last_size = current_size

            else:
                # FIX: this branch runs whenever the inner loop exits because
                # `proc.poll() is None` was already False the moment the loop
                # condition was (re-)checked - most commonly because another
                # thread killed our process via eviction
                # (kill_proc_for_streamer) *between* our loop iterations,
                # before the loop body ever ran and got a chance
                # to see site.evicted_streamers/_stop_event. That's the exact
                # race that let a freshly-evicted, not-yet-growth-confirmed
                # streamer fall through to the "safety net" below and get
                # flagged as a write failure.
                #
                # Handle that case the same way the loop body already does
                # for eviction/stop (clean teardown, no "recording finished"
                # bookkeeping, no safety-net check) instead of treating it
                # like a normal end-of-attempt.
                if site._stop_event.is_set() or streamer in site.evicted_streamers:
                    _teardown_attempt(site, streamer, proc, close_logs, kill=False,
                                       clear_stall=True, clear_ffmpeg_error=True, clear_ad_alert=True)
                    return

                # Normal yt-dlp exit (return code 0) is a valid restart
                # trigger too: the monitor loop sees the streamer still live
                # and relaunches almost immediately.
                if active_file:
                    _final_size, _, _, _final_file_error = get_streamer_file_size(
                        output_dir, streamer, cfg=cfg,
                        proc_start_time=proc_start_time,
                        known_filename=active_file,
                    )
                else:
                    _final_size, _final_file_error = 0, True
                _refresh_restart_anchor_if_growing(
                    site, streamer, growth_seen, reason="normal_exit")
                dbg(f"[STALL] attempt ended (normal_exit): streamer={streamer!r} "
                    f"returncode={proc.returncode} active_file={active_file!r} "
                    f"last_size={last_size} final_size={_final_size} "
                    f"file_error={_final_file_error} growth_seen={growth_seen} "
                    f"attempt_duration={time.time() - proc_start_time:.1f}s "
                    f"anchor_refreshed={bool(growth_seen)}",
                    site_name=streamer)

                # Safety net: if proc.poll() was already non-None before the
                # loop got to sleep even once, _check_no_confirm_deadline()
                # above may never have run this attempt. Give it one last
                # chance here before we report the attempt as finished.
                _check_no_confirm_deadline()

                _teardown_attempt(site, streamer, proc, close_logs, kill=False,
                                   clear_stall=True, clear_ffmpeg_error=True, clear_ad_alert=True)

                # Clear LQ tracking when streamer goes offline: this ensures
                # the next time they go live the normal downloader is used
                # (LQ is only attempted once per online session).
                _clear_lq_attempt_on_offline(app, streamer, cfg, site)

                _update_last_live_cache(app, site, streamer)

                site.log_line(f"Recording finished: {streamer}")
                break

    except KeyboardInterrupt:
        if proc is not None:
            try:
                kill_proc(proc)
            except Exception as e:
                dbg(f"record_stream: kill_proc() failed for {streamer!r} during KeyboardInterrupt: {e}")

        site.unregister_proc(streamer)
        site.clear_ad_alert(streamer)

        try:
            close_logs()
        except Exception as e:
            dbg(f"record_stream: close_logs() failed for {streamer!r} during KeyboardInterrupt: {e}")

    finally:
        with site.lock:
            site.currently_recording.discard(streamer)
            # Stop treating this streamer's file as an active recording so
            # the top-bar disk-rate graph drops it the moment recording ends.
            site.recording_output_paths.pop(streamer, None)
            # Clear the UPGRADE_QUALITY baseline along with currently_recording
            # so the next time this streamer starts recording (fresh or
            # restarted-for-quality) gets a clean baseline rather than
            # comparing against a stale resolution from a previous session.
            site.recording_resolution.pop(streamer, None)
            # Clear the ffprobe-measured display quality too, for the same
            # reason
            site.display_resolution.pop(streamer, None)
            site.recording_attempt_started.pop(streamer, None)
            # Always clean up evicted_streamers here so the set doesn't grow
            # unboundedly over the lifetime of the process.  The eviction flag
            # is only meaningful while the recording thread is alive; once we
            # reach this finally block the thread is done regardless of why it
            # stopped (normal end, eviction, or crash).
            site.evicted_streamers.discard(streamer)

        # AUTO_SUFFIX / SPLIT_AFTER restart continuity: persist enough state
        # for a *future* record_stream() attempt (a separate thread call for
        # this same live session — e.g. after an eviction) to continue this
        # part sequence instead of starting back at part 1. A no-op if the
        # live session already ended (streamer went offline) — see
        # SiteState.set_segment_continuation(). segment_num==1 means this
        # attempt's file was never suffixed, so it's the one that would need
        # a retroactive rename if a continuation attempt follows;
        # segment_num>1 means it was already suffixed at creation, so
        # there's nothing pending.
        _unsuffixed_for_continuation = (
            active_file if (active_file and segment_num == 1) else None
        )
        site.set_segment_continuation(
            streamer, segment_num + 1, _unsuffixed_for_continuation
        )

        site.clear_ad_alert(streamer)

        # Interruptible: on shutdown this returns instantly instead of
        # keeping the thread (and is_alive()) reporting "active" for up to
        # cooldown_after_recording seconds after the recording has actually
        # stopped. This is what was inflating the shutdown count and making
        # quit take so long.
        site._stop_event.wait(timeout=cfg["cooldown_after_recording"])


def start_recording_if_needed(app: "AppState", live_now: List[str], cfg: dict, site: "SiteState",
                               show_popup: bool = True, source: str = "poll",
                               resolution_map: Optional[Dict[str, Optional[int]]] = None) -> None:
    with site.lock:
        currently_recording = set(site.currently_recording)
        blocked = set(cfg["blocked"])
        disabled_live = [s for s in live_now
                         if s in blocked and s not in currently_recording]
        to_start = [s for s in live_now
                    if s not in currently_recording and s not in blocked]
    # Remember that these streamers were live while disabled, so that if
    # they're later enabled during the same live session, we know
    # live-since predates the recording start and shouldn't be used as the
    # NOTIFY_NO_CONFIRM_FILE deadline anchor for them.
    for streamer in disabled_live:
        site.mark_blocked_while_live(streamer)

    for streamer in disabled_live:
        _maybe_show_live_popup(app, streamer, cfg, site, show_popup=show_popup,
                               source=source, is_recording=False,
                               reason="Disabled")

    if not to_start:
        site.recording_threads[:] = [t for t in site.recording_threads if t.is_alive()]
        return

    global_cfg = load_global_config()
    max_concurrent = global_cfg.get("max_concurrent_rec", 0)

    with app.global_json_lock:
        global_data = app.load_global_json()

    config_id = app.get_config_id()
    saved_entries = global_data.get("priorities", {}).get(config_id, {}).get("entries", [])
    
    priority_map = {}
    for e in saved_entries:
        s_name = e.get("streamer", "")
        s_site = e.get("site", "")
        priority_map[(s_name, s_site)] = {
            "priority": e.get("priority", 999999),
            "bypass": e.get("bypass", False),
            "lq_enabled": e.get("lq_enabled", False),
            "split_mode": e.get("split_mode"),           # None = inherit (or legacy data)
            "split_enabled": e.get("split_enabled", False),  # legacy fallback
            "split_after": e.get("split_after", 0),
            "intro_delay_enabled": e.get("intro_delay_enabled", False),
            "intro_delay_minutes": e.get("intro_delay_minutes", 0),
            "intro_delay_split": e.get("intro_delay_split", False),
            "auto_suffix_mode": e.get("auto_suffix_mode"),  # None = inherit
            "output_dir_mode": e.get("output_dir_mode"),    # None = inherit
            "output_dir_custom_enabled": e.get("output_dir_custom_enabled", False),
            "output_dir_custom_path": e.get("output_dir_custom_path", ""),
        }

    site_label = cfg.get("site_label", os.path.basename(site.config_path))

    with app.recording_start_lock:
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
            streamer_cfg = _resolve_auto_suffix(streamer_cfg, streamer_info)
            streamer_cfg = _resolve_intro_delay(streamer_cfg, streamer_info)
            streamer_cfg = _resolve_output_dir(streamer_cfg, streamer_info)
            eviction_warning = ""

            # Concurrency enforcement
            # Lock ordering inside this block:
            #   recording_start_lock  (already held by the outer `with`)
            #   -> site.lock / s.lock   (acquired below, released before kill)
            #   -> kill_proc_for_streamer (no locks held during the blocking call)
            #
            # Stale-count window: after kill_proc_for_streamer() returns, the
            # evicted record_stream thread is still alive until its finally block
            # removes the streamer from currently_recording. Both the evicted
            # and the new streamer are briefly in currently_recording. Because
            # recording_start_lock serialises all starts, this window cannot
            # trigger a second eviction cascade.
            if max_concurrent > 0:
                active_recordings = []
                for s in app.sites:
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
                        # Restart timing here is unbounded (could be
                        # minutes/hours until a slot frees up), unlike
                        # LQ/quality-upgrade which restart within seconds
                        target_site.mark_evicted_for_concurrency(target_streamer)
                        target_site.evict_and_restart(target_streamer, refresh_anchor=False)
                        eviction_warning = f"evicted {target_streamer}"

                    elif not is_bypass:
                        dbg(f"[CONCURRENCY] max_concurrent ({max_concurrent}) reached. "
                            f"Streamer {streamer} (prio: {streamer_prio}) cannot evict "
                            f"any active stream.")
                        _maybe_show_live_popup(app, streamer, cfg, site,
                                               show_popup=show_popup,
                                               source=source,
                                               is_recording=False,
                                               reason="Lower priority")
                        site.mark_blocked_while_live(streamer)
                        continue

            with site.lock:
                site.currently_recording.add(streamer)
                site.evicted_streamers.discard(streamer)
                if resolution_map is not None:
                    _start_height = resolution_map.get(streamer)
                    _prior_baseline = site.recording_resolution.get(streamer)
                    dbg(f"[UPGRADE_QUALITY] start_recording_if_needed baseline set: "
                        f"resolution_map_height={_start_height!r}p prior_baseline={_prior_baseline!r}p "
                        f"source={source!r}", site_name=streamer)
                    if _start_height is not None:
                        site.recording_resolution[streamer] = _start_height
                    else:
                        site.recording_resolution.pop(streamer, None)
                site.recording_attempt_started[streamer] = time.time()
            site.mark_live(streamer)
            # Persistent anchor: if this streamer was ever observed
            # live-while-disabled during the current live session, and we
            # haven't already set an anchor for it, set one now (first
            # recording start after enabling). Left in place for every
            # retry for the rest of this live session — live-since itself
            # is never touched or treated as stale.
            if site.was_blocked_while_live(streamer):
                site.set_enable_anchor(streamer, time.time())
                site.clear_blocked_while_live(streamer)
            # Same idea, for the other deferred-restart case: if this
            # streamer was evicted for concurrency at some earlier point,
            # this is the actual restart, so refresh last_restart_anchor
            # to now (not at eviction time, since that gap was unbounded)
            # and consume the flag.
            if site.was_evicted_for_concurrency(streamer):
                site.set_last_restart_anchor(streamer, time.time())
                site.clear_evicted_for_concurrency(streamer)
            _intro_delay_holding = (streamer_cfg.get("intro_delay_enabled", False)
                                     and not streamer_cfg.get("intro_delay_split", False))
            if global_cfg.get("notify_confirm_file", True) and not _intro_delay_holding:
                dbg(f"[NOTIFY] NOTIFY_CONFIRM_FILE enabled — deferring live notification for "
                    f"streamer={streamer!r} until record_stream() confirms file growth")
            else:
                _maybe_show_live_popup(app, streamer, cfg, site,
                                       show_popup=show_popup,
                                       source=source,
                                       is_recording=not _intro_delay_holding,
                                       reason="Intro Delay" if _intro_delay_holding else "",
                                       warning=eviction_warning)
            t = threading.Thread(target=record_stream, args=(app, streamer, streamer_cfg, site), kwargs={"use_lq": is_lq, "show_popup": show_popup, "eviction_warning": eviction_warning}, daemon=True)
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
        except Exception as e:
            dbg(f"config_watcher: {e}")
            pass
        site._stop_event.wait(timeout=poll_interval)


def _process_streamer_schedules(app: "AppState", site: "SiteState") -> None:
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
    config_id = app.get_config_id()
    now       = datetime.now()

    # Read entries outside the write-lock so _modify_config_streamer can run
    # without risk of deadlock (it touches .conf files, not global.json).
    with app.global_json_lock:
        gdata = app.load_global_json()

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
    except Exception as e:
        dbg(f"normalize_label: {e}")
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
            except Exception as e:
                dbg(f"_parse_attempt: {e}")
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
    def _mutate(gdata):
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
    app.update_global_json(_mutate)

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
    # ── Quality-upgrade simulation (DEBUG) ─────────────────────────
    # Fakes a higher checker-reported resolution (via live_info) so the real
    # UPGRADE_QUALITY machinery runs without the source genuinely switching.
    # See _SIMULATE_QUALITY_UPGRADE up top.
    _simulation._maybe_simulate_quality_upgrade(site, live_info)

    with site.lock:
        active = set(site.currently_recording) - site.evicted_streamers
    # Skip streamers that have already been upgraded once this live session.
    active = {s for s in active if not site.was_quality_upgraded(s)}

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
            site.mark_quality_upgraded(streamer)
            site.evict_and_restart(streamer)


def monitor_site(app: "AppState", site: "SiteState") -> None:
    """Main polling loop for a single site — runs in its own thread."""
    try:
        from .twitch_eventsub import TwitchEventSub, EventSubState
        site.eventsub_state = EventSubState()
    except ImportError:
        site.eventsub_state = None

    initial_cfg = load_config(site.config_path)

    if site.eventsub_state is not None and initial_cfg.get("twitch_enabled"):
        def _on_stream_online(broadcaster_login: str, cfg: dict) -> None:
            site.mark_live(broadcaster_login)
            current_cfg = load_config(cfg["config_path"])
            if broadcaster_login in current_cfg.get("streamers", []):
                start_recording_if_needed(app, [broadcaster_login], current_cfg, site,
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

    # Stagger startup liveness checks slightly so the curses UI finishes
    # drawing its initial frames smoothly before external processes launch.
    time.sleep(2.0)

    # Fires at most once per process; the actual 24h throttling is enforced
    # inside maybe_backfill_last_live() via a timestamp persisted in
    # global.json, so a restart within 24h of the last fire is a no-op and
    # a restart after 24h fires again automatically.
    _gql_backfill_done = False

    while not site._stop_event.is_set():
        # Evaluate schedule-based enable/disable for all streamers before the
        # liveness check so any config changes take effect this iteration.
        try:
            _process_streamer_schedules(app, site)
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
            # Diagnostic generation for this reconciliation cycle. Increment
            # before obtaining live_info so every subsequent session log can
            # be tied to one specific checker cycle.
            site._live_check_generation += 1
            _check_generation = site._live_check_generation

            live_info = get_live_streamers(streamers, cfg, site=site)
            live_now  = list(live_info.keys())
            cfg = load_config(site.config_path)

            with site.dash_lock:
                site.dash_all_streamers.clear()
                site.dash_all_streamers.extend(streamers)
                site.dash_blocked.clear()
                site.dash_blocked.update(cfg["blocked"])

            if not _gql_backfill_done:
                _gql_backfill_done = True
                try:
                    maybe_backfill_last_live(
                        site, cfg,
                        get_last_backfill_ts_fn=lambda: _load_last_gql_backfill_ts(app, site.config_path),
                        set_last_backfill_ts_fn=lambda ts: _save_last_gql_backfill_ts(app, site.config_path, ts),
                        save_last_live_fn=lambda cp, ll: _save_last_live_cache(app, cp, ll),
                        dbg_fn=dbg,
                        log_fn=site.log_line,
                    )
                except Exception as e:
                    dbg(f"[GQL] last_live backfill error: {type(e).__name__}: {e}")

            # One call each — mark_offline() tears down the whole
            # LiveSession (quality_upgraded, blocked_while_live,
            # enable_anchor, notif_shown all reset together as a unit),
            # and mark_live() is a no-op if already tracked.
            live_set = set(live_now)

            dbg(
                f"[SESSION] RECONCILE cycle={_check_generation} "
                f"streamers={len(streamers)} live_now={len(live_set)} "
                f"tracked_live={len(site.live_sessions)}",
                site_name=site.label,
            )

            for s in streamers:
                if s not in live_set:
                    _was_tracked = s in site.live_sessions
                    _cache_present = s in site._live_since_cache
                    _last_seen = site._last_seen_live.get(s)

                    dbg(
                        f"[SESSION] RECONCILE cycle={_check_generation} "
                        f"streamer={s!r} result=OFFLINE "
                        f"live_session_present={_was_tracked} "
                        f"live_since_cache_present={_cache_present} "
                        f"last_seen_live={_last_seen!r} "
                        f"last_seen_age={time.time() - _last_seen:.1f}s"
                        if _last_seen is not None
                        else
                        f"[SESSION] RECONCILE cycle={_check_generation} "
                        f"streamer={s!r} result=OFFLINE "
                        f"live_session_present={_was_tracked} "
                        f"live_since_cache_present={_cache_present} "
                        f"last_seen_live=None",
                        site_name=s,
                    )

                    site.mark_offline(s)
                else:
                    dbg(
                        f"[SESSION] RECONCILE cycle={_check_generation} "
                        f"streamer={s!r} result=LIVE "
                        f"live_session_present={s in site.live_sessions} "
                        f"live_since_cache_present={s in site._live_since_cache}",
                        site_name=s,
                    )
                    site.mark_live(s)

            # "Skip this stream" auto re-enable: any skip-disabled streamer
            # that's currently reported offline gets un-blocked, so the
            # disable only lasted for the stream it was applied during.
            #
            # Deliberately NOT gated on having observed a live->offline
            # transition in this process
            # If the streamer was also removed from [Streamers] entirely
            # in the meantime (rather than just left in [Block]), leave it
            # alone — it's a real removal now, not a pending skip.
            with site.dash_lock:
                to_unblock = [s for s in streamers if s not in live_set and s in site.skip_disabled]
            if to_unblock:
                still_in_streamers = set(load_config(site.config_path)["streamers"])
                remaining_skip = set(site.skip_disabled)
                for s in to_unblock:
                    remaining_skip.discard(s)
                    if s in still_in_streamers:
                        _modify_config_streamer(site.config_path, s, "add")
                        site.log_line(f"'{s}' went offline — auto re-enabled (skip-this-stream expired).")
                with site.dash_lock:
                    site.skip_disabled = remaining_skip
                _save_skip_disabled(app, site.config_path, remaining_skip)
                site.invalidate_config_cache()

            if live_now:
                start_recording_if_needed(app, live_now, cfg, site, resolution_map=live_info)

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


def _check_and_kill_zombie_yt_dlps(app: "AppState") -> None:
    with app.global_json_lock:
        gdata = app.load_global_json()
        pids = gdata.get("yt_dlp_pids", [])
    
    if not pids:
        return

    running_pids = []
    for pid in pids:
        try:
            if sys.platform == "win32":
                try:
                    out = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    if str(pid) in out:
                        running_pids.append(pid)
                except Exception as e:
                    dbg(f"_check_and_kill_zombie_yt_dlps: {e}")
                    pass
            else:
                os.kill(pid, 0)
                try:
                    with open(f"/proc/{pid}/cmdline", "r", encoding="utf-8", errors="replace") as _f:
                        cmd = _f.read()
                    if "yt_dlp" in cmd or "yt-dlp" in cmd:
                        running_pids.append(pid)
                except Exception as e:
                    # process might have died or no permission, assume running
                    dbg(f"_check_and_kill_zombie_yt_dlps: {e}")
                    running_pids.append(pid)
        except OSError:
            pass

    if running_pids:
        ans = input(f"Warning: There are {len(running_pids)} child yt-dlp processes still running from a previous instance. Kill them now? [Y/n] ")
        if ans.lower() in ("", "y", "yes"):
            for pid in running_pids:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                else:
                    import signal
                    try:
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass

    def _mutate(gdata):
        gdata["yt_dlp_pids"] = []
    app.update_global_json(_mutate)

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

    app = AppState()

    # Install orphan-process protection as early as possible, before any
    # yt-dlp/ffmpeg process can be spawned. Ensures that no matter how
    # jj-dlp's window/console gets closed (X button, taskkill, logoff,
    # terminal closed, crash), child processes get cleaned up instead of
    # being left running in the background. See _install_shutdown_safety_net
    # for the breakdown of each layer.
    _install_shutdown_safety_net(app)
    _install_thread_excepthook(app)

    # Bundled executables in bin/ lose their execute bit when copied from the
    # GitHub zip.  Fix it on every launch (not just after an update) so the
    # permission is correct even the first time jj-dlp runs after updating.
    ensure_bin_executable()

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

    # ── Refuse to start a second instance ─────────────────────────────────────
    # Must run before _check_and_kill_zombie_yt_dlps(): otherwise a second
    # instance launched while the first is still recording would see the
    # first instance's live yt-dlp children and misreport them as orphaned.
    if not acquire_single_instance_lock(app):
        print(
            f"\njj-dlp v{__version__}  ·  Another instance of jj-dlp appears to be running.\n"
            f"\n"
            f"If this is in error, delete 'jj-dlp.instance.lock' (located in the jj_dlp folder) and try again."
        )
        input("\nPress Enter to close...")
        sys.exit(1)

    _check_and_kill_zombie_yt_dlps(app)

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
        cwd   = os.getcwd()
        configs_dir = os.path.join(cwd, "configs")
        if not os.path.isdir(configs_dir):
            print(f"ERROR: No 'configs/' directory found in {cwd}. "
                  "Pass --config <path> or create a configs/ folder.",
                  file=sys.stderr)
            sys.exit(1)

        found = []
        for f in os.listdir(configs_dir):
            if f.endswith(".conf") and os.path.isfile(os.path.join(configs_dir, f)):
                # global.conf is always loaded silently; never shown in the chooser
                if f == _GLOBAL_CONF_NAME:
                    continue
                rel = os.path.relpath(os.path.join(configs_dir, f), cwd)
                if rel not in found:
                    found.append(rel)
        found.sort()

        if not found:
            print(f"ERROR: No .conf files found in {configs_dir}. "
                  "Pass --config <path> or place a config file in configs/.",
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
            global_data = app.load_global_json()
            saved_configs = global_data.get("startup_configs", [])

            if not ask_for_config and saved_configs and all(c in found for c in saved_configs):
                chosen = saved_configs
            else:
                # Multi-select chooser
                from . import curses_dashboard
                chosen = curses_dashboard.choose_config(app, found)

        config_paths = [os.path.join(cwd, f) for f in chosen]

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

    if any_check:
        dbg(f"[UPDATER] enabled startup checker update_interval={update_interval}")
        startup_available = is_update_available(app)
        dbg(f"[UPDATER] startup read update_available={startup_available}")
        if startup_available:
            with app.update_available_lock:
                app.update_available = True
            # Reset changelog_shown so it will display after the update is applied.
            with app.global_json_lock:
                _gd = app.load_global_json()
                if _gd.get("changelog_shown") is not False:
                    _gd["changelog_shown"] = False
                    app.save_global_json(_gd)
                    dbg("[UPDATER] startup: update available — changelog_shown set to false")
            print("\n[Updater] A new version of jj-dlp is available!")
            ans = _input_with_timeout("[Updater] Do you want to update now? (y/n) [timeout in 10s]: ", timeout_seconds=10)
            if ans == 'y':
                perform_update()
                sys.exit(0)
            elif ans is None:
                print("[Updater] No response received. Continuing with current version.")

        def _periodic_update_checker() -> None:
            while True:
                check_for_updates_background(app)
                new_available = is_update_available(app)
                with app.update_available_lock:
                    prev_available = app.update_available
                    app.update_available = new_available
                dbg(f"[UPDATER] periodic check prev={prev_available} new={new_available}")
                # When an update becomes newly available, reset changelog_shown so it will
                # display to the user after the update is applied.
                if new_available and not prev_available:
                    with app.global_json_lock:
                        _gd = app.load_global_json()
                        _gd["changelog_shown"] = False
                        app.save_global_json(_gd)
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
    _startup_thresh = global_cfg.get("ff_err_thresh", 200)
    if _startup_thresh >= 0:
        app.ffmpeg_error_restart_threshold = _startup_thresh

    # ── Launch per-site state + threads ──────────────────────────────────────
    sites: List[SiteState] = app.sites
    for cp in config_paths:
        site = SiteState(cp, app)
        sites.append(site)

    def _dash_log(msg: str):
        # Global (not per-site) events — e.g. web UI startup/bind-failure
        # announcements and debug-log write errors — so log once, not once
        # per loaded site. See AppState.log_global_line.
        app.log_global_line(msg)

    def _dash_dbg(msg: str):
        """Route a dbg() line to every site's *debug* log buffer.

        This buffer is separate from dash_log_lines (the activity log), so no
        matter how frequently a debug tag fires, it can only evict older debug
        lines — it can never push real activity lines (recording
        started/stopped, errors, etc.) out of the Log tab's history.

        Untagged messages (no leading "[TAG]") are unexpected-exception guards
        that should be impossible in normal operation; when they do fire, also
        append them to dash_log_lines so they surface in the Log tab without
        requiring the user to have the debug buffer visible.
        """
        # msg is always "[YYYY-MM-DD HH:MM:SS] <original>" (22-char timestamp
        # prefix added by dbg() in logger.py).  A tagged message has another
        # "[TAG]" immediately after that prefix; untagged messages don't.
        after_ts = msg[22:] if len(msg) > 22 else ""
        is_tagged = after_ts.startswith("[")
        for s in sites:
            with s.dash_lock:
                s.dash_debug_lines.append(msg)   # deque(maxlen=...) evicts automatically
                if not is_tagged:
                    s.dash_log_lines.append(msg)

    _logger.configure(_dash_log, _dash_dbg)

    # Sort sites by site_order so they appear in the desired positions in the dashboard
    sites.sort(key=lambda s: s.site_order)

    for site in sites:
        # Monitor thread (liveness check loop)
        mt = threading.Thread(target=monitor_site, args=(app, site), daemon=True,
                              name=f"monitor:{site.label}")
        mt.start()
        site.monitor_thread = mt

        # Config watcher thread
        cfg_i = load_config(site.config_path)
        wt = threading.Thread(target=config_watcher,
                              args=(site, cfg_i.get("config_check_interval", 3)),
                              daemon=True, name=f"watcher:{site.label}")
        wt.start()
        site.watcher_thread = wt

    # ── Launch embedded web UI (opt-in; status + add/remove/disable) ──────────
    try:
        from . import http_server as _http_server
        _http_server.start_web_server(
            sites, global_cfg,
            log_fn=_dash_log,
            modify_streamer_fn=_modify_config_streamer,
        )
    except Exception as e:
        msg = f"[WEBUI] failed to start: {type(e).__name__}: {e}"
        startup_dbg(msg)
        _dash_log(msg)

    # ── Launch dashboard ───────────────────────────────────────────────────────
    dashboard = None
    try:
        from . import curses_dashboard
        dashboard = curses_dashboard.run_dashboard(app, global_cfg=global_cfg)

    except KeyboardInterrupt:
        pass
    finally:
        # Persist the disk-rate graph history on any shutdown path (normal
        # quit persists inside run(); this covers Ctrl-C / KeyboardInterrupt).
        if dashboard is not None:
            try:
                dashboard._persist_graph_history()
            except Exception as e:
                dbg(f"_run_dashboard: {e}")
                pass

        for site in sites:
            site.stop()
            if site.eventsub is not None:
                try:
                    site.eventsub.stop(timeout=5)
                except Exception as e:
                    dbg(f"_run_dashboard: {e}")
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