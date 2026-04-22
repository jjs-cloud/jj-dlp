#!/usr/bin/env python3

import subprocess
import time
import sys
import os
import json
import threading
from datetime import datetime
from typing import List, Set, Tuple
import configparser
import argparse
from urllib.parse import urlparse



# ── Early startup debug log ──────────────────────────────────────────────────
# Written BEFORE config is loaded so crashes during startup are captured.
# The log sits next to this script file: jj-dlp-startup-debug.log
#
# To disable either log file, set the corresponding flag to False:
ENABLE_STARTUP_LOG: bool = False   # jj-dlp-startup-debug.log
ENABLE_CRASH_LOG:   bool = True   # jj-dlp-crash.log
#
_STARTUP_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jj-dlp-startup-debug.log")

def _startup_dbg(msg: str) -> None:
    """Write a timestamped line to the startup debug log (always, unconditionally)."""
    if not ENABLE_STARTUP_LOG:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(_STARTUP_LOG, "a", encoding="utf-8") as _f:
            _f.write(line)
    except Exception:
        pass  # can't do much if even the log file fails

def _startup_dbg_flush() -> None:
    """Write a separator so each run is easy to spot in the log."""
    _startup_dbg("=" * 60)
    _startup_dbg(f"NEW RUN  argv={sys.argv}")
    _startup_dbg(f"cwd      = {os.getcwd()}")
    _startup_dbg(f"__file__ = {os.path.abspath(__file__)}")
    _startup_dbg(f"python   = {sys.executable}")
# ─────────────────────────────────────────────────────────────────────────────


def kill_proc(proc):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.kill()


def load_config(config_path: str):
    _startup_dbg(f"load_config called with: {config_path!r}")
    if not os.path.isfile(config_path):
        _startup_dbg(f"load_config: file NOT FOUND — {config_path!r}")
        print(f"ERROR: Config file not found at: {config_path}", file=sys.stderr)
        sys.exit(1)

    _startup_dbg(f"load_config: file found, attempting configparser.read...")
    parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8")
    except Exception as _e:
        _startup_dbg(f"load_config: configparser FAILED — {type(_e).__name__}: {_e}")
        raise
    _startup_dbg(f"load_config: configparser read OK, sections={parser.sections()}")

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

    check_interval = safe_int(general.get("CHECK_INTERVAL", 60), 60)
    output_dir = general.get("OUTPUT_DIR", "recordings").strip().strip('\"\'')
    output_tmpl = general.get("OUTPUT_TMPL", "%(title)s [%(id)s].%(ext)s").strip().strip('\"\'')
    cooldown = safe_int(general.get("COOLDOWN_AFTER_RECORDING", 5), 5)
    stall_check_interval = safe_int(general.get("STALL_CHECK_INTERVAL", 30), 30)
    stall_timeout = safe_int(general.get("STALL_TIMEOUT", 120), 120)
    config_check_interval = safe_int(general.get("CONFIG_CHECK_INTERVAL", 3), 3)
    site_tmpl = general.get("SITE_TMPL", "").strip().strip('"\'')
    tmpl_parts = urlparse(site_tmpl).path.rstrip("/").split("/") if site_tmpl else []
    username_idx = None
    for i, p in enumerate(tmpl_parts):
        if "{username}" in p:
            username_idx = i - len(tmpl_parts)
            break
    verbosity = safe_int(general.get("VERBOSITY", 1), 1)

    logging_enabled = general.get("LOGGING", "false").strip().lower() == "true"
    log_path = general.get("LOG_PATH", "").strip().strip('\"\'')
    split_logs = general.get("SPLIT_LOGS", "false").strip().lower() == "true"

    debug_logs = general.get("DEBUG_LOGS", "false").strip().lower() == "true"
    debug_log_path_raw = general.get("DEBUG_LOG_PATH", "").strip().strip('\"\'')
    debug_log_path = debug_log_path_raw if debug_log_path_raw else ""

    yt_dlp_path_raw = general.get("YT_DLP_PATH", "").strip().strip('"\'')
    yt_dlp_path = yt_dlp_path_raw if yt_dlp_path_raw else "yt-dlp"

    if not os.path.isabs(output_dir):
        output_dir = os.path.abspath(output_dir)

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
                parts = item.split()
                downloader_cmd.extend(parts)

    return {
        "streamers": streamers,
        "blocked": blocked,
        "check_interval": check_interval,
        "output_dir": output_dir,
        "output_tmpl": output_tmpl,
        "cooldown": cooldown,
        "stall_check_interval": stall_check_interval,
        "stall_timeout": stall_timeout,
        "yt_dlp_path": yt_dlp_path,
        "checker_cmd": checker_cmd,
        "downloader_cmd": downloader_cmd,
        "config_check_interval": config_check_interval,
        "verbosity": verbosity,
        "logging_enabled": logging_enabled,
        "log_path": log_path,
        "split_logs": split_logs,
        "debug_logs": debug_logs,
        "debug_log_path": debug_log_path,
        "site_tmpl": site_tmpl,
        "username_idx": username_idx,
        "config_path": config_path,
    }


currently_recording: Set[str] = set()
recording_threads: List[threading.Thread] = []
lock = threading.Lock()

trigger_full_check_event = threading.Event()
known_streamers: Set[str] = set()

VERBOSITY = 1  # default; overridden after config is loaded
verbosity_lock = threading.Lock()
VERBOSITY_NAMES = {
    1: "normal      (log only)",
    2: "debug only  (dbg only)",
    3: "verbose     (log + dbg)",
}

# Output mode: controls what yt-dlp subprocess output is shown in the terminal.
# Unrelated to VERBOSITY / log() / dbg().
#   1 = dashboard   (live status overview)
#   2 = clean       (stdout+stderr suppressed)
#   3 = stdout only
#   4 = stderr only
#   5 = everything
OUTPUT_MODE = 1
OUTPUT_MODE_NAMES = {
    1: "dashboard   (live status overview)",
    2: "clean       (stdout+stderr suppressed)",
    3: "stdout only",
    4: "stderr only",
    5: "everything  (stdout+stderr shown)",
}
output_mode_lock = threading.Lock()

# debug.log file output (DEBUG_LOGS = true in config)
DEBUG_LOGS_ENABLED: bool = False
DEBUG_LOG_PATH: str = ""
debug_log_lock = threading.Lock()

# Dashboard state (output mode 1)
dashboard_lock = threading.Lock()
# Maps streamer -> epoch float when they went live (None = offline)
dashboard_live_since: dict = {}
# Seconds until the next full liveness check (updated by main loop)
dashboard_next_check_in: float = 0.0
# All known streamers for display (updated by main loop)
dashboard_all_streamers: list = []


def cycle_verbosity() -> None:
    global VERBOSITY
    with verbosity_lock:
        VERBOSITY = VERBOSITY % 3 + 1
        mode = VERBOSITY
        name = VERBOSITY_NAMES[VERBOSITY]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [verbosity {mode}] {name}", flush=True)


def cycle_output_mode() -> None:
    global OUTPUT_MODE
    with output_mode_lock:
        OUTPUT_MODE = OUTPUT_MODE % 5 + 1
        mode = OUTPUT_MODE
        name = OUTPUT_MODE_NAMES[OUTPUT_MODE]
    if mode != 1:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [output mode {mode}] {name}", flush=True)


def _keyboard_listener() -> None:
    """Background thread: watches keypresses to cycle verbosity (v) and output mode (o)."""
    if sys.platform == "win32":
        import msvcrt
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("v", "V"):
                    cycle_verbosity()
                elif ch in ("o", "O"):
                    cycle_output_mode()
            time.sleep(0.05)
    else:
        import tty
        import termios
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            return  # not a tty (e.g. piped input) — silently skip
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("v", "V"):
                    cycle_verbosity()
                elif ch in ("o", "O"):
                    cycle_output_mode()
                elif ch in ("\x03", "\x1c"):  # Ctrl-C / Ctrl-\
                    os.kill(os.getpid(), __import__("signal").SIGINT)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _format_duration(seconds: float) -> str:
    """Return a human-readable duration string like 2h 05m 33s."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


def _live_bar(seconds: float, width: int = 20) -> str:
    """
    Return a compact bar that grows with time-live.
    Scale: bar fills completely after 6 hours (21600 s).
    """
    MAX_SECS = 6 * 3600
    filled = min(int(width * seconds / MAX_SECS), width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"


def render_dashboard() -> None:
    """Render a full-screen dashboard view (output mode 1)."""
    LIVE_COLOR   = "\033[92m"   # bright green
    OFF_COLOR    = "\033[90m"   # dark grey
    TITLE_COLOR  = "\033[96m"   # cyan
    WARN_COLOR   = "\033[93m"   # yellow
    RESET        = "\033[0m"
    CLEAR        = "\033[2J\033[H"  # clear screen + home

    with dashboard_lock:
        streamers   = list(dashboard_all_streamers)
        live_since  = dict(dashboard_live_since)
        next_in     = dashboard_next_check_in

    now = time.time()
    lines = []

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"{TITLE_COLOR}{'─' * 52}{RESET}")
    lines.append(f"{TITLE_COLOR}  jj-dlp dashboard  ·  {ts}{RESET}")
    lines.append(f"{TITLE_COLOR}{'─' * 52}{RESET}")
    lines.append("")

    if not streamers:
        lines.append(f"  {WARN_COLOR}Loading streamers...{RESET}")
    else:
        col_name = 18
        for s in streamers:
            since = live_since.get(s)
            if since is not None:
                elapsed   = now - since
                dur_str   = _format_duration(elapsed)
                bar       = _live_bar(elapsed)
                status    = f"{LIVE_COLOR}● LIVE   {RESET}"
                lines.append(
                    f"  {LIVE_COLOR}{s:<{col_name}}{RESET}  {status}  "
                    f"{LIVE_COLOR}{bar}{RESET}  {dur_str}"
                )
            else:
                status = f"{OFF_COLOR}○ offline{RESET}"
                bar    = f"{OFF_COLOR}[{'░' * 20}]{RESET}"
                lines.append(
                    f"  {OFF_COLOR}{s:<{col_name}}{RESET}  {status}  {bar}"
                )

    lines.append("")
    next_in_display = max(0.0, next_in)
    lines.append(f"  Next check in: {WARN_COLOR}{next_in_display:.0f}s{RESET}")
    lines.append(f"{TITLE_COLOR}{'─' * 52}{RESET}")
    lines.append(f"  {OFF_COLOR}press 'o' to cycle through output modes{RESET}")

    sys.stdout.write(CLEAR + "\n".join(lines) + "\n")
    sys.stdout.flush()


def _dashboard_renderer_thread() -> None:
    """Continuously re-renders the dashboard while output mode 1 is active."""
    while True:
        with output_mode_lock:
            mode = OUTPUT_MODE
        if mode == 1:
            with dashboard_lock:
                next_in = dashboard_next_check_in
            dbg(f"dashboard: render tick — next_check_in={next_in:.1f}s (id={id(dashboard_next_check_in)})")
            render_dashboard()
        time.sleep(1)


def log(msg: str) -> None:
    with verbosity_lock:
        v = VERBOSITY
    with output_mode_lock:
        mode = OUTPUT_MODE
    if mode == 1:
        return  # dashboard owns the screen
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _write_debug_log(msg: str) -> None:
    """Append a dbg() message to debug.log regardless of OUTPUT_MODE or VERBOSITY."""
    with debug_log_lock:
        enabled = DEBUG_LOGS_ENABLED
        path    = DEBUG_LOG_PATH
    if not enabled or not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
            f.flush()
    except Exception:
        pass


def dbg(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full = f"[{ts}] {msg}"
    _write_debug_log(full)
    with verbosity_lock:
        v = VERBOSITY
    with output_mode_lock:
        mode = OUTPUT_MODE
    if mode == 1:
        return  # dashboard owns the screen
    if v in (2, 3):
        print(full, flush=True)


def get_debug_log_path(cfg: dict) -> str:
    p = cfg.get("debug_log_path") or ""
    if p.strip():
        return p
    return os.path.join(cfg.get("output_dir", "."), "debug.log")


def get_log_path(cfg: dict) -> str:
    lp = cfg.get("log_path") or ""
    if lp.strip():
        return lp
    return os.path.join(cfg.get("output_dir", "."), "jj-dlp.log")


def get_log_file_paths(cfg: dict) -> Tuple[str, str]:
    base = get_log_path(cfg)
    if cfg.get("split_logs"):
        return f"{base}.stdout.log", f"{base}.stderr.log"
    else:
        return base, base


def open_log_streams(cfg: dict):
    """
    Always returns subprocess.PIPE so record_stream can drain and selectively
    display output according to OUTPUT_MODE.  Log-file handles are opened here
    and passed through to the drainer threads via the returned closer.
    """
    log_out_fp = None
    log_err_fp = None

    if cfg.get("logging_enabled"):
        out_path, err_path = get_log_file_paths(cfg)
        try:
            log_out_fp = open(out_path, "a", encoding="utf-8")
        except Exception:
            log_out_fp = None
        try:
            if err_path == out_path:
                log_err_fp = log_out_fp
            else:
                log_err_fp = open(err_path, "a", encoding="utf-8")
        except Exception:
            log_err_fp = None

    def _close():
        for fp in {log_out_fp, log_err_fp}:
            try:
                if fp is not None and hasattr(fp, "close"):
                    fp.close()
            except Exception:
                pass

    # Always use PIPE; the drainer threads handle display + optional file write.
    return subprocess.PIPE, subprocess.PIPE, _close, log_out_fp, log_err_fp


def _drain_pipe(pipe, log_fp, show_modes: set) -> None:
    """
    Read lines from *pipe* until EOF.
    - Writes each line to *log_fp* if provided.
    - Prints to terminal when OUTPUT_MODE is in *show_modes*.

    show_modes: set of OUTPUT_MODE values that should display this stream.
                stdout -> {3, 5}   stderr -> {4, 5}
    """
    try:
        for raw in pipe:
            line = raw.decode(errors="replace").rstrip("\n")
            if log_fp is not None:
                try:
                    log_fp.write(line + "\n")
                    log_fp.flush()
                except Exception:
                    pass
            with output_mode_lock:
                mode = OUTPUT_MODE
            if mode in show_modes:
                print(line, flush=True)
    except Exception:
        pass


def build_yt_dlp_command(yt_dlp_path: str, base_cmd: List[str], extra: List[str]) -> List[str]:
    return [yt_dlp_path, *base_cmd, *extra]


def get_live_streamers(streamers: List[str], cfg: dict) -> List[str]:
    if not streamers:
        return []

    streamers = [s for s in streamers if s not in cfg["blocked"]]
    if not streamers:
        return []

    urls = [cfg["site_tmpl"].format(username=s) for s in streamers]
    cmd = build_yt_dlp_command(cfg["yt_dlp_path"], cfg["checker_cmd"], urls)

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if cfg["logging_enabled"]:
        out_path, err_path = get_log_file_paths(cfg)
        try:
            if result.stdout:
                with open(out_path, "a", encoding="utf-8") as lf:
                    lf.write(result.stdout)
        except Exception:
            pass
        try:
            if result.stderr:
                with open(err_path, "a", encoding="utf-8") as lf:
                    lf.write(result.stderr)
        except Exception:
            pass

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
                    if ui is not None:
                        streamer = url.rstrip("/").split("/")[ui].lstrip("@").lower().strip()
                    else:
                        streamer = url.rstrip("/").split("/")[-1].lstrip("@").lower().strip()
                except Exception:
                    streamer = url.rstrip("/").split("/")[-1].lstrip("@").lower().strip()

                if streamer:
                    live.append(streamer)
        except Exception:
            if cfg.get("logging_enabled"):
                try:
                    out_path, _ = get_log_file_paths(cfg)
                    with open(out_path, "a", encoding="utf-8") as lf:
                        lf.write(f"JSON parse error for line: {line}")
                except Exception:
                    pass
            continue
    return live


def wait_for_streamer_file(output_dir: str, streamer: str, proc_start_time: float, timeout: float = 15.0, interval: float = 0.5):
    start = time.time()

    while time.time() - start < timeout:
        if os.path.isdir(output_dir):
            files = [
                os.path.join(output_dir, f)
                for f in os.listdir(output_dir)
                if os.path.isfile(os.path.join(output_dir, f))
                and streamer.lower() in f.lower()
            ]

            if proc_start_time is not None:
                files = [f for f in files if os.path.getmtime(f) >= proc_start_time]

            if files:
                return max(files, key=os.path.getmtime)

        time.sleep(interval)

    return None

def get_streamer_file_size(
    output_dir: str,
    streamer: str,
    cfg: dict = None,
    last_growth_time: float = None,
    stall_timeout: int = None,
    stall_check_interval: int = None,
    proc_start_time: float = None,
) -> tuple[int, bool, str]:   # <- Now returns size, stall_detected, filename

    try:
        if not os.path.isdir(output_dir):
            filename = ""
            size = 0
        else:
            filename = wait_for_streamer_file(output_dir, streamer, proc_start_time)
            if filename:
                size = os.path.getsize(filename)
            else:
                size = 0
                filename = ""

        stalled_time = 0.0
        stall_detected = False

        dbg(f"stall_checker: file: {os.path.basename(filename) or '<none>'}")

        try:
            if last_growth_time is not None:
                stalled_time = max(0.0, time.time() - last_growth_time - stall_check_interval)

                if stall_timeout is not None and stalled_time >= stall_timeout:
                    stall_detected = True
                    log(f"stall_checker: Stall detected for {streamer} ({os.path.basename(filename) or '<none>'})! "
                        f"stalled_time: {stalled_time:.1f}s exceeds stall_timeout: {stall_timeout}s")
        except Exception:
            stalled_time = 0.0
            log(f"stall_checker: Exception during stall check for {streamer}: {sys.exc_info()[0]}")

        if cfg and cfg.get("logging_enabled"):
            try:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                out_path, _ = get_log_file_paths(cfg)
                with open(out_path, "a", encoding="utf-8") as lf:
                    lf.write(
                        f"[{ts}] STALL_CHECK streamer={streamer} file={os.path.basename(filename) or '<none>'} "
                        f"size={size} stalled_time={stalled_time:.1f}s stall_detected={stall_detected}"
                    )
            except Exception:
                pass

        return size, stall_detected, filename   # ← return filename too

    except Exception:
        log(f"Exception in get_streamer_file_size for {streamer}: {sys.exc_info()[0]}")
        return 0, False, ""


def record_stream(streamer: str, cfg: dict) -> None:
    channel_url = cfg["site_tmpl"].format(username=streamer)
    output_dir = cfg["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, cfg["output_tmpl"])

    log(f"Recording started: {streamer}  (saving to: {output_dir})\n")

    proc = None
    close_logs = lambda: None

    try:
        while True:
            cmd = build_yt_dlp_command(cfg["yt_dlp_path"], cfg["downloader_cmd"], ["-o", output_path, channel_url])
            out_target, err_target, close_logs, log_out_fp, log_err_fp = open_log_streams(cfg)

            try:
                proc = subprocess.Popen(cmd, stdout=out_target, stderr=err_target)
                proc_start_time = time.time()
                # Drain subprocess pipes in background threads so the main
                # record loop is never blocked.  Each drainer respects the
                # current OUTPUT_MODE to decide whether to print to terminal.
                threading.Thread(
                    target=_drain_pipe,
                    args=(proc.stdout, log_out_fp, {3, 5}),
                    daemon=True,
                ).start()
                threading.Thread(
                    target=_drain_pipe,
                    args=(proc.stderr, log_err_fp, {4, 5}),
                    daemon=True,
                ).start()
            except Exception as e:
                log(f"Failed to start yt-dlp for {streamer}: {e}")
                try:
                    close_logs()
                except Exception:
                    pass
                break

            # === FIXED: Unpack all 3 return values ===
            last_size, _, current_filename = get_streamer_file_size(
                output_dir, streamer, cfg=cfg, proc_start_time=proc_start_time
            )

            dbg(f"stall_checker: Setting initial last_size for {streamer}: {last_size} bytes "
                f"(file: {os.path.basename(current_filename) if current_filename else '<none>'})")

            last_growth_time = time.time()
            dbg(f"stall_checker: Setting initial last_growth_time for {streamer}: {last_growth_time}")

            stall_check_interval = cfg["stall_check_interval"]
            stall_timeout = cfg["stall_timeout"]
            seconds_since_check = 0
            dbg(f"stall_checker: The first stall check will be in {stall_check_interval} seconds.")

            while proc.poll() is None:  # While process is still running
                current_cfg = load_config(cfg["config_path"])

                if streamer in current_cfg["blocked"]:
                    kill_proc(proc)
                    log(f"Recording STOPPED (blocked) -> {streamer}")
                    try:
                        close_logs()
                    except Exception:
                        pass
                    with lock:
                        currently_recording.discard(streamer)
                    log(f"Recording finished: {streamer}")
                    time.sleep(cfg["cooldown"])
                    return

                poll_interval = cfg.get("config_check_interval", 3)
                time.sleep(poll_interval)
                seconds_since_check += poll_interval

                if seconds_since_check >= stall_check_interval:
                    seconds_since_check = 0

                    # === FIXED: Unpack all 3 return values here too ===
                    current_size, stall_detected, current_filename = get_streamer_file_size(
                        output_dir, streamer, cfg=cfg,
                        proc_start_time=proc_start_time,
                        last_growth_time=last_growth_time,
                        stall_timeout=stall_timeout,
                        stall_check_interval=stall_check_interval
                    )

                    if stall_detected:
                        filename_display = os.path.basename(current_filename) if current_filename else f"{streamer} (no file yet)"
                        log(f"\n\nstall_checker: Stall confirmed for {filename_display} — killing and restarting yt-dlp.\n\n")
                        kill_proc(proc)
                        try:
                            close_logs()
                        except Exception:
                            pass
                        time.sleep(5)
                        break

                    filename_display = os.path.basename(current_filename) if current_filename else f"{streamer} (no file yet)"

                    if current_size > last_size:
                        dbg(f"stall_checker: {filename_display} grew by {current_size - last_size} bytes ({last_size} --> {current_size})")
                        dbg(f"stall_checker: Setting new last_size for {filename_display}: {current_size} bytes")
                        last_size = current_size
                        last_growth_time = time.time()
                        dbg(f"stall_checker: Setting new last_growth_time for {filename_display}: {last_growth_time}")
                    elif current_size < last_size:
                        last_size = current_size
                        last_growth_time = time.time()
                        log(f"stall_checker: File size decreased for {filename_display}. "
                            f"Updated last_size to {last_size} bytes.")
                    else:
                        log(f"stall_checker: No file size growth detected for {filename_display}. "
                            f"Size has been {current_size} bytes since last check.")

            else:
                try:
                    close_logs()
                except Exception:
                    pass
                log(f"Recording finished: {streamer}")
                break

    except KeyboardInterrupt:
        if proc is not None:
            try:
                kill_proc(proc)
            except Exception:
                pass
        try:
            close_logs()
        except Exception:
            pass
    finally:
        with lock:
            currently_recording.discard(streamer)
        time.sleep(cfg["cooldown"])


def start_recording_if_needed(live_now: List[str], cfg: dict) -> None:
    global recording_threads

    with lock:
        to_start = [s for s in live_now if s not in currently_recording and s not in cfg["blocked"]]
        if not to_start:
            recording_threads[:] = [t for t in recording_threads if t.is_alive()]
            return

        for streamer in to_start:
            currently_recording.add(streamer)
            # Track when this streamer went live for the dashboard
            with dashboard_lock:
                if streamer not in dashboard_live_since:
                    dashboard_live_since[streamer] = time.time()
            t = threading.Thread(target=record_stream, args=(streamer, cfg), daemon=True)
            t.start()
            recording_threads.append(t)

        recording_threads[:] = [t for t in recording_threads if t.is_alive()]


def config_watcher(config_path: str, poll_interval: int = 3) -> None:
    dbg("config_watcher thread started")
    prev_streamers: Set[str] = set()
    first_run = True

    while True:
        try:
            cfg = load_config(config_path)
            curr_streamers = set(cfg.get("streamers", []))
            blocked = set(cfg.get("blocked", []))

            dbg(f"config_watcher: curr_streamers={curr_streamers} prev_streamers={prev_streamers} first_run={first_run}")

            if first_run:
                prev_streamers = curr_streamers
                first_run = False
                dbg("config_watcher: first run — baseline set, skipping trigger check")
            else:
                added = [s for s in (curr_streamers - prev_streamers) if s not in blocked]
                dbg(f"config_watcher: added={added}")
                if added: 
                    log(f"config_watcher: new streamer(s) detected: {', '.join(added)} — triggering immediate full check")
                    with lock:
                        known_streamers.update(curr_streamers)
                    trigger_full_check_event.set()
                    dbg("config_watcher: trigger_full_check_event SET")
                prev_streamers = curr_streamers

        except Exception as e:
            dbg(f"config_watcher: exception during poll: {e}")

        time.sleep(poll_interval)


def main() -> None:
    # ── Double-click / drag-and-drop fix ─────────────────────────────────────
    # When launched by double-clicking on Windows, the CWD is whatever Explorer
    # happens to use (often C:\Windows\system32), not the script's own folder.
    # Change to the script's directory immediately so config discovery, relative
    # OUTPUT_DIR paths, and log files all resolve correctly.
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.getcwd() != _script_dir:
        os.chdir(_script_dir)
        _startup_dbg(f"CWD changed to script directory: {_script_dir}")
    # ─────────────────────────────────────────────────────────────────────────

    _startup_dbg_flush()  # DBG: log every startup attempt to jj-dlp-startup-debug.log
    parser = argparse.ArgumentParser(description="Stream recorder")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config file (default: jj-dlp.conf in current directory)"
    )

    args = parser.parse_args()

    # Determine config path: explicit flag → default name → discovery fallback
    if args.config is not None:
        config_path = os.path.abspath(args.config)
        if not os.path.isfile(config_path):
            print(f"ERROR: Config file not found at: {config_path}", file=sys.stderr)
            sys.exit(1)
    else:
        default_path = os.path.abspath("jj-dlp.conf")
        if os.path.isfile(default_path):
            config_path = default_path
        else:
            # Discover all .conf files in the current directory
            cwd = os.getcwd()
            _startup_dbg(f"config discovery: cwd={cwd!r}")
            found = sorted(
                f for f in os.listdir(cwd)
                if f.endswith(".conf") and os.path.isfile(os.path.join(cwd, f))
            )
            _startup_dbg(f"config discovery: .conf files found={found}")
            if not found:
                print(
                    f"ERROR: No config file found. Expected 'jj-dlp.conf' in {cwd} "
                    "or pass --config <path>.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if len(found) == 1:
                config_path = os.path.join(cwd, found[0])
                print(f"Config file not found. Using the only .conf file discovered: {found[0]}")
            else:
                print("\nConfig file 'jj-dlp.conf' not found and --config <path> not specified. The following .conf files were discovered:\n\n")
                for i, name in enumerate(found, 1):
                    print(f"  [{i}] {name}")
                print()
                while True:
                    try:
                        raw = input(f"Select a config file [1-{len(found)}]: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print("\nAborted.", file=sys.stderr)
                        sys.exit(1)
                    if raw.isdigit() and 1 <= int(raw) <= len(found):
                        config_path = os.path.join(cwd, found[int(raw) - 1])
                        break
                    print(f"  Please enter a number between 1 and {len(found)}.")

    initial_cfg = load_config(config_path)

    global VERBOSITY, DEBUG_LOGS_ENABLED, DEBUG_LOG_PATH, dashboard_next_check_in
    with verbosity_lock:
        VERBOSITY = initial_cfg.get("verbosity", 1)

    with debug_log_lock:
        DEBUG_LOGS_ENABLED = initial_cfg.get("debug_logs", False)
        DEBUG_LOG_PATH     = get_debug_log_path(initial_cfg) if DEBUG_LOGS_ENABLED else ""

    log(f"Verbosity: {VERBOSITY} — {VERBOSITY_NAMES[VERBOSITY]}  (press 'v' to cycle)")
    log(f"Config file: {config_path}")
    log(f"Output directory: {initial_cfg['output_dir']}")

    if initial_cfg["logging_enabled"]:
        log(f"Log file: {get_log_path(initial_cfg)}")

    if initial_cfg.get("debug_logs"):
        log(f"Debug log: {get_debug_log_path(initial_cfg)}")
                                                                      

    log(f"Check interval: {initial_cfg['check_interval']}s")
    log(f"Output mode: {OUTPUT_MODE} — {OUTPUT_MODE_NAMES[OUTPUT_MODE]}  (press 'o' to cycle through modes 1–5)\n")

    keyboard_thread = threading.Thread(target=_keyboard_listener, daemon=True)
    keyboard_thread.start()

    dashboard_thread = threading.Thread(target=_dashboard_renderer_thread, daemon=True)
    dashboard_thread.start()

    watcher_interval = initial_cfg.get("config_check_interval", 3)
    watcher_thread = threading.Thread(
        target=config_watcher,
        args=(config_path, watcher_interval),
        daemon=True
    )
    watcher_thread.start()
    dbg(f"main: config_watcher thread launched (poll every {watcher_interval}s)")

    try:
        while True:
            cfg = load_config(config_path)

            streamers = cfg["streamers"]

            with lock:
                known_streamers.clear()
                known_streamers.update(streamers)

            # While the liveness check is running the countdown has no meaning —
            # zero it so the dashboard shows 0s (i.e. "checking now").
            with dashboard_lock:
                dashboard_next_check_in = 0.0
            dbg(f"dashboard: [WRITE] zeroed before liveness check (id={id(dashboard_next_check_in)})")

            dbg(f"main: top of loop — streamers={streamers} currently_recording={currently_recording} event_is_set={trigger_full_check_event.is_set()}")

            if not streamers:
                log("ERROR: No streamers configured. Retrying next cycle.")
            else:
                log(f"Checking live status for {', '.join(streamers)}\n")
                live_now = get_live_streamers(streamers, cfg)
                dbg(f"main: get_live_streamers returned: {live_now}")

                # Update dashboard: mark offline streamers, keep live ones
                with dashboard_lock:
                    dashboard_all_streamers.clear()
                    dashboard_all_streamers.extend(streamers)
                    live_set = set(live_now)
                    for s in streamers:
                        if s not in live_set:
                            dashboard_live_since.pop(s, None)
                        elif s not in dashboard_live_since:
                            dashboard_live_since[s] = time.time()

                dbg(f"dashboard: state updated — live={list(live_set)} all={streamers}")

                if live_now:
                    log(f"Live now: {', '.join(live_now)}\n")
                    start_recording_if_needed(live_now, cfg)
                else:
                    log("All streamers are offline.")

            wait_secs = cfg.get("check_interval", 60)
            log(f"Next full check in {wait_secs}s...\n")

            # Seed the dashboard countdown and record the deadline BEFORE we enter
            # the wait loop.  This means the display shows the full interval
            # immediately after the liveness check, rather than a stale 0.
            deadline = time.time() + wait_secs
            with dashboard_lock:
                dashboard_next_check_in = float(wait_secs)
            dbg(f"dashboard: [WRITE] seeded to {wait_secs}s (id={id(dashboard_next_check_in)}, deadline={deadline:.3f})")

            dbg(f"main: entering wait loop (timeout={wait_secs}s)")
            try:
                triggered = False
                while True:
                    remaining = deadline - time.time()
                    with dashboard_lock:
                        dashboard_next_check_in = max(0.0, remaining)
                    dbg(f"dashboard: countdown tick — remaining={remaining:.1f}s")
                    if remaining <= 0:
                        # Zero it out explicitly so the display shows 0 cleanly
                        # while the next liveness check runs.
                        with dashboard_lock:
                            dashboard_next_check_in = 0.0
                        dbg("dashboard: countdown reached 0 — breaking wait loop")
                        break
                    fired = trigger_full_check_event.wait(timeout=min(1.0, remaining))
                    if fired:
                        triggered = True
                        with dashboard_lock:
                            dashboard_next_check_in = 0.0
                        dbg("dashboard: early trigger received — countdown zeroed")
                        break
                dbg(f"main: wait returned — triggered={triggered} event_is_set={trigger_full_check_event.is_set()}")
                if triggered:
                    trigger_full_check_event.clear()
                    dbg("main: event cleared, looping immediately for full check")
                else:
                    dbg("main: normal timeout elapsed, proceeding to next cycle")
            except Exception as e:
                dbg(f"main: wait raised exception: {e} — falling back to sleep")
                time.sleep(wait_secs)

    except KeyboardInterrupt:
        log("Shutting down...")
        for t in recording_threads:
            if t.is_alive():
                t.join(timeout=15) # Wait 15 seconds for each thread to finish gracefully
        log("Goodbye!")


if __name__ == "__main__":
    try:
        main()
    except Exception as _top_e:
        import traceback
        _startup_dbg(f"UNCAUGHT EXCEPTION in main(): {type(_top_e).__name__}: {_top_e}")
        _startup_dbg(traceback.format_exc())
        # Also write to a visible file next to the script so nothing is lost
        if ENABLE_CRASH_LOG:
            _crash_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jj-dlp-crash.log")
            try:
                with open(_crash_path, "a", encoding="utf-8") as _cf:
                    _cf.write(f"\n{'='*60}\n")
                    _cf.write(f"CRASH at {datetime.now()}\n")
                    _cf.write(traceback.format_exc())
            except Exception:
                pass
        raise  # re-raise so the normal Python error is still printed