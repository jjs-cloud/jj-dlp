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

    popup_notifications = general.get("POPUP_NOTIFICATIONS", "true").strip().lower() == "true"
    popup_timeout = safe_int(general.get("POPUP_TIMEOUT", 15), 15)

    debug_logs = general.get("DEBUG_LOGS", "false").strip().lower() == "true"
    debug_log_path_raw = general.get("DEBUG_LOG_PATH", "").strip().strip('\"\'')
    debug_log_path = debug_log_path_raw if debug_log_path_raw else ""

    yt_dlp_path_raw = general.get("YT_DLP_PATH", "").strip().strip('"\'')
    yt_dlp_path = yt_dlp_path_raw if yt_dlp_path_raw else "yt-dlp"

    ffmpeg_path_raw = general.get("FFMPEG_PATH", "").strip().strip('"\'')
    ffmpeg_path = ffmpeg_path_raw  # empty string means no path given; --ffmpeg-location will be omitted

    if not os.path.isabs(output_dir):
        output_dir = os.path.abspath(output_dir)

    # ── Twitch EventSub (optional) ────────────────────────────────────────────
    twitch_cfg = parser["Twitch"] if parser.has_section("Twitch") else {}
    twitch_client_id     = twitch_cfg.get("CLIENT_ID", "").strip().strip('"\'')
    twitch_client_secret = twitch_cfg.get("CLIENT_SECRET", "").strip().strip('"\'')
    twitch_webhook_secret= twitch_cfg.get("WEBHOOK_SECRET", "jj-dlp-secret").strip().strip('"\'')
    twitch_callback_url  = twitch_cfg.get("CALLBACK_URL", "").strip().strip('"\'')
    twitch_webhook_port  = safe_int(twitch_cfg.get("WEBHOOK_PORT", 8888), 8888)
    twitch_enabled = bool(twitch_client_id and twitch_client_secret and twitch_callback_url)
    # ─────────────────────────────────────────────────────────────────────────

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
        "ffmpeg_path": ffmpeg_path,
        "checker_cmd": checker_cmd,
        "downloader_cmd": downloader_cmd,
        "config_check_interval": config_check_interval,
        "verbosity": verbosity,
        "logging_enabled": logging_enabled,
        "log_path": log_path,
        "split_logs": split_logs,
        "popup_notifications": popup_notifications,
        "popup_timeout": popup_timeout,
        "debug_logs": debug_logs,
        "debug_log_path": debug_log_path,
        "site_tmpl": site_tmpl,
        "username_idx": username_idx,
        "config_path": config_path,
        # Twitch EventSub
        "twitch_enabled": twitch_enabled,
        "twitch_client_id": twitch_client_id,
        "twitch_client_secret": twitch_client_secret,
        "twitch_webhook_secret": twitch_webhook_secret,
        "twitch_callback_url": twitch_callback_url,
        "twitch_webhook_port": twitch_webhook_port,
    }


currently_recording: Set[str] = set()
recording_threads: List[threading.Thread] = []
lock = threading.Lock()

trigger_full_check_event = threading.Event()
known_streamers: Set[str] = set()

VERBOSITY = 1  # default; overridden after config is loaded
verbosity_lock = threading.Lock()
# Verbosity levels apply only when OUTPUT_MODE == 2 (terminal mode).
# They control which streams of output are shown alongside log() messages.
#   1 = clean      (log only)
#   2 = stdout     (log + stdout)
#   3 = debug      (log + stdout + debug)
#   4 = stderr     (stderr only)
#   5 = everything (log + stdout + debug + stderr)
VERBOSITY_NAMES = {
    1: "clean",
    2: "stdout",
    3: "debug",
    4: "stderr",
    5: "everything",
}
VERBOSITY_DESC = {
    1: "log only",
    2: "log + stdout",
    3: "log + stdout + debug",
    4: "stderr only",
    5: "log + stdout + debug + stderr",
}

# Output mode: controls the display style.
#   1 = dashboard  (live status overview)
#   2 = terminal   (text output; verbosity level controls detail)
#   3 = add a streamer  (internal UI mode)
#   4 = remove a streamer  (internal UI mode)
#   5 = disable a streamer  (internal UI mode)
OUTPUT_MODE = 1
OUTPUT_MODE_NAMES = {
    1: "dashboard",
    2: "terminal",
    3: "add a streamer",
    4: "remove a streamer",
    5: "disable a streamer",
}
output_mode_lock = threading.Lock()

# ── FFmpeg Error Monitoring ────────────────────────────────────────────
# Strings to watch for in ffmpeg's stderr output. If any of these appear
# more than FFMPEG_ERROR_RESTART_THRESHOLD times during a single recording
# session, yt-dlp will be killed and restarted automatically.
#
# Add new patterns here as new ffmpeg errors are identified.
# Matching is case-insensitive and uses substring search (not regex).
FFMPEG_ERROR_PATTERNS: List[str] = [
    "timestamp discontinuity",
    "Packet corrupt",
    # Add more patterns below as needed, e.g.:
    # "av_interleaved_write_frame",
    # "application provided invalid",
]

# How many total pattern matches (across all patterns) must occur before
# yt-dlp is restarted. Set to 0 to disable ffmpeg error monitoring.
FFMPEG_ERROR_RESTART_THRESHOLD: int = 500
# ──────────────────────────────────────────────────────────────────────

# ── Keybind Configuration ──────────────────────────────────────────────
# Use single characters OR control codes like '\x16' (Ctrl+V)

KEYBIND_VERBOSITY = "\x02"   # Ctrl+B
KEYBIND_OUTPUT    = "\x0f"   # Ctrl+O
KEYBIND_ADD       = "a"
KEYBIND_REMOVE    = "r"
KEYBIND_DISABLE   = "d"

# Optional: human-readable labels for UI
KEYBIND_LABELS = {
    KEYBIND_VERBOSITY: "Ctrl+B",
    KEYBIND_OUTPUT:    "Ctrl+O",
    KEYBIND_ADD:       "A",
    KEYBIND_REMOVE:    "R",
    KEYBIND_DISABLE:   "D",
}
# ───────────────────────────────────────────────────────────────────────


# Shared config path (set in main, used by streamer management modes)
_config_path: str = ""

# debug.log file output (DEBUG_LOGS = true in config)
DEBUG_LOGS_ENABLED: bool = False
DEBUG_LOG_PATH: str = ""
debug_log_lock = threading.Lock()

# Event used to wake the streamer-management thread when mode 3/4 becomes active
_streamer_mgmt_event = threading.Event()

# When set, the keyboard listener must not read from stdin (mgmt thread owns it)
_stdin_owned_by_mgmt = threading.Event()

# Dashboard state (output mode 1)
dashboard_lock = threading.Lock()
# Maps streamer -> epoch float when they went live (None = offline)
dashboard_live_since: dict = {}
# Seconds until the next full liveness check (updated by main loop)
dashboard_next_check_in: float = 0.0
# All known streamers for display (updated by main loop)
dashboard_all_streamers: list = []
# Streamers currently in [Block] (updated by main loop); used for "disabled" display
dashboard_blocked_streamers: set = set()
# Set when shutting down so the renderer stops immediately without waiting for its sleep to expire
_dashboard_stop_event = threading.Event()


# ── Popup notification ────────────────────────────────────────────────────────
def _show_live_popup(streamer: str, source: str = "poll", popup_timeout: int = 15) -> None:
    """
    Show a non-blocking tkinter popup when a streamer goes live.
    source: 'poll' (normal yt-dlp loop) or 'eventsub' (Twitch EventSub push).
    popup_timeout: seconds before the popup auto-closes (configurable via POPUP_TIMEOUT).
    Runs in its own daemon thread so it never blocks the main loop.
    tkinter is optional — if unavailable, the popup is silently skipped.
    """
    dbg(f"[POPUP] _show_live_popup called — streamer={streamer!r}  source={source!r}  popup_timeout={popup_timeout}s")

    def _run():
        dbg(f"[POPUP] thread started — streamer={streamer!r}  source={source!r}")
        try:
            import tkinter as tk
            dbg(f"[POPUP] tkinter imported OK")

            root = tk.Tk()
            root.withdraw()  # hide the blank root window

            win = tk.Toplevel(root)
            win.title("jj-dlp — Stream Live")
            win.resizable(False, False)
            win.attributes("-topmost", True)

            label_text = f"🔴  {streamer}  is now LIVE"
            source_text = f"via {'EventSub' if source == 'eventsub' else 'poll check'}"

            tk.Label(win, text=label_text,
                     font=("Segoe UI", 16, "bold"), padx=20, pady=10).pack()
            tk.Label(win, text=source_text,
                     font=("Segoe UI", 10), fg="gray", padx=20).pack()
            tk.Button(win, text="Dismiss", command=win.destroy,
                      padx=12, pady=4).pack(pady=(4, 12))

            dbg(f"[POPUP] window created — scheduling auto-close in {popup_timeout}s")
            # Auto-close after popup_timeout seconds (configured via POPUP_TIMEOUT)
            win.after(popup_timeout * 1000, win.destroy)

            dbg(f"[POPUP] entering mainloop")
            root.mainloop()
            dbg(f"[POPUP] mainloop exited for streamer={streamer!r}")
        except ImportError:
            dbg(f"[POPUP] tkinter not available — skipping popup notification")
        except Exception as e:
            dbg(f"[POPUP] exception in _run — {type(e).__name__}: {e}")

    t = threading.Thread(target=_run, daemon=True, name=f"popup-{streamer}")
    t.start()
    dbg(f"[POPUP] daemon thread launched (name={t.name!r})")
# ─────────────────────────────────────────────────────────────────────────────


def cycle_verbosity() -> None:
    global VERBOSITY
    with verbosity_lock:
        VERBOSITY = VERBOSITY % 5 + 1
        mode = VERBOSITY
        name = VERBOSITY_NAMES[VERBOSITY]
        desc = VERBOSITY_DESC[VERBOSITY]
    kb_verb = KEYBIND_LABELS.get(KEYBIND_VERBOSITY, "Ctrl+B")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [verbosity {mode}] {name}       ({desc})", flush=True)


def cycle_output_mode() -> None:
    global OUTPUT_MODE
    with output_mode_lock:
        # Only cycle between modes 1 (dashboard) and 2 (terminal)
        if OUTPUT_MODE == 1:
            OUTPUT_MODE = 2
        else:
            OUTPUT_MODE = 1

        mode = OUTPUT_MODE
        name = OUTPUT_MODE_NAMES[OUTPUT_MODE]

    if mode == 2:
        with verbosity_lock:
            v = VERBOSITY
        v_name = VERBOSITY_NAMES[v]
        v_desc = VERBOSITY_DESC[v]
        kb_verb = KEYBIND_LABELS.get(KEYBIND_VERBOSITY, "Ctrl+B")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [output mode 2] {name}   [{v_name}] ({v_desc}) (press {kb_verb} to cycle verbosity levels)", flush=True)


def _set_output_mode(mode: int) -> None:
    """Jump directly to a specific output mode."""
    global OUTPUT_MODE
    with output_mode_lock:
        OUTPUT_MODE = mode
    if mode in (3, 4, 5):
        _stdin_owned_by_mgmt.set()
        _streamer_mgmt_event.set()
    else:
        _stdin_owned_by_mgmt.clear()
        if mode == 2:
            with verbosity_lock:
                v = VERBOSITY
            v_name = VERBOSITY_NAMES[v]
            v_desc = VERBOSITY_DESC[v]
            kb_verb = KEYBIND_LABELS.get(KEYBIND_VERBOSITY, "Ctrl+B")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            name = OUTPUT_MODE_NAMES.get(mode, "")
            print(f"[{ts}] [output mode 2] {name}   [{v_name}] ({v_desc}) (press {kb_verb} to cycle verbosity levels)", flush=True)


def _keyboard_listener() -> None:
    """Background thread: watches keypresses based on configurable keybinds."""
    if sys.platform == "win32":
        import msvcrt
        while True:
            # Yield stdin to the streamer-mgmt thread while it owns it
            if _stdin_owned_by_mgmt.is_set():
                time.sleep(0.05)
                continue
            if msvcrt.kbhit():
                ch = msvcrt.getwch()

                if ch == KEYBIND_VERBOSITY:
                    cycle_verbosity()
                elif ch == KEYBIND_OUTPUT:
                    cycle_output_mode()
                elif ch.lower() == KEYBIND_ADD.lower():
                    _set_output_mode(3)
                elif ch.lower() == KEYBIND_REMOVE.lower():
                    _set_output_mode(4)
                elif ch.lower() == KEYBIND_DISABLE.lower():
                    _set_output_mode(5)

            time.sleep(0.05)

    else:
        import tty
        import termios
        import select
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            return

        try:
            tty.setraw(fd)
            while True:
                # Yield stdin to the streamer-mgmt thread while it owns it.
                # Restore normal tty settings so _read_line_raw gets a clean
                # terminal, then wait (without touching stdin) until it's done.
                if _stdin_owned_by_mgmt.is_set():
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    while _stdin_owned_by_mgmt.is_set():
                        _stdin_owned_by_mgmt.wait(timeout=0.1)
                    tty.setraw(fd)
                    continue

                # Use select() with a short timeout instead of a bare blocking
                # read(1).  This lets us re-check _stdin_owned_by_mgmt promptly
                # after the user presses a hotkey that switches to the mgmt page,
                # so we never race with _read_line_raw for subsequent characters.
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not ready:
                    continue

                # Re-check immediately after select returns — the mgmt thread
                # may have claimed stdin during the 50 ms window.
                if _stdin_owned_by_mgmt.is_set():
                    continue

                ch = sys.stdin.read(1)

                if ch == KEYBIND_VERBOSITY:
                    cycle_verbosity()
                elif ch == KEYBIND_OUTPUT:
                    cycle_output_mode()
                elif ch.lower() == KEYBIND_ADD.lower():
                    _set_output_mode(3)
                elif ch.lower() == KEYBIND_REMOVE.lower():
                    _set_output_mode(4)
                elif ch.lower() == KEYBIND_DISABLE.lower():
                    _set_output_mode(5)
                elif ch in ("\x03", "\x1c"):
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
    LIVE_COLOR     = "\033[92m"   # bright green
    OFF_COLOR      = "\033[90m"   # dark grey
    DISABLED_COLOR = "\033[93m"   # yellow
    TITLE_COLOR    = "\033[96m"   # cyan
    WARN_COLOR     = "\033[93m"   # yellow
    RESET          = "\033[0m"
    CLEAR          = "\033[2J\033[H"  # clear screen + home

    with dashboard_lock:
        streamers   = list(dashboard_all_streamers)
        live_since  = dict(dashboard_live_since)
        next_in     = dashboard_next_check_in
        blocked_set = set(dashboard_blocked_streamers)

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
            is_disabled = s in blocked_set
            since = live_since.get(s)
            if is_disabled:
                status = f"{DISABLED_COLOR}⊘ disabled{RESET}"
                bar    = f"{DISABLED_COLOR}[{'░' * 20}]{RESET}"
                lines.append(
                    f"  {DISABLED_COLOR}{s:<{col_name}}{RESET}  {status}  {bar}"
                )
            elif since is not None:
                elapsed   = now - since
                dur_str   = _format_duration(elapsed)
                bar       = _live_bar(elapsed)
                status    = f"{LIVE_COLOR}● LIVE    {RESET}"
                lines.append(
                    f"  {LIVE_COLOR}{s:<{col_name}}{RESET}  {status}  "
                    f"{LIVE_COLOR}{bar}{RESET}  {dur_str}"
                )
            else:
                status = f"{OFF_COLOR}○ offline {RESET}"
                bar    = f"{OFF_COLOR}[{'░' * 20}]{RESET}"
                lines.append(
                    f"  {OFF_COLOR}{s:<{col_name}}{RESET}  {status}  {bar}"
                )

    lines.append("")
    next_in_display = max(0.0, next_in)
    lines.append(f"  Next check in: {WARN_COLOR}{next_in_display:.0f}s{RESET}")

    # ── Twitch EventSub status block ──────────────────────────────────────────
    srv_status              = _eventsub_state.get_server_status()
    last_notif, notif_total = _eventsub_state.get_notification_info()
    sub_ids                 = _eventsub_state.get_subscription_ids()
    eventsub_configured     = _eventsub is not None

    if eventsub_configured or srv_status not in ("not started", "stopped"):
        lines.append("")
        lines.append(f"  {TITLE_COLOR}── Twitch EventSub ─────────────────────────────{RESET}")

        # Server bind status
        if "listening" in srv_status:
            lines.append(f"  Webhook server:   {LIVE_COLOR}● {srv_status}{RESET}")
        elif "ERROR" in srv_status:
            lines.append(f"  Webhook server:   {WARN_COLOR}✗ {srv_status}{RESET}")
        else:
            lines.append(f"  Webhook server:   {OFF_COLOR}○ {srv_status}{RESET}")

        # Subscription count status
        if sub_ids:
            sub_count = len(sub_ids)
            lines.append(
                f"  Subscriptions:    {LIVE_COLOR}● {sub_count} active{RESET}"
            )
        else:
            lines.append(f"  Subscriptions:    {WARN_COLOR}○ none yet (subscribing...){RESET}")

        # Notification counter
        if notif_total > 0:
            lines.append(
                f"  Notifications:    {LIVE_COLOR}{notif_total} received{RESET}"
            )
            if last_notif:
                lines.append(f"  Last event:       {LIVE_COLOR}{last_notif}{RESET}")
        else:
            lines.append(f"  Notifications:    {OFF_COLOR}0 received (waiting for a stream.online push){RESET}")

        # Callback URL hint — stored in cfg, readable from the TwitchEventSub object's initial cfg
        if _eventsub is not None:
            cb_url = getattr(_eventsub, "_initial_cfg", {}).get("twitch_callback_url", "")
        else:
            cb_url = ""
        if cb_url:
            lines.append(f"  Callback URL:     {OFF_COLOR}{cb_url}{RESET}")
    # ─────────────────────────────────────────────────────────────────────────

    lines.append(f"{TITLE_COLOR}{'─' * 52}{RESET}")
    
    kb_out = KEYBIND_LABELS.get(KEYBIND_OUTPUT, KEYBIND_OUTPUT)
    kb_add = KEYBIND_LABELS.get(KEYBIND_ADD, KEYBIND_ADD.upper())
    kb_rem = KEYBIND_LABELS.get(KEYBIND_REMOVE, KEYBIND_REMOVE.upper())
    kb_dis = KEYBIND_LABELS.get(KEYBIND_DISABLE, KEYBIND_DISABLE.upper())

    lines.append(f"  {OFF_COLOR}press {kb_out} to switch to terminal mode{RESET}")
    lines.append(f"  {OFF_COLOR}press {kb_add} to add or enable a streamer{RESET}")
    lines.append(f"  {OFF_COLOR}press {kb_rem} to remove a streamer{RESET}")
    lines.append(f"  {OFF_COLOR}press {kb_dis} to disable a streamer{RESET}")
    
    sys.stdout.write(CLEAR + "\r\n".join(lines) + "\r\n")
    sys.stdout.flush()


def _dashboard_renderer_thread() -> None:
    """Continuously re-renders the dashboard while output mode 1 is active."""
    while not _dashboard_stop_event.is_set():
        with output_mode_lock:
            mode = OUTPUT_MODE
        if mode == 1:
            with dashboard_lock:
                next_in = dashboard_next_check_in
            #dbg(f"[DASHBOARD] render tick — next_check_in={next_in:.1f}s (id={id(dashboard_next_check_in)})")
            render_dashboard()
        # Use event.wait instead of time.sleep so shutdown can interrupt immediately
        _dashboard_stop_event.wait(timeout=1)


def _modify_config_streamer(config_path: str, username: str, action: str) -> str:
    """
    Edit the config file in-place to add or remove a streamer.

    action='add'    → remove from [Block] (if present), add to [Streamers]
    action='remove' → remove from [Streamers], add to [Block]

    Returns a human-readable result message.
    """
    username = username.strip().lower()
    if not username:
        return "No username provided."

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"ERROR reading config: {e}"

    # Parse section positions so we can do targeted insertions
    section_starts: dict = {}  # section_name -> line index of [Section] header
    current_section = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1]
            section_starts[current_section] = i

    def _remove_from_section(sec: str, name: str) -> bool:
        """Remove all lines that are exactly `name` (or `name =`) from section `sec`.
        Returns True if at least one line was removed."""
        if sec not in section_starts:
            return False
        removed = False
        # Determine the line range that belongs to this section
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
            # Adjust section_starts for sections after the deleted line
            for sec_name in list(section_starts.keys()):
                if section_starts[sec_name] > i:
                    section_starts[sec_name] -= 1

        return removed

    def _add_to_section(sec: str, name: str) -> None:
        """Append `name` as a new key at the end of section `sec`.
        Creates the section if it doesn't exist."""
        if sec not in section_starts:
            # Append a new section at the end of the file
            lines.append(f"\n[{sec}]\n")
            section_starts[sec] = len(lines) - 1

        # Find the end of this section
        sec_line = section_starts[sec]
        next_sec_line = len(lines)
        for other_sec, other_line in section_starts.items():
            if other_line > sec_line:
                next_sec_line = min(next_sec_line, other_line)

        # Check if it's already there (case-insensitive)
        for i in range(sec_line + 1, next_sec_line):
            key = lines[i].strip().split("=")[0].strip().lower()
            if key == name:
                return  # already present

        # Insert just before the next section (or EOF), after any trailing blank line
        insert_at = next_sec_line
        # Walk back past blank lines at the section boundary
        while insert_at > sec_line + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1

        lines.insert(insert_at, f"{name}\n")
        # Update section_starts for sections after insertion
        for sec_name in list(section_starts.keys()):
            if section_starts[sec_name] >= insert_at:
                section_starts[sec_name] += 1

    messages = []
    if action == "add":
        dbg(f"[CONFIG_EDIT] adding {username!r} to [Streamers], removing from [Block] if present")
        removed_from_block = _remove_from_section("Block", username)
        if removed_from_block:
            messages.append(f"Unblocked '{username}'.")
        _add_to_section("Streamers", username)
        messages.append(f"Added '{username}' to [Streamers].")
    elif action == "remove":
        dbg(f"[CONFIG_EDIT] removing {username!r} from [Streamers], adding to [Block]")
        removed_from_streamers = _remove_from_section("Streamers", username)
        if removed_from_streamers:
            messages.append(f"Removed '{username}' from [Streamers].")
        else:
            messages.append(f"'{username}' was not found in [Streamers].")
        _add_to_section("Block", username)
        messages.append(f"Added '{username}' to [Block].")
    elif action == "disable":
        dbg(f"[CONFIG_EDIT] disabling {username!r} — keeping in [Streamers], adding to [Block]")
        # Check if already in [Streamers]
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
            messages.append(f"Disabled '{username}' (added to [Block], kept in [Streamers]).")
        else:
            messages.append(f"'{username}' was not found in [Streamers].")

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        dbg(f"[CONFIG_EDIT] config written OK — result: {'  '.join(messages)}")
    except Exception as e:
        dbg(f"[CONFIG_EDIT] ERROR writing config: {e}")
        return f"ERROR writing config: {e}"

    return "  ".join(messages)


def _streamer_mgmt_thread() -> None:
    """
    Background thread that handles output modes 3 (add streamer) and 4 (remove streamer).
    Uses raw terminal input on Unix; falls back to normal input() on Windows.
    """
    TITLE_COLOR = "\033[96m"
    OK_COLOR    = "\033[92m"
    WARN_COLOR  = "\033[93m"
    RESET       = "\033[0m"
    CLEAR       = "\033[2J\033[H"

    def _read_line_raw() -> str:
        """Read a line of text from stdin in raw mode (Unix), echoing characters."""
        if sys.platform == "win32":
            import msvcrt
            buf = []
            sys.stdout.write("\n> ")
            sys.stdout.flush()
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\r", "\n"):
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return "".join(buf)
                    elif ch in ("\x08", "\x7f"):  # backspace
                        if buf:
                            buf.pop()
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                    elif ch == "\x03":  # Ctrl-C
                        os.kill(os.getpid(), __import__("signal").SIGINT)
                    elif ch in ("\x1c",):
                        os.kill(os.getpid(), __import__("signal").SIGINT)
                    else:
                        buf.append(ch)
                        sys.stdout.write(ch)
                        sys.stdout.flush()
                time.sleep(0.02)
        else:
            import tty, termios
            fd = sys.stdin.fileno()
            try:
                old = termios.tcgetattr(fd)
            except termios.error:
                # Not a tty — fall back
                sys.stdout.write("\n> ")
                sys.stdout.flush()
                return sys.stdin.readline().rstrip("\n")
            buf = []
            sys.stdout.write("\n> ")
            sys.stdout.flush()
            try:
                tty.setraw(fd)
                while True:
                    ch = sys.stdin.read(1)
                    if ch in ("\r", "\n"):
                        sys.stdout.write("\r\n")
                        sys.stdout.flush()
                        return "".join(buf)
                    elif ch in ("\x08", "\x7f"):  # backspace
                        if buf:
                            buf.pop()
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                    elif ch == "\x03":
                        os.kill(os.getpid(), __import__("signal").SIGINT)
                    elif ch in ("\x1c",):
                        os.kill(os.getpid(), __import__("signal").SIGINT)
                    else:
                        buf.append(ch)
                        sys.stdout.write(ch)
                        sys.stdout.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

    while True:
        _streamer_mgmt_event.wait()
        _streamer_mgmt_event.clear()

        with output_mode_lock:
            mode = OUTPUT_MODE

        if mode not in (3, 4, 5):
            continue

        if mode == 3:
            title = "Add a Streamer"
            prompt = "Enter streamer username to ADD or ENABLE"
            action = "add"
        elif mode == 4:
            title = "Remove a Streamer"
            prompt = "Enter streamer username to REMOVE"
            action = "remove"
        else:
            title = "Disable a Streamer"
            prompt = "Enter streamer username to DISABLE"
            action = "disable"

        result_msg = ""

        while True:
            with output_mode_lock:
                current_mode = OUTPUT_MODE
            if current_mode not in (3, 4, 5):
                break

            # Build streamer list for ADD, REMOVE and DISABLE modes
            streamer_list_block = ""
            if action in ("add", "remove", "disable"):
                with dashboard_lock:
                    current_streamers = list(dashboard_all_streamers)
                if current_streamers:
                    streamer_lines = "\r\n".join(
                        f"    \033[90m· {s}\033[0m" for s in current_streamers
                    )
                    streamer_list_block = (
                        f"  \033[96mStreamers:\033[0m\r\n"
                        f"{streamer_lines}\r\n"
                        f"\r\n"
                    )
                else:
                    streamer_list_block = f"  \033[90m(no streamers configured)\033[0m\r\n\r\n"

            # Re-draw the page
            header = (
                f"{CLEAR}"
                f"{TITLE_COLOR}{'─' * 52}{RESET}\r\n"
                f"{TITLE_COLOR}  jj-dlp · {title}{RESET}\r\n"
                f"{TITLE_COLOR}{'─' * 52}{RESET}\r\n"
                f"\r\n"
            )
            footer = (
                f"\r\n{TITLE_COLOR}{'─' * 52}{RESET}\r\n"
                f"  \033[90mPress Enter to jump back to Dashboard\033[0m\r\n"
            )
            if result_msg:
                body = f"  {OK_COLOR}{result_msg}{RESET}\r\n"
            else:
                body = ""

            sys.stdout.write(
                header + streamer_list_block + body +
                f"  {WARN_COLOR}{prompt}:{RESET}" + footer
            )
            sys.stdout.flush()

            username = _read_line_raw()

            # Check if mode changed while we were waiting for input
            with output_mode_lock:
                current_mode = OUTPUT_MODE
            if current_mode not in (3, 4, 5):
                break

            if username == "__REDRAW__":
                result_msg = ""
                continue

            if not username.strip():
                _set_output_mode(1)
                break

            cfg_path = _config_path
            if not cfg_path:
                result_msg = "ERROR: config path not available."
                continue

            result_msg = _modify_config_streamer(cfg_path, username, action)

            # Trigger a config reload / immediate check
            trigger_full_check_event.set()

            # Re-draw once so the user sees the confirmation message
            header = (
                f"{CLEAR}"
                f"{TITLE_COLOR}{'─' * 52}{RESET}\r\n"
                f"{TITLE_COLOR}  jj-dlp · {title}{RESET}\r\n"
                f"{TITLE_COLOR}{'─' * 52}{RESET}\r\n"
            )
            footer = (
                f"\r\n{TITLE_COLOR}{'─' * 52}{RESET}\r\n"
            )
            body = f"  {OK_COLOR}{result_msg}{RESET}\r\n"

            sys.stdout.write(header + body + footer)
            sys.stdout.flush()

            # Wait so user can read it
            time.sleep(1.0)

            # Return to dashboard
            _set_output_mode(1)
            break

def log(msg: str) -> None:
    with output_mode_lock:
        mode = OUTPUT_MODE
    if mode in (1, 3, 4, 5):
        return  # dashboard / streamer mgmt pages own the screen
    # mode 2 (terminal): suppress log() when verbosity is 4 (stderr only)
    with verbosity_lock:
        v = VERBOSITY
    if v == 4:
        return  # stderr-only verbosity — log() messages are suppressed
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
    with output_mode_lock:
        mode = OUTPUT_MODE
    if mode in (1, 3, 4, 5):
        return  # dashboard / streamer mgmt pages own the screen
    with verbosity_lock:
        v = VERBOSITY
    # Show debug output in verbosity 3 (log+stdout+debug) and 5 (everything)
    if v in (3, 5):
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


def _drain_pipe(pipe, log_fp, pipe_type: str,
                ffmpeg_error_counter=None,
                ffmpeg_error_event=None,
                streamer: str = "") -> None:
    """
    Read lines from *pipe* until EOF.
    - Writes each line to *log_fp* if provided.
    - Prints to terminal when OUTPUT_MODE == 2 and VERBOSITY includes this stream.
    - If ffmpeg_error_counter and ffmpeg_error_event are provided, scans each
      line for FFMPEG_ERROR_PATTERNS and increments the shared counter.
      When the counter exceeds FFMPEG_ERROR_RESTART_THRESHOLD, sets the event
      to signal record_stream to kill and restart yt-dlp.

    pipe_type: 'stdout' or 'stderr' — used to determine if this pipe should be
               shown given the current verbosity level.
    ffmpeg_error_counter: a single-element list [int] so it can be mutated
                          across threads without a lock (GIL-safe for += 1).
    ffmpeg_error_event:   threading.Event set when the threshold is crossed.
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
            if mode == 2:
                with verbosity_lock:
                    v = VERBOSITY
                # stdout shown in verbosity 2 (stdout), 3 (debug), 5 (everything)
                # stderr shown in verbosity 4 (stderr), 5 (everything)
                show = (
                    (pipe_type == "stdout" and v in (2, 3, 5)) or
                    (pipe_type == "stderr" and v in (4, 5))
                )
                if show:
                    print(line, flush=True)

            # FFmpeg error pattern monitoring
            if (ffmpeg_error_counter is not None
                    and ffmpeg_error_event is not None
                    and FFMPEG_ERROR_RESTART_THRESHOLD > 0
                    and not ffmpeg_error_event.is_set()):
                line_lower = line.lower()
                for pattern in FFMPEG_ERROR_PATTERNS:
                    if pattern.lower() in line_lower:
                        ffmpeg_error_counter[0] += 1
                        dbg(f"[FFMPEG_MONITOR] [{streamer}]: pattern '{pattern}' matched "
                            f"(count={ffmpeg_error_counter[0]}/{FFMPEG_ERROR_RESTART_THRESHOLD})")
                        if ffmpeg_error_counter[0] >= FFMPEG_ERROR_RESTART_THRESHOLD:
                            log(f"ffmpeg_monitor [{streamer}]: error threshold reached "
                                f"({ffmpeg_error_counter[0]} matches) — signalling restart.")
                            ffmpeg_error_event.set()
                        break  # only count once per line even if multiple patterns match
    except Exception:
        pass


def build_yt_dlp_command(yt_dlp_path: str, base_cmd: List[str], extra: List[str], ffmpeg_path: str = "") -> List[str]:
    ffmpeg_args = ["--ffmpeg-location", ffmpeg_path] if ffmpeg_path else []
    return [yt_dlp_path, *ffmpeg_args, *base_cmd, *extra]


def get_live_streamers(streamers: List[str], cfg: dict) -> List[str]:
    if not streamers:
        return []

    streamers = [s for s in streamers if s not in cfg["blocked"]]
    if not streamers:
        dbg(f"[CHECKER] get_live_streamers: all streamers are blocked — skipping check")
        return []

    urls = [cfg["site_tmpl"].format(username=s) for s in streamers]
    cmd = build_yt_dlp_command(cfg["yt_dlp_path"], cfg["checker_cmd"], urls, cfg.get("ffmpeg_path", ""))
    dbg(f"[CHECKER] running liveness check for {streamers} — cmd={cmd}")

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    dbg(f"[CHECKER] yt-dlp check returncode={result.returncode}")

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
                    dbg(f"[CHECKER] detected live: {streamer!r} (url={url!r})")
                    live.append(streamer)
        except Exception:
            if cfg.get("logging_enabled"):
                try:
                    out_path, _ = get_log_file_paths(cfg)
                    with open(out_path, "a", encoding="utf-8") as lf:
                        lf.write(f"JSON parse error for line: {line}")
                except Exception:
                    pass
            dbg(f"[CHECKER] JSON parse error for line: {line!r}")
            continue
    dbg(f"[CHECKER] get_live_streamers result: {live}")
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

        #dbg(f"[STALL_CHECKER] file: {os.path.basename(filename) or '<none>'}")

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

    dbg(f"[RECORD] thread started for {streamer!r} — url={channel_url!r} output_dir={output_dir!r}")
    log(f"Recording started: {streamer}  (saving to: {output_dir})\n")

    proc = None
    close_logs = lambda: None

    try:
        while True:
            cmd = build_yt_dlp_command(cfg["yt_dlp_path"], cfg["downloader_cmd"], ["-o", output_path, channel_url], cfg.get("ffmpeg_path", ""))
            dbg(f"[RECORD] launching yt-dlp for {streamer!r} — cmd={cmd}")
            out_target, err_target, close_logs, log_out_fp, log_err_fp = open_log_streams(cfg)

            try:
                proc = subprocess.Popen(cmd, stdout=out_target, stderr=err_target)
                proc_start_time = time.time()
                dbg(f"[RECORD] yt-dlp process started for {streamer!r} — pid={proc.pid}")

                # Shared ffmpeg error counter and signal event for this yt-dlp session.
                # A single-element list is used so both drain threads can safely
                # increment it under the GIL without an explicit lock.
                ffmpeg_error_counter = [0]
                ffmpeg_error_event = threading.Event()

                # Drain subprocess pipes in background threads so the main
                # record loop is never blocked.  Each drainer respects the
                # current OUTPUT_MODE to decide whether to print to terminal.
                threading.Thread(
                    target=_drain_pipe,
                    args=(proc.stdout, log_out_fp, "stdout"),
                    kwargs={"ffmpeg_error_counter": ffmpeg_error_counter,
                            "ffmpeg_error_event": ffmpeg_error_event,
                            "streamer": streamer},
                    daemon=True,
                ).start()
                threading.Thread(
                    target=_drain_pipe,
                    args=(proc.stderr, log_err_fp, "stderr"),
                    kwargs={"ffmpeg_error_counter": ffmpeg_error_counter,
                            "ffmpeg_error_event": ffmpeg_error_event,
                            "streamer": streamer},
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

            #dbg(f"[STALL_CHECKER] Setting initial last_size for {streamer}: {last_size} bytes "
                #f"(file: {os.path.basename(current_filename) if current_filename else '<none>'})")

            last_growth_time = time.time()
            #dbg(f"[STALL_CHECKER] Setting initial last_growth_time for {streamer}: {last_growth_time}")

            stall_check_interval = cfg["stall_check_interval"]
            stall_timeout = cfg["stall_timeout"]
            seconds_since_check = 0
            #dbg(f"[STALL_CHECKER] The first stall check will be in {stall_check_interval} seconds.")

            while proc.poll() is None:  # While process is still running
                current_cfg = load_config(cfg["config_path"])

                if streamer in current_cfg["blocked"]:
                    dbg(f"[RECORD] {streamer!r} is now blocked — killing yt-dlp (pid={proc.pid})")
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

                # Check if ffmpeg error threshold was crossed by a drain thread
                if ffmpeg_error_event.is_set():
                    log(f"\n\nffmpeg_monitor [{streamer}]: restarting yt-dlp due to ffmpeg errors "
                        f"(threshold={FFMPEG_ERROR_RESTART_THRESHOLD}).\n\n")
                    dbg(f"[RECORD] killing yt-dlp (pid={proc.pid}) for {streamer!r} — ffmpeg error threshold crossed")
                    kill_proc(proc)
                    try:
                        close_logs()
                    except Exception:
                        pass
                    time.sleep(5)
                    break

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
                        dbg(f"[STALL_CHECKER] {filename_display} grew by {current_size - last_size} bytes ({last_size} --> {current_size})")
                        #dbg(f"[STALL_CHECKER] Setting new last_size for {filename_display}: {current_size} bytes")
                        last_size = current_size
                        last_growth_time = time.time()
                        #dbg(f"[STALL_CHECKER] Setting new last_growth_time for {filename_display}: {last_growth_time}")
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
                dbg(f"[RECORD] yt-dlp process exited naturally for {streamer!r} — returncode={proc.returncode}")
                log(f"Recording finished: {streamer}")
                break

    except KeyboardInterrupt:
        dbg(f"[RECORD] KeyboardInterrupt received for {streamer!r} — killing yt-dlp")
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
        dbg(f"[RECORD] thread finalizing for {streamer!r} — cooldown={cfg['cooldown']}s")
        time.sleep(cfg["cooldown"])


def start_recording_if_needed(live_now: List[str], cfg: dict, show_popup: bool = True) -> None:
    global recording_threads

    with lock:
        to_start = [s for s in live_now if s not in currently_recording and s not in cfg["blocked"]]
        if not to_start:
            dbg(f"[RECORD] start_recording_if_needed: nothing new to start — live_now={live_now} currently_recording={currently_recording}")
            recording_threads[:] = [t for t in recording_threads if t.is_alive()]
            return

        for streamer in to_start:
            currently_recording.add(streamer)
            dbg(f"[RECORD] start_recording_if_needed: launching recording thread for {streamer!r}")
            # Track when this streamer went live for the dashboard
            with dashboard_lock:
                if streamer not in dashboard_live_since:
                    dashboard_live_since[streamer] = time.time()
            # Show popup notification (poll path)
            if show_popup and cfg.get("popup_notifications", True):
                dbg(f"[POPUP] [poll] triggering popup for {streamer!r}")
                _show_live_popup(streamer, source="poll", popup_timeout=cfg.get("popup_timeout", 15))
            elif not show_popup:
                dbg(f"[POPUP] [poll] popup suppressed (already shown by eventsub) for {streamer!r}")
            else:
                dbg(f"[POPUP] [poll] popup disabled via config — skipping for {streamer!r}")
            t = threading.Thread(target=record_stream, args=(streamer, cfg), daemon=True)
            t.start()
            recording_threads.append(t)

        recording_threads[:] = [t for t in recording_threads if t.is_alive()]


def config_watcher(config_path: str, poll_interval: int = 3) -> None:
    dbg(f"[CONFIG_WATCHER] thread started")
    prev_streamers: Set[str] = set()
    first_run = True

    while True:
        try:
            cfg = load_config(config_path)
            curr_streamers = set(cfg.get("streamers", []))
            blocked = set(cfg.get("blocked", []))

            #dbg(f"[CONFIG_WATCHER] curr_streamers={curr_streamers} prev_streamers={prev_streamers} first_run={first_run}")

            if first_run:
                prev_streamers = curr_streamers
                first_run = False
                dbg(f"[CONFIG_WATCHER] first run — baseline set, skipping trigger check")
            else:
                added = [s for s in (curr_streamers - prev_streamers) if s not in blocked]
                #dbg(f"[CONFIG_WATCHER] added={added}")
                if added: 
                    log(f"config_watcher: new streamer(s) detected: {', '.join(added)} — triggering immediate full check")
                    with lock:
                        known_streamers.update(curr_streamers)
                    trigger_full_check_event.set()
                    dbg(f"[CONFIG_WATCHER] trigger_full_check_event SET")
                prev_streamers = curr_streamers

        except Exception as e:
            dbg(f"[CONFIG_WATCHER] exception during poll: {e}")

        time.sleep(poll_interval)


# ══════════════════════════════════════════════════════════════════════════════
# Twitch EventSub  —  instant "stream.online" notifications
# ══════════════════════════════════════════════════════════════════════════════
from integrations.twitch_eventsub import TwitchEventSub, EventSubState

# Shared state object — read by render_dashboard(), written by TwitchEventSub
_eventsub_state = EventSubState()

# Convenience aliases so the dashboard renderer code stays unchanged
def _eventsub_get_status():
    return _eventsub_state.get_server_status()

_eventsub_stop_event = threading.Event()
_eventsub_manage_lock = threading.Lock()

# The TwitchEventSub instance (set in main() if twitch_enabled)
_eventsub: TwitchEventSub = None
# ══════════════════════════════════════════════════════════════════════════════


# ── Twitch EventSub functions have moved to integrations/twitch_eventsub.py ──
# All logic below (_twitch_get_token, _twitch_api, _twitch_resolve_user_ids,
# _twitch_subscribe, _twitch_unsubscribe, _eventsub_verify_signature,
# _eventsub_handle_request, _eventsub_http_server, _eventsub_sync_subscriptions,
# _eventsub_manager_thread) is now encapsulated in TwitchEventSub.
# ─────────────────────────────────────────────────────────────────────────────



# ══════════════════════════════════════════════════════════════════════════════


def _ensure_ffmpeg(ffmpeg_path: str) -> str:
    """
    Verify that ffmpeg exists at ffmpeg_path.
    If not found, prompt the user to install it via winget.
    Returns the resolved ffmpeg path on success, or exits on failure.
    """
    # Resolve relative paths against the script directory so the check is
    # consistent regardless of where the script was launched from.
    resolved = ffmpeg_path
    if not os.path.isabs(resolved) and os.sep in resolved or (os.altsep and os.altsep in resolved):
        # Looks like a relative file path (e.g. "bin/ffmpeg.exe") — resolve it
        resolved = os.path.join(os.path.dirname(os.path.abspath(__file__)), resolved)
        if os.path.isfile(resolved):
            return resolved
    else:
        # Plain command name (e.g. "ffmpeg") — check PATH via shutil.which
        import shutil
        if shutil.which(resolved):
            return resolved

    # ── ffmpeg not found ──────────────────────────────────────────────────────
    WARN  = "\033[93m"
    INFO  = "\033[96m"
    OK    = "\033[92m"
    ERR   = "\033[91m"
    RESET = "\033[0m"

    print(f"\n{WARN}WARNING: ffmpeg not found at '{ffmpeg_path}' (resolved: '{resolved}'){RESET}")
    print(f"{INFO}ffmpeg is required for recording streams.{RESET}\n")

    # Check whether winget is available on this machine
    winget_available = False
    try:
        result = subprocess.run(
            ["winget", "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        winget_available = result.returncode == 0
    except FileNotFoundError:
        winget_available = False

    if winget_available:
        print("ffmpeg can be installed automatically using winget.\n")
        try:
            answer = input("  Install ffmpeg now? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        if answer in ("", "y", "yes"):
            print(f"\n{INFO}Running: winget install Gyan.FFmpeg{RESET}\n")
            try:
                ret = subprocess.run(
                    ["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--accept-source-agreements", "--accept-package-agreements"],
                    check=False
                )
            except Exception as e:
                print(f"{ERR}winget failed to launch: {e}{RESET}")
                ret = None

            if ret is not None and ret.returncode == 0:
                print(f"\n{OK}ffmpeg installed successfully!{RESET}")
                print(f"{WARN}NOTE: The PATH update won't take effect until you restart your terminal.")
                print(f"      Please relaunch jj-dlp and it will find ffmpeg automatically.{RESET}\n")
                input("Press Enter to exit...")
                sys.exit(0)
        else:
            print()  # blank line before manual instructions

    # ── Manual instructions ───────────────────────────────────────────────────
    print(f"{INFO}To install ffmpeg manually, run the following in a terminal:{RESET}\n")
    print(f"    winget install --id Gyan.FFmpeg -e\n")
    print("After installing, either:")
    print(f"  • Set  FFMPEG_PATH = ffmpeg  in your config (uses the system PATH), or")
    print(f"  • Run  install-ffmpeg.bat  if included in the repo.\n")
    print(f"{ERR}Cannot continue without ffmpeg. Exiting.{RESET}\n")
    input("Press Enter to exit...")
    sys.exit(1)


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
                print("\nThe following .conf files were discovered (skip this step by passing --config <path>):\n\n")
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

    # Verify ffmpeg is present only when FFMPEG_PATH is explicitly set in the config.
    # If no path was given we leave ffmpeg_path as "" so --ffmpeg-location is omitted
    # entirely and yt-dlp resolves ffmpeg on its own (e.g. via PATH).
    if initial_cfg.get("ffmpeg_path", ""):
        initial_cfg["ffmpeg_path"] = _ensure_ffmpeg(initial_cfg["ffmpeg_path"])

    global VERBOSITY, DEBUG_LOGS_ENABLED, DEBUG_LOG_PATH, dashboard_next_check_in, _config_path
    _config_path = config_path
    with verbosity_lock:
        VERBOSITY = initial_cfg.get("verbosity", 1)

    with debug_log_lock:
        DEBUG_LOGS_ENABLED = initial_cfg.get("debug_logs", False)
        DEBUG_LOG_PATH     = get_debug_log_path(initial_cfg) if DEBUG_LOGS_ENABLED else ""

    kb_v = KEYBIND_LABELS.get(KEYBIND_VERBOSITY, KEYBIND_VERBOSITY)
    v = VERBOSITY
    log(f"Verbosity: {v} — {VERBOSITY_NAMES.get(v, v)} ({VERBOSITY_DESC.get(v, '')})  (press {kb_v} to cycle)")
    log(f"Config file: {config_path}")
    log(f"Output directory: {initial_cfg['output_dir']}")

    if initial_cfg["logging_enabled"]:
        log(f"Log file: {get_log_path(initial_cfg)}")

    if initial_cfg.get("debug_logs"):
        log(f"Debug log: {get_debug_log_path(initial_cfg)}")
                                                                      

    log(f"Check interval: {initial_cfg['check_interval']}s")
    kb_o = KEYBIND_LABELS.get(KEYBIND_OUTPUT, KEYBIND_OUTPUT)
    kb_b = KEYBIND_LABELS.get(KEYBIND_VERBOSITY, KEYBIND_VERBOSITY)
    log(f"Output mode: {OUTPUT_MODE} — {OUTPUT_MODE_NAMES[OUTPUT_MODE]}  (press {kb_o} to toggle dashboard/terminal, {kb_b} to cycle verbosity)\n")

    keyboard_thread = threading.Thread(target=_keyboard_listener, daemon=True)
    keyboard_thread.start()
    dbg(f"[MAIN] keyboard_listener thread launched")

    dashboard_thread = threading.Thread(target=_dashboard_renderer_thread, daemon=True)
    dashboard_thread.start()
    dbg(f"[MAIN] dashboard_renderer thread launched")

    streamer_mgmt_thread = threading.Thread(target=_streamer_mgmt_thread, daemon=True)
    streamer_mgmt_thread.start()
    dbg(f"[MAIN] streamer_mgmt thread launched")

    watcher_interval = initial_cfg.get("config_check_interval", 3)
    watcher_thread = threading.Thread(
        target=config_watcher,
        args=(config_path, watcher_interval),
        daemon=True
    )
    watcher_thread.start()
    dbg(f"[MAIN] config_watcher thread launched (poll every {watcher_interval}s)")

    # ── Twitch EventSub (optional) ────────────────────────────────────────────
    global _eventsub
    if initial_cfg.get("twitch_enabled"):
        log("[Twitch] EventSub: credentials found — starting webhook listener and subscription manager")

        def _on_stream_online(broadcaster_login: str, cfg: dict) -> None:
            """Called by TwitchEventSub when a stream.online push is received."""
            # Update dashboard live_since immediately
            with dashboard_lock:
                if broadcaster_login not in dashboard_live_since:
                    dashboard_live_since[broadcaster_login] = time.time()
                    dbg(f"[TWITCH] on_stream_online: updated dashboard_live_since for {broadcaster_login}")
            # Load fresh config and start recording
            dbg(f"[TWITCH] on_stream_online: loading config to verify {broadcaster_login} is in [Streamers]")
            current_cfg  = load_config(cfg["config_path"])
            in_streamers = broadcaster_login in current_cfg.get("streamers", [])
            is_blocked   = broadcaster_login in current_cfg.get("blocked", [])
            dbg(f"[TWITCH] on_stream_online: {broadcaster_login} in_streamers={in_streamers}  is_blocked={is_blocked}")
            if in_streamers and not is_blocked:
                if current_cfg.get("popup_notifications", True):
                    _show_live_popup(broadcaster_login, source="eventsub",
                                     popup_timeout=current_cfg.get("popup_timeout", 15))
                start_recording_if_needed([broadcaster_login], current_cfg, show_popup=False)
            else:
                dbg(f"[TWITCH] on_stream_online: skipping {broadcaster_login} "
                    f"(in_streamers={in_streamers}, is_blocked={is_blocked})")
                log(f"[Twitch] EventSub: {broadcaster_login} notified as live but is not in "
                    "[Streamers] or is blocked — skipping")

        _eventsub = TwitchEventSub(
            cfg              = initial_cfg,
            state            = _eventsub_state,
            on_stream_online = _on_stream_online,
            load_config_fn   = load_config,
            dbg_fn           = dbg,
            log_fn           = log,
        )
        _eventsub.start()
    else:
        dbg(f"[MAIN] Twitch EventSub not configured — polling only")
    # ─────────────────────────────────────────────────────────────────────────

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
            dbg(f"[DASHBOARD] [WRITE] zeroed before liveness check (id={id(dashboard_next_check_in)})")

            dbg(f"[MAIN] top of loop — streamers={streamers} currently_recording={currently_recording} event_is_set={trigger_full_check_event.is_set()}")

            if not streamers:
                log("ERROR: No streamers configured. Retrying next cycle.")
            else:
                log(f"Checking live status for {', '.join(streamers)}\n")
                live_now = get_live_streamers(streamers, cfg)
                dbg(f"[MAIN] get_live_streamers returned: {live_now}")

                cfg = load_config(config_path) # fixes a bug with restarting the record thread after blocking a streamer

                # Update dashboard: mark offline streamers, keep live ones
                with dashboard_lock:
                    dashboard_all_streamers.clear()
                    dashboard_all_streamers.extend(streamers)
                    dashboard_blocked_streamers.clear()
                    dashboard_blocked_streamers.update(cfg["blocked"])
                    live_set = set(live_now)
                    for s in streamers:
                        if s not in live_set:
                            dashboard_live_since.pop(s, None)
                        elif s not in dashboard_live_since:
                            dashboard_live_since[s] = time.time()

                dbg(f"[DASHBOARD] state updated — live={list(live_set)} all={streamers}")

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
            dbg(f"[DASHBOARD] [WRITE] seeded to {wait_secs}s (id={id(dashboard_next_check_in)}, deadline={deadline:.3f})")

            dbg(f"[MAIN] entering wait loop (timeout={wait_secs}s)\n")
            try:
                triggered = False
                while True:
                    remaining = deadline - time.time()
                    with dashboard_lock:
                        dashboard_next_check_in = max(0.0, remaining)
                    #dbg(f"[DASHBOARD] countdown tick — remaining={remaining:.1f}s")
                    if remaining <= 0:
                        # Zero it out explicitly so the display shows 0 cleanly
                        # while the next liveness check runs.
                        with dashboard_lock:
                            dashboard_next_check_in = 0.0
                        dbg(f"[DASHBOARD] countdown reached 0 — breaking wait loop")
                        break
                    fired = trigger_full_check_event.wait(timeout=min(1.0, remaining))
                    if fired:
                        triggered = True
                        with dashboard_lock:
                            dashboard_next_check_in = 0.0
                        dbg(f"[DASHBOARD] early trigger received — countdown zeroed")
                        break
                dbg(f"[MAIN] wait returned — triggered={triggered} event_is_set={trigger_full_check_event.is_set()}")
                if triggered:
                    trigger_full_check_event.clear()
                    dbg(f"[MAIN] event cleared, looping immediately for full check")
                else:
                    dbg(f"[MAIN] normal timeout elapsed, proceeding to next cycle")
            except Exception as e:
                dbg(f"[MAIN] wait raised exception: {e} — falling back to sleep")
                time.sleep(wait_secs)

    except KeyboardInterrupt:
        # Signal the dashboard renderer to stop immediately (no waiting for its sleep to expire)
        _dashboard_stop_event.set()
        if _eventsub is not None:
            _eventsub.stop(timeout=5)  # signal EventSub threads to clean up

        TITLE_COLOR = "\033[96m"
        WARN_COLOR  = "\033[93m"
        OK_COLOR    = "\033[92m"
        RESET       = "\033[0m"
        CLEAR       = "\033[2J\033[H"

        active_recordings = [t for t in recording_threads if t.is_alive()]

        sys.stdout.write(CLEAR)
        sys.stdout.write(f"{TITLE_COLOR}{'─' * 52}{RESET}\r\n")
        sys.stdout.write(f"{TITLE_COLOR}  jj-dlp  ·  Shutting down...{RESET}\r\n")
        sys.stdout.write(f"{TITLE_COLOR}{'─' * 52}{RESET}\r\n\r\n")

        if active_recordings:
            sys.stdout.write(
                f"  {WARN_COLOR}Waiting for {len(active_recordings)} active recording(s) to finish...{RESET}\r\n\r\n"
            )
        sys.stdout.flush()

        for t in recording_threads:
            if t.is_alive():
                t.join(timeout=15)  # Wait 15 seconds for each thread to finish gracefully

        sys.stdout.write(f"  {OK_COLOR}✓  All done. Goodbye!{RESET}\r\n\r\n")
        sys.stdout.flush()


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