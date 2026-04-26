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

    debug_logs = general.get("DEBUG_LOGS", "false").strip().lower() == "true"
    debug_log_path_raw = general.get("DEBUG_LOG_PATH", "").strip().strip('\"\'')
    debug_log_path = debug_log_path_raw if debug_log_path_raw else ""

    yt_dlp_path_raw = general.get("YT_DLP_PATH", "").strip().strip('"\'')
    yt_dlp_path = yt_dlp_path_raw if yt_dlp_path_raw else "yt-dlp"

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
        "checker_cmd": checker_cmd,
        "downloader_cmd": downloader_cmd,
        "config_check_interval": config_check_interval,
        "verbosity": verbosity,
        "logging_enabled": logging_enabled,
        "log_path": log_path,
        "split_logs": split_logs,
        "popup_notifications": popup_notifications,
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
#   6 = add a streamer
#   7 = remove a streamer
OUTPUT_MODE = 1
OUTPUT_MODE_NAMES = {
    1: "dashboard   (live status overview)",
    2: "clean       (stdout+stderr suppressed)",
    3: "stdout only",
    4: "stderr only",
    5: "everything  (stdout+stderr shown)",
    6: "add a streamer",
    7: "remove a streamer",
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
FFMPEG_ERROR_RESTART_THRESHOLD: int = 1000
# ──────────────────────────────────────────────────────────────────────

# ── Keybind Configuration ──────────────────────────────────────────────
# Use single characters OR control codes like '\x16' (Ctrl+V)

KEYBIND_VERBOSITY = "\x02"   # Ctrl+B
KEYBIND_OUTPUT    = "\x0f"   # Ctrl+O
KEYBIND_ADD       = "a"
KEYBIND_REMOVE    = "r"

# Optional: human-readable labels for UI
KEYBIND_LABELS = {
    KEYBIND_VERBOSITY: "Ctrl+B",
    KEYBIND_OUTPUT:    "Ctrl+O",
    KEYBIND_ADD:       "A",
    KEYBIND_REMOVE:    "R",
}
# ───────────────────────────────────────────────────────────────────────


# Shared config path (set in main, used by streamer management modes)
_config_path: str = ""

# debug.log file output (DEBUG_LOGS = true in config)
DEBUG_LOGS_ENABLED: bool = False
DEBUG_LOG_PATH: str = ""
debug_log_lock = threading.Lock()

# Event used to wake the streamer-management thread when mode 6/7 becomes active
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
# Set when shutting down so the renderer stops immediately without waiting for its sleep to expire
_dashboard_stop_event = threading.Event()


# ── Popup notification ────────────────────────────────────────────────────────
def _show_live_popup(streamer: str, source: str = "poll") -> None:
    """
    Show a non-blocking tkinter popup when a streamer goes live.
    source: 'poll' (normal yt-dlp loop) or 'eventsub' (Twitch EventSub push).
    Runs in its own daemon thread so it never blocks the main loop.
    tkinter is optional — if unavailable, the popup is silently skipped.
    """
    dbg(f"popup: _show_live_popup called — streamer={streamer!r}  source={source!r}")

    def _run():
        dbg(f"popup: thread started — streamer={streamer!r}  source={source!r}")
        try:
            import tkinter as tk
            dbg(f"popup: tkinter imported OK")

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

            dbg(f"popup: window created — scheduling auto-close in 15s")
            # Auto-close after 15 seconds
            win.after(15000, win.destroy)

            dbg(f"popup: entering mainloop")
            root.mainloop()
            dbg(f"popup: mainloop exited for streamer={streamer!r}")
        except ImportError:
            dbg("popup: tkinter not available — skipping popup notification")
        except Exception as e:
            dbg(f"popup: exception in _run — {type(e).__name__}: {e}")

    t = threading.Thread(target=_run, daemon=True, name=f"popup-{streamer}")
    t.start()
    dbg(f"popup: daemon thread launched (name={t.name!r})")
# ─────────────────────────────────────────────────────────────────────────────


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
        # Only cycle through modes 1–5
        if OUTPUT_MODE >= 5 or OUTPUT_MODE < 1:
            OUTPUT_MODE = 1
        else:
            OUTPUT_MODE += 1

        mode = OUTPUT_MODE
        name = OUTPUT_MODE_NAMES[OUTPUT_MODE]

    if mode != 1:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [output mode {mode}] {name}", flush=True)


def _set_output_mode(mode: int) -> None:
    """Jump directly to a specific output mode."""
    global OUTPUT_MODE
    with output_mode_lock:
        OUTPUT_MODE = mode
    if mode in (6, 7):
        _stdin_owned_by_mgmt.set()
        _streamer_mgmt_event.set()
    else:
        _stdin_owned_by_mgmt.clear()
        if mode != 1:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            name = OUTPUT_MODE_NAMES.get(mode, "")
            print(f"[{ts}] [output mode {mode}] {name}", flush=True)


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
                    _set_output_mode(6)
                elif ch.lower() == KEYBIND_REMOVE.lower():
                    _set_output_mode(7)

            time.sleep(0.05)

    else:
        import tty
        import termios
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            return

        try:
            tty.setraw(fd)
            while True:
                # Yield stdin to the streamer-mgmt thread while it owns it.
                # We must restore normal tty settings so _read_line_raw can
                # take over, then re-enter raw mode when control returns to us.
                if _stdin_owned_by_mgmt.is_set():
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    _stdin_owned_by_mgmt.wait()  # block until mgmt releases stdin
                    tty.setraw(fd)
                    continue

                ch = sys.stdin.read(1)

                if ch == KEYBIND_VERBOSITY:
                    cycle_verbosity()
                elif ch == KEYBIND_OUTPUT:
                    cycle_output_mode()
                elif ch.lower() == KEYBIND_ADD.lower():
                    _set_output_mode(6)
                elif ch.lower() == KEYBIND_REMOVE.lower():
                    _set_output_mode(7)
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

    # ── Twitch EventSub status block ──────────────────────────────────────────
    with _eventsub_status_lock:
        srv_status    = _eventsub_server_status
        last_notif    = _eventsub_last_notification
        notif_total   = _eventsub_notifications_total
    with _eventsub_sub_lock:
        sub_ids       = dict(_eventsub_subscription_ids)
    eventsub_configured = bool(_eventsub_cfg.get("twitch_enabled") or sub_ids)

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

        # Callback URL hint
        cb_url = _eventsub_cfg.get("twitch_callback_url", "")
        if cb_url:
            lines.append(f"  Callback URL:     {OFF_COLOR}{cb_url}{RESET}")
    # ─────────────────────────────────────────────────────────────────────────

    lines.append(f"{TITLE_COLOR}{'─' * 52}{RESET}")
    
    kb_out = KEYBIND_LABELS.get(KEYBIND_OUTPUT, KEYBIND_OUTPUT)
    kb_add = KEYBIND_LABELS.get(KEYBIND_ADD, KEYBIND_ADD.upper())
    kb_rem = KEYBIND_LABELS.get(KEYBIND_REMOVE, KEYBIND_REMOVE.upper())

    lines.append(f"  {OFF_COLOR}press {kb_out} to cycle through output modes{RESET}")
    lines.append(f"  {OFF_COLOR}press {kb_add} to add a streamer{RESET}")
    lines.append(f"  {OFF_COLOR}press {kb_rem} to remove a streamer{RESET}")
    
    sys.stdout.write(CLEAR + "\n".join(lines) + "\n")
    sys.stdout.flush()


def _dashboard_renderer_thread() -> None:
    """Continuously re-renders the dashboard while output mode 1 is active."""
    while not _dashboard_stop_event.is_set():
        with output_mode_lock:
            mode = OUTPUT_MODE
        if mode == 1:
            with dashboard_lock:
                next_in = dashboard_next_check_in
            dbg(f"dashboard: render tick — next_check_in={next_in:.1f}s (id={id(dashboard_next_check_in)})")
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
        removed_from_block = _remove_from_section("Block", username)
        if removed_from_block:
            messages.append(f"Unblocked '{username}'.")
        _add_to_section("Streamers", username)
        messages.append(f"Added '{username}' to [Streamers].")
    elif action == "remove":
        removed_from_streamers = _remove_from_section("Streamers", username)
        if removed_from_streamers:
            messages.append(f"Removed '{username}' from [Streamers].")
        else:
            messages.append(f"'{username}' was not found in [Streamers].")
        _add_to_section("Block", username)
        messages.append(f"Added '{username}' to [Block].")

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        return f"ERROR writing config: {e}"

    return "  ".join(messages)


def _streamer_mgmt_thread() -> None:
    """
    Background thread that handles output modes 6 (add streamer) and 7 (remove streamer).
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

        if mode not in (6, 7):
            continue

        if mode == 6:
            title = "Add a Streamer"
            prompt = "Enter streamer username to ADD"
            action = "add"
        else:
            title = "Remove a Streamer"
            prompt = "Enter streamer username to REMOVE"
            action = "remove"

        result_msg = ""

        while True:
            with output_mode_lock:
                current_mode = OUTPUT_MODE
            if current_mode not in (6, 7):
                break

            # Re-draw the page
            header = (
                f"{CLEAR}"
                f"{TITLE_COLOR}{'─' * 52}{RESET}\n"
                f"{TITLE_COLOR}  jj-dlp · {title}{RESET}\n"
                f"{TITLE_COLOR}{'─' * 52}{RESET}\n"
            )
            footer = (
                f"\n{TITLE_COLOR}{'─' * 52}{RESET}\n"
                f"  \033[90mPress Enter to jump back to Dashboard\033[0m\n"
            )
            if result_msg:
                body = f"  {OK_COLOR}{result_msg}{RESET}\n"
            else:
                body = ""

            sys.stdout.write(header + body + f"  {WARN_COLOR}{prompt}:{RESET}" + footer)
            sys.stdout.flush()

            username = _read_line_raw()

            # Check if mode changed while we were waiting for input
            with output_mode_lock:
                current_mode = OUTPUT_MODE
            if current_mode not in (6, 7):
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
                f"{TITLE_COLOR}{'─' * 52}{RESET}\n"
                f"{TITLE_COLOR}  jj-dlp · {title}{RESET}\n"
                f"{TITLE_COLOR}{'─' * 52}{RESET}\n"
            )
            footer = (
                f"\n{TITLE_COLOR}{'─' * 52}{RESET}\n"
            )
            body = f"  {OK_COLOR}{result_msg}{RESET}\n"

            sys.stdout.write(header + body + footer)
            sys.stdout.flush()

            # Wait so user can read it
            time.sleep(1.0)

            # Return to dashboard
            _set_output_mode(1)
            break

def log(msg: str) -> None:
    with verbosity_lock:
        v = VERBOSITY
    with output_mode_lock:
        mode = OUTPUT_MODE
    if mode in (1, 6, 7):
        return  # dashboard / streamer mgmt pages own the screen
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
    if mode in (1, 6, 7):
        return  # dashboard / streamer mgmt pages own the screen
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


def _drain_pipe(pipe, log_fp, show_modes: set,
                ffmpeg_error_counter=None,
                ffmpeg_error_event=None,
                streamer: str = "") -> None:
    """
    Read lines from *pipe* until EOF.
    - Writes each line to *log_fp* if provided.
    - Prints to terminal when OUTPUT_MODE is in *show_modes*.
    - If ffmpeg_error_counter and ffmpeg_error_event are provided, scans each
      line for FFMPEG_ERROR_PATTERNS and increments the shared counter.
      When the counter exceeds FFMPEG_ERROR_RESTART_THRESHOLD, sets the event
      to signal record_stream to kill and restart yt-dlp.

    show_modes: set of OUTPUT_MODE values that should display this stream.
                stdout -> {3, 5}   stderr -> {4, 5}
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
            if mode in show_modes:
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
                        dbg(f"ffmpeg_monitor [{streamer}]: pattern '{pattern}' matched "
                            f"(count={ffmpeg_error_counter[0]}/{FFMPEG_ERROR_RESTART_THRESHOLD})")
                        if ffmpeg_error_counter[0] >= FFMPEG_ERROR_RESTART_THRESHOLD:
                            log(f"ffmpeg_monitor [{streamer}]: error threshold reached "
                                f"({ffmpeg_error_counter[0]} matches) — signalling restart.")
                            ffmpeg_error_event.set()
                        break  # only count once per line even if multiple patterns match
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
                    args=(proc.stdout, log_out_fp, {3, 5}),
                    kwargs={"ffmpeg_error_counter": ffmpeg_error_counter,
                            "ffmpeg_error_event": ffmpeg_error_event,
                            "streamer": streamer},
                    daemon=True,
                ).start()
                threading.Thread(
                    target=_drain_pipe,
                    args=(proc.stderr, log_err_fp, {4, 5}),
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

                # Check if ffmpeg error threshold was crossed by a drain thread
                if ffmpeg_error_event.is_set():
                    log(f"\n\nffmpeg_monitor [{streamer}]: restarting yt-dlp due to ffmpeg errors "
                        f"(threshold={FFMPEG_ERROR_RESTART_THRESHOLD}).\n\n")
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
            # Show popup notification (poll path)
            if cfg.get("popup_notifications", True):
                dbg(f"popup: [poll] triggering popup for {streamer!r}")
                _show_live_popup(streamer, source="poll")
            else:
                dbg(f"popup: [poll] popup disabled via config — skipping for {streamer!r}")
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


# ══════════════════════════════════════════════════════════════════════════════
# Twitch EventSub  —  instant "stream.online" notifications
# ══════════════════════════════════════════════════════════════════════════════
#
# How it works:
#   1. A tiny HTTP server listens on WEBHOOK_PORT (default 8888).
#   2. On startup (and whenever the streamer list changes) we:
#        a. Get a Twitch app-access token via client_credentials OAuth.
#        b. Resolve each streamer's login name → user_id via the Helix API.
#        c. Subscribe to "stream.online" for every user_id via EventSub.
#   3. Twitch sends an HMAC-signed POST to CALLBACK_URL when a streamer goes
#      live.  We verify the signature, then immediately call
#      start_recording_if_needed() — no waiting for the 60-second poll.
#   4. All subscriptions are cleaned up when the process exits.
#
# Requirements:  Python ≥ 3.8 standard library only (no extra packages).
# Optional:      If CALLBACK_URL uses https, you'll need a reverse proxy
#                (e.g. nginx + Let's Encrypt) or ngrok in front of the server.
#
# ──────────────────────────────────────────────────────────────────────────────

import hmac
import hashlib
import urllib.request
import urllib.error

# ── EventSub global state ─────────────────────────────────────────────────────
_twitch_token: str = ""
_twitch_token_lock = threading.Lock()
_eventsub_subscription_ids: dict = {}   # login -> subscription_id
_eventsub_sub_lock = threading.Lock()
_eventsub_cfg: dict = {}                # cfg snapshot used by the HTTP server

# Dashboard-visible status fields (written by EventSub threads, read by renderer)
_eventsub_server_status: str = "not started"   # e.g. "listening on port 8888" / "ERROR: ..."
_eventsub_server_port: int = 0
_eventsub_status_lock = threading.Lock()
_eventsub_last_notification: str = ""          # human-readable last event received
_eventsub_notifications_total: int = 0         # count of verified notifications received
# ─────────────────────────────────────────────────────────────────────────────

_eventsub_stop_event = threading.Event()
_eventsub_manage_lock = threading.Lock()


def _twitch_get_token(client_id: str, client_secret: str) -> str:
    """Fetch a fresh app-access token from Twitch via client_credentials."""
    url = "https://id.twitch.tv/oauth2/token"
    data = (
        f"client_id={client_id}&client_secret={client_secret}"
        "&grant_type=client_credentials"
    ).encode()
    dbg(f"[Twitch] token: POST {url}")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            token = body.get("access_token", "")
            expires_in = body.get("expires_in", "?")
            if token:
                dbg(f"[Twitch] token: obtained OK  (expires_in={expires_in}s, "
                    f"token prefix={token[:8]}...)")
            else:
                dbg(f"[Twitch] token: response had no access_token — full body: {body}")
            return token
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log(f"[Twitch] token fetch failed: HTTP {e.code} — {body}")
        dbg(f"[Twitch] token: HTTPError {e.code} detail: {body}")
        return ""
    except Exception as e:
        log(f"[Twitch] token fetch failed: {e}")
        dbg(f"[Twitch] token: exception: {type(e).__name__}: {e}")
        return ""


def _twitch_api(path: str, client_id: str, token: str, method: str = "GET",
                data: bytes = None, params: dict = None) -> dict:
    """Minimal Twitch Helix API helper. Returns parsed JSON dict or {}."""
    base = "https://api.twitch.tv/helix"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{base}{path}?{qs}"
    else:
        url = f"{base}{path}"
    dbg(f"[Twitch] API {method} {url}  (body_len={len(data) if data else 0})")
    headers = {
        "Client-Id": client_id,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            result = json.loads(raw)
            dbg(f"[Twitch] API {method} {path} → HTTP 200  ({len(raw)} bytes)")
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        dbg(f"[Twitch] API {method} {path} → HTTP {e.code}: {body}")
        return {}
    except Exception as e:
        dbg(f"[Twitch] API {method} {path} → exception: {type(e).__name__}: {e}")
        return {}


def _twitch_resolve_user_ids(logins: list, client_id: str, token: str) -> dict:
    """Return {login_lower: user_id} for all resolved logins."""
    result = {}
    dbg(f"[Twitch] resolve_user_ids: resolving {len(logins)} login(s): {logins}")
    # Helix allows up to 100 logins per request
    for i in range(0, len(logins), 100):
        chunk = logins[i:i + 100]
        params_str = "&".join(f"login={l}" for l in chunk)
        url = f"https://api.twitch.tv/helix/users?{params_str}"
        dbg(f"[Twitch] resolve_user_ids: GET {url}")
        req = urllib.request.Request(url, headers={
            "Client-Id": client_id,
            "Authorization": f"Bearer {token}",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                users_found = data.get("data", [])
                dbg(f"[Twitch] resolve_user_ids: API returned {len(users_found)} user(s)")
                for u in users_found:
                    login_lower = u["login"].lower()
                    uid = u["id"]
                    result[login_lower] = uid
                    dbg(f"[Twitch] resolve_user_ids:   {login_lower!r} → user_id={uid}")
                # Report any logins that came back empty
                found_logins = {u["login"].lower() for u in users_found}
                for missing in chunk:
                    if missing.lower() not in found_logins:
                        dbg(f"[Twitch] resolve_user_ids:   {missing!r} → NOT FOUND "
                            f"(check spelling / account exists?)")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            log(f"[Twitch] user-id resolve failed: HTTP {e.code} — {body}")
            dbg(f"[Twitch] resolve_user_ids: HTTPError {e.code}: {body}")
        except Exception as e:
            log(f"[Twitch] user-id resolve failed: {e}")
            dbg(f"[Twitch] resolve_user_ids: exception: {type(e).__name__}: {e}")
    dbg(f"[Twitch] resolve_user_ids: final result = {result}")
    return result


def _twitch_subscribe(user_id: str, client_id: str, token: str,
                      callback_url: str, webhook_secret: str) -> str:
    """
    Subscribe to stream.online for user_id via EventSub.
    Returns the subscription id string, or '' on failure.
    """
    dbg(f"[Twitch] subscribe: creating stream.online subscription "
        f"for user_id={user_id}  callback={callback_url}")
    payload = json.dumps({
        "type": "stream.online",
        "version": "1",
        "condition": {"broadcaster_user_id": user_id},
        "transport": {
            "method": "webhook",
            "callback": callback_url,
            "secret": webhook_secret,
        },
    }).encode()
    resp = _twitch_api("/eventsub/subscriptions", client_id, token,
                       method="POST", data=payload)
    subs = resp.get("data", [])
    if subs:
        sub = subs[0]
        sub_id     = sub.get("id", "")
        sub_status = sub.get("status", "?")
        dbg(f"[Twitch] subscribe: OK — sub_id={sub_id}  status={sub_status}")
        return sub_id
    # Subscription failed — log whatever Twitch returned
    error_msg = resp.get("message", "")
    error_code = resp.get("error", "")
    dbg(f"[Twitch] subscribe: FAILED for user_id={user_id} — "
        f"error={error_code!r}  message={error_msg!r}  full_resp={resp}")
    return ""


def _twitch_unsubscribe(sub_id: str, client_id: str, token: str) -> None:
    """Delete an EventSub subscription by id."""
    dbg(f"[Twitch] unsubscribe: deleting sub_id={sub_id}")
    _twitch_api(f"/eventsub/subscriptions?id={sub_id}", client_id, token,
                method="DELETE")
    dbg(f"[Twitch] unsubscribe: done sub_id={sub_id}")


def _eventsub_verify_signature(secret: str, msg_id: str, msg_timestamp: str,
                               body: bytes, twitch_sig: str) -> bool:
    """Return True if the HMAC-SHA256 signature from Twitch is valid."""
    hmac_message = (msg_id + msg_timestamp).encode() + body
    expected = "sha256=" + hmac.new(
        secret.encode(), hmac_message, hashlib.sha256
    ).hexdigest()
    match = hmac.compare_digest(expected, twitch_sig)
    if not match:
        dbg(f"[Twitch] sig_verify: MISMATCH  "
            f"expected={expected[:32]}...  got={twitch_sig[:32]}...")
    return match


def _eventsub_handle_request(method: str, path: str, headers: dict,
                              body: bytes, cfg: dict) -> tuple:
    """
    Process one inbound HTTP request from Twitch.
    Returns (status_code: int, response_body: bytes).
    """
    global _eventsub_last_notification, _eventsub_notifications_total

    secret    = cfg.get("twitch_webhook_secret", "")
    msg_type  = headers.get("twitch-eventsub-message-type", "")
    msg_id    = headers.get("twitch-eventsub-message-id", "")
    msg_ts    = headers.get("twitch-eventsub-message-timestamp", "")
    signature = headers.get("twitch-eventsub-message-signature", "")

    dbg(f"[Twitch] http_handler: {method} {path}  "
        f"msg-type={msg_type!r}  msg-id={msg_id!r}  "
        f"body_len={len(body)}  sig_present={bool(signature)}")

    if method != "POST":
        dbg(f"[Twitch] http_handler: rejecting non-POST request ({method})")
        return 405, b"Method Not Allowed"

    if not signature:
        dbg("[Twitch] http_handler: no Twitch signature header present — "
            "this may not be a real Twitch request, or HMAC headers are missing")

    if not _eventsub_verify_signature(secret, msg_id, msg_ts, body, signature):
        log("[Twitch] EventSub: signature verification FAILED — "
            "ignoring request (wrong WEBHOOK_SECRET, or not from Twitch)")
        dbg("[Twitch] http_handler: 403 — signature mismatch")
        return 403, b"Forbidden"

    dbg("[Twitch] http_handler: signature OK")

    try:
        payload = json.loads(body)
    except Exception as e:
        dbg(f"[Twitch] http_handler: JSON parse error: {e}  body={body[:200]!r}")
        return 400, b"Bad Request"

    # ── Twitch challenge (sent once when we subscribe) ────────────────────────
    if msg_type == "webhook_callback_verification":
        challenge = payload.get("challenge", "")
        sub_info  = payload.get("subscription", {})
        sub_type  = sub_info.get("type", "?")
        sub_cond  = sub_info.get("condition", {})
        dbg(f"[Twitch] http_handler: challenge verification request "
            f"type={sub_type}  condition={sub_cond}  challenge={challenge!r}")
        log(f"[Twitch] EventSub: challenge verified for {sub_type} "
            f"(condition={sub_cond}) — subscription is now active")
        with _eventsub_status_lock:
            _eventsub_last_notification = f"challenge OK for {sub_type}"
        return 200, challenge.encode()

    # ── Live notification ─────────────────────────────────────────────────────
    if msg_type == "notification":
        event             = payload.get("event", {})
        broadcaster_login = event.get("broadcaster_user_login", "").lower()
        broadcaster_id    = event.get("broadcaster_user_id", "?")
        stream_type       = event.get("type", "?")   # "live" or "playlist"
        started_at        = event.get("started_at", "")

        dbg(f"[Twitch] http_handler: NOTIFICATION  "
            f"login={broadcaster_login!r}  id={broadcaster_id}  "
            f"stream_type={stream_type!r}  started_at={started_at!r}")

        if broadcaster_login:
            ts_str = datetime.now().strftime("%H:%M:%S")
            with _eventsub_status_lock:
                _eventsub_notifications_total += 1
                _eventsub_last_notification = (
                    f"{broadcaster_login} went live at {ts_str} "
                    f"(#{_eventsub_notifications_total})"
                )

            log(f"[Twitch] EventSub: *** {broadcaster_login} just went live "
                f"(stream_type={stream_type}) — triggering recording immediately ***\n")

            # Update dashboard live_since immediately
            with dashboard_lock:
                if broadcaster_login not in dashboard_live_since:
                    dashboard_live_since[broadcaster_login] = time.time()
                    dbg(f"[Twitch] http_handler: updated dashboard_live_since for {broadcaster_login}")

            # Load fresh config and start recording
            dbg(f"[Twitch] http_handler: loading config to verify {broadcaster_login} is still in [Streamers]")
            current_cfg = load_config(cfg["config_path"])
            in_streamers = broadcaster_login in current_cfg.get("streamers", [])
            is_blocked   = broadcaster_login in current_cfg.get("blocked", [])
            dbg(f"[Twitch] http_handler: {broadcaster_login} "
                f"in_streamers={in_streamers}  is_blocked={is_blocked}")

            if in_streamers and not is_blocked:
                dbg(f"[Twitch] http_handler: calling start_recording_if_needed([{broadcaster_login!r}])")
                # Show popup notification (EventSub path)
                if current_cfg.get("popup_notifications", True):
                    dbg(f"popup: [eventsub] triggering popup for {broadcaster_login!r}")
                    _show_live_popup(broadcaster_login, source="eventsub")
                else:
                    dbg(f"popup: [eventsub] popup disabled via config — skipping for {broadcaster_login!r}")
                start_recording_if_needed([broadcaster_login], current_cfg)
                dbg(f"[Twitch] http_handler: start_recording_if_needed returned")
            else:
                dbg(f"[Twitch] http_handler: skipping {broadcaster_login} "
                    f"(in_streamers={in_streamers}, is_blocked={is_blocked})")
                log(f"[Twitch] EventSub: {broadcaster_login} notified as live but is not in "
                    f"[Streamers] or is blocked — skipping")
        else:
            dbg(f"[Twitch] http_handler: notification had no broadcaster_user_login — "
                f"full event: {event}")

        return 200, b"OK"

    # ── Subscription revocation ───────────────────────────────────────────────
    if msg_type == "revocation":
        sub_info   = payload.get("subscription", {})
        sub_type   = sub_info.get("type", "?")
        sub_status = sub_info.get("status", "?")
        sub_cond   = sub_info.get("condition", {})
        sub_id     = sub_info.get("id", "?")
        dbg(f"[Twitch] http_handler: REVOCATION  sub_id={sub_id}  "
            f"type={sub_type}  status={sub_status}  condition={sub_cond}")
        log(f"[Twitch] EventSub: subscription REVOKED "
            f"(type={sub_type}  status={sub_status}  condition={sub_cond}) "
            f"— will resubscribe within 15 seconds")
        # Remove from our tracking so the manager resubscribes on next cycle
        with _eventsub_sub_lock:
            for login, sid in list(_eventsub_subscription_ids.items()):
                if sid == sub_id:
                    dbg(f"[Twitch] http_handler: removing revoked sub for login={login!r}")
                    del _eventsub_subscription_ids[login]
                    break
        return 200, b"OK"

    # ── Unknown message type ──────────────────────────────────────────────────
    dbg(f"[Twitch] http_handler: unhandled msg_type={msg_type!r} — returning 200 anyway")
    return 200, b"OK"


def _eventsub_http_server(cfg: dict, stop_event: threading.Event) -> None:
    """
    Blocking HTTP server — run in a daemon thread.
    Handles one request at a time (sufficient for EventSub volumes).
    """
    import socket
    global _eventsub_server_status, _eventsub_server_port

    port = cfg.get("twitch_webhook_port", 8888)
    callback_url = cfg.get("twitch_callback_url", "?")
    dbg(f"[Twitch] http_server: starting — binding to 0.0.0.0:{port}  "
        f"callback_url={callback_url!r}")

    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(16)
        srv.settimeout(1.0)
        with _eventsub_status_lock:
            _eventsub_server_status = f"listening on port {port}"
            _eventsub_server_port   = port
        log(f"[Twitch] EventSub webhook server listening on 0.0.0.0:{port}")
        dbg(f"[Twitch] http_server: bind+listen OK on port {port}")
    except Exception as e:
        err = f"ERROR: could not bind port {port}: {e}"
        with _eventsub_status_lock:
            _eventsub_server_status = err
        log(f"[Twitch] EventSub: {err}")
        dbg(f"[Twitch] http_server: fatal bind error: {type(e).__name__}: {e}")
        return

    req_count = 0
    while not stop_event.is_set():
        try:
            conn, addr = srv.accept()
        except OSError:
            # accept() times out every 1 s — normal, just loop
            continue

        req_count += 1
        dbg(f"[Twitch] http_server: accepted connection #{req_count} from {addr}")

        try:
            data = b""
            conn.settimeout(5.0)
            # Read until we have headers + the full body
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data:
                    header_part, _, body_so_far = data.partition(b"\r\n\r\n")
                    cl = 0
                    for hline in header_part.split(b"\r\n"):
                        if hline.lower().startswith(b"content-length:"):
                            try:
                                cl = int(hline.split(b":", 1)[1].strip())
                            except Exception:
                                pass
                    if len(body_so_far) >= cl:
                        dbg(f"[Twitch] http_server: req #{req_count} "
                            f"headers_len={len(header_part)} body_len={len(body_so_far)} "
                            f"content-length={cl}")
                        break

            if not data:
                dbg(f"[Twitch] http_server: req #{req_count} from {addr} — empty data, closing")
                conn.close()
                continue

            # Parse the raw HTTP/1.x request
            header_part, _, body = data.partition(b"\r\n\r\n")
            header_lines = header_part.decode(errors="replace").splitlines()
            request_line = header_lines[0] if header_lines else ""
            parts  = request_line.split(" ")
            method = parts[0] if parts else "GET"
            path   = parts[1] if len(parts) > 1 else "/"

            headers = {}
            for hl in header_lines[1:]:
                if ":" in hl:
                    k, _, v = hl.partition(":")
                    headers[k.strip().lower()] = v.strip()

            dbg(f"[Twitch] http_server: req #{req_count} parsed — "
                f"{method} {path}  headers_count={len(headers)}")

            # Use the live cfg snapshot
            with _twitch_token_lock:
                active_cfg = dict(_eventsub_cfg)

            status, resp_body = _eventsub_handle_request(
                method, path, headers, body, active_cfg
            )

            response = (
                f"HTTP/1.1 {status} OK\r\n"
                f"Content-Length: {len(resp_body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode() + resp_body
            conn.sendall(response)
            dbg(f"[Twitch] http_server: req #{req_count} → responded {status}")

        except Exception as e:
            dbg(f"[Twitch] http_server: req #{req_count} handler exception: "
                f"{type(e).__name__}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    srv.close()
    with _eventsub_status_lock:
        _eventsub_server_status = "stopped"
    log("[Twitch] EventSub webhook server stopped")
    dbg("[Twitch] http_server: thread exiting")


def _eventsub_sync_subscriptions(cfg: dict) -> None:
    """
    Ensure we have active EventSub subscriptions for all configured streamers
    and that subscriptions for removed streamers are deleted.
    """
    global _twitch_token, _eventsub_cfg

    client_id      = cfg["twitch_client_id"]
    client_secret  = cfg["twitch_client_secret"]
    webhook_secret = cfg["twitch_webhook_secret"]
    callback_url   = cfg["twitch_callback_url"]
    streamers      = [s for s in cfg["streamers"] if s not in cfg["blocked"]]

    dbg(f"[Twitch] sync_subscriptions: entry  streamers={streamers}  "
        f"callback_url={callback_url!r}")

    # ── Get / refresh access token ────────────────────────────────────────────
    with _twitch_token_lock:
        existing_token = _twitch_token
    if not existing_token:
        dbg("[Twitch] sync_subscriptions: no token cached — fetching new one")
        new_token = _twitch_get_token(client_id, client_secret)
        with _twitch_token_lock:
            _twitch_token = new_token
        token = new_token
    else:
        dbg(f"[Twitch] sync_subscriptions: using cached token (prefix={existing_token[:8]}...)")
        token = existing_token

    if not token:
        log("[Twitch] EventSub: could not obtain access token — "
            "skipping subscription sync this cycle (will retry in 15s)")
        dbg("[Twitch] sync_subscriptions: aborting — no token")
        return

    # ── Update cfg snapshot for the HTTP server ───────────────────────────────
    with _twitch_token_lock:
        _eventsub_cfg.update(cfg)
    dbg("[Twitch] sync_subscriptions: cfg snapshot updated")

    # ── Diff: what needs to change? ───────────────────────────────────────────
    with _eventsub_sub_lock:
        already_subscribed = set(_eventsub_subscription_ids.keys())

    to_subscribe   = [s for s in streamers if s not in already_subscribed]
    to_unsubscribe = [s for s in already_subscribed if s not in streamers]

    dbg(f"[Twitch] sync_subscriptions: already_subscribed={sorted(already_subscribed)}  "
        f"to_subscribe={to_subscribe}  to_unsubscribe={to_unsubscribe}")

    # ── Remove stale subscriptions ────────────────────────────────────────────
    for login in to_unsubscribe:
        with _eventsub_sub_lock:
            sub_id = _eventsub_subscription_ids.pop(login, None)
        if sub_id:
            dbg(f"[Twitch] sync_subscriptions: unsubscribing {login!r}  sub_id={sub_id}")
            _twitch_unsubscribe(sub_id, client_id, token)
            log(f"[Twitch] EventSub: unsubscribed {login} (removed from [Streamers])")
        else:
            dbg(f"[Twitch] sync_subscriptions: {login!r} had no sub_id stored — nothing to delete")

    # ── Add new subscriptions ─────────────────────────────────────────────────
    if not to_subscribe:
        dbg("[Twitch] sync_subscriptions: nothing new to subscribe — done")
        return

    dbg(f"[Twitch] sync_subscriptions: resolving user IDs for {to_subscribe}")
    login_to_id = _twitch_resolve_user_ids(to_subscribe, client_id, token)

    for login in to_subscribe:
        user_id = login_to_id.get(login)
        if not user_id:
            log(f"[Twitch] EventSub: could not resolve user_id for '{login}' "
                "— check spelling / the account exists on Twitch")
            dbg(f"[Twitch] sync_subscriptions: skipping {login!r} — no user_id resolved")
            continue

        dbg(f"[Twitch] sync_subscriptions: subscribing {login!r} (user_id={user_id})")
        sub_id = _twitch_subscribe(user_id, client_id, token, callback_url, webhook_secret)
        if sub_id:
            with _eventsub_sub_lock:
                _eventsub_subscription_ids[login] = sub_id
            log(f"[Twitch] EventSub: subscribed to stream.online for {login} "
                f"(sub_id={sub_id})")
            dbg(f"[Twitch] sync_subscriptions: {login!r} subscribed OK  sub_id={sub_id}")
        else:
            log(f"[Twitch] EventSub: subscription FAILED for {login} "
                f"(check CALLBACK_URL is reachable from the internet)")
            dbg(f"[Twitch] sync_subscriptions: {login!r} subscription returned empty id")

    with _eventsub_sub_lock:
        final_subs = dict(_eventsub_subscription_ids)
    dbg(f"[Twitch] sync_subscriptions: done — active subscriptions: {final_subs}")


def _eventsub_manager_thread(config_path: str, stop_event: threading.Event) -> None:
    """
    Long-running thread that keeps EventSub subscriptions in sync with the
    current streamer list.  Re-syncs on streamer changes and once per hour
    to rotate the access token.
    """
    global _twitch_token

    RESYNC_INTERVAL = 3600   # seconds — token refresh + subscription sanity check
    last_streamers: set = set()
    last_sync_time: float = 0.0
    loop_count = 0

    dbg("[Twitch] manager_thread: started")
    log("[Twitch] EventSub manager started — will sync subscriptions now")

    while not stop_event.is_set():
        loop_count += 1
        dbg(f"[Twitch] manager_thread: loop #{loop_count}")
        try:
            cfg = load_config(config_path)
            if not cfg.get("twitch_enabled"):
                dbg("[Twitch] manager_thread: twitch_enabled=False in config — sleeping 30s")
                stop_event.wait(timeout=30)
                continue

            current_streamers = set(
                s for s in cfg["streamers"] if s not in cfg["blocked"]
            )
            now = time.time()
            time_since_sync = now - last_sync_time
            streamers_changed = current_streamers != last_streamers
            token_stale = time_since_sync >= RESYNC_INTERVAL

            dbg(f"[Twitch] manager_thread: current_streamers={sorted(current_streamers)}  "
                f"last_streamers={sorted(last_streamers)}  "
                f"streamers_changed={streamers_changed}  "
                f"time_since_sync={time_since_sync:.0f}s  token_stale={token_stale}")

            if streamers_changed or token_stale or last_sync_time == 0.0:
                reason = []
                if last_sync_time == 0.0:
                    reason.append("first run")
                if streamers_changed:
                    reason.append(f"streamers changed ({sorted(last_streamers)} → {sorted(current_streamers)})")
                if token_stale:
                    reason.append(f"token refresh due ({time_since_sync:.0f}s elapsed)")
                    # Force token refresh on next _eventsub_sync_subscriptions call
                    with _twitch_token_lock:
                        _twitch_token = ""
                    dbg("[Twitch] manager_thread: cleared cached token for refresh")

                dbg(f"[Twitch] manager_thread: syncing — reason: {'; '.join(reason)}")
                log(f"[Twitch] EventSub: syncing subscriptions ({', '.join(reason)})")
                _eventsub_sync_subscriptions(cfg)
                last_streamers = current_streamers
                last_sync_time = now
                dbg(f"[Twitch] manager_thread: sync complete")
            else:
                dbg(f"[Twitch] manager_thread: no sync needed — "
                    f"next token refresh in {RESYNC_INTERVAL - time_since_sync:.0f}s")

        except Exception as e:
            import traceback
            log(f"[Twitch] EventSub manager error: {e}")
            dbg(f"[Twitch] manager_thread: exception in loop #{loop_count}: "
                f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

        dbg(f"[Twitch] manager_thread: sleeping 15s before next check")
        stop_event.wait(timeout=15)

    # ── Shutdown: clean up all subscriptions ──────────────────────────────────
    dbg("[Twitch] manager_thread: stop_event set — cleaning up subscriptions")
    log("[Twitch] EventSub manager: cleaning up subscriptions...")
    try:
        cfg = load_config(config_path)
        client_id = cfg["twitch_client_id"]
        with _twitch_token_lock:
            token = _twitch_token
        with _eventsub_sub_lock:
            subs = dict(_eventsub_subscription_ids)
        dbg(f"[Twitch] manager_thread: deleting {len(subs)} subscription(s): {list(subs.keys())}")
        for login, sub_id in subs.items():
            try:
                dbg(f"[Twitch] manager_thread: deleting sub for {login!r}  sub_id={sub_id}")
                _twitch_unsubscribe(sub_id, client_id, token)
            except Exception as e:
                dbg(f"[Twitch] manager_thread: error deleting {login!r}: {e}")
    except Exception as e:
        dbg(f"[Twitch] manager_thread: error during cleanup: {e}")
    log("[Twitch] EventSub manager: done")
    dbg("[Twitch] manager_thread: exiting")


# ══════════════════════════════════════════════════════════════════════════════


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

    global VERBOSITY, DEBUG_LOGS_ENABLED, DEBUG_LOG_PATH, dashboard_next_check_in, _config_path
    _config_path = config_path
    with verbosity_lock:
        VERBOSITY = initial_cfg.get("verbosity", 1)

    with debug_log_lock:
        DEBUG_LOGS_ENABLED = initial_cfg.get("debug_logs", False)
        DEBUG_LOG_PATH     = get_debug_log_path(initial_cfg) if DEBUG_LOGS_ENABLED else ""

    kb_v = KEYBIND_LABELS.get(KEYBIND_VERBOSITY, KEYBIND_VERBOSITY)
    log(f"Verbosity: {VERBOSITY} — {VERBOSITY_NAMES[VERBOSITY]}  (press {kb_v} to cycle)")
    log(f"Config file: {config_path}")
    log(f"Output directory: {initial_cfg['output_dir']}")

    if initial_cfg["logging_enabled"]:
        log(f"Log file: {get_log_path(initial_cfg)}")

    if initial_cfg.get("debug_logs"):
        log(f"Debug log: {get_debug_log_path(initial_cfg)}")
                                                                      

    log(f"Check interval: {initial_cfg['check_interval']}s")
    kb_o = KEYBIND_LABELS.get(KEYBIND_OUTPUT, KEYBIND_OUTPUT)
    log(f"Output mode: {OUTPUT_MODE} — {OUTPUT_MODE_NAMES[OUTPUT_MODE]}  (press {kb_o} to cycle through modes 1–7)\n")

    keyboard_thread = threading.Thread(target=_keyboard_listener, daemon=True)
    keyboard_thread.start()

    dashboard_thread = threading.Thread(target=_dashboard_renderer_thread, daemon=True)
    dashboard_thread.start()

    streamer_mgmt_thread = threading.Thread(target=_streamer_mgmt_thread, daemon=True)
    streamer_mgmt_thread.start()

    watcher_interval = initial_cfg.get("config_check_interval", 3)
    watcher_thread = threading.Thread(
        target=config_watcher,
        args=(config_path, watcher_interval),
        daemon=True
    )
    watcher_thread.start()
    dbg(f"main: config_watcher thread launched (poll every {watcher_interval}s)")

    # ── Twitch EventSub (optional) ────────────────────────────────────────────
    if initial_cfg.get("twitch_enabled"):
        log("[Twitch] EventSub: credentials found — starting webhook listener and subscription manager")

        # Store initial cfg snapshot for the HTTP server
        with _twitch_token_lock:
            _eventsub_cfg.update(initial_cfg)

        eventsub_http_thread = threading.Thread(
            target=_eventsub_http_server,
            args=(initial_cfg, _eventsub_stop_event),
            daemon=True,
        )
        eventsub_http_thread.start()

        eventsub_mgr_thread = threading.Thread(
            target=_eventsub_manager_thread,
            args=(config_path, _eventsub_stop_event),
            daemon=True,
        )
        eventsub_mgr_thread.start()
    else:
        dbg("main: Twitch EventSub not configured — polling only")
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
        # Signal the dashboard renderer to stop immediately (no waiting for its sleep to expire)
        _dashboard_stop_event.set()
        _eventsub_stop_event.set()   # also stop EventSub threads if running

        TITLE_COLOR = "\033[96m"
        WARN_COLOR  = "\033[93m"
        OK_COLOR    = "\033[92m"
        RESET       = "\033[0m"
        CLEAR       = "\033[2J\033[H"

        active_recordings = [t for t in recording_threads if t.is_alive()]

        sys.stdout.write(CLEAR)
        sys.stdout.write(f"{TITLE_COLOR}{'─' * 52}{RESET}\n")
        sys.stdout.write(f"{TITLE_COLOR}  jj-dlp  ·  Shutting down...{RESET}\n")
        sys.stdout.write(f"{TITLE_COLOR}{'─' * 52}{RESET}\n\n")

        if active_recordings:
            sys.stdout.write(
                f"  {WARN_COLOR}Waiting for {len(active_recordings)} active recording(s) to finish...{RESET}\n\n"
            )
        sys.stdout.flush()

        for t in recording_threads:
            if t.is_alive():
                t.join(timeout=15)  # Wait 15 seconds for each thread to finish gracefully

        sys.stdout.write(f"  {OK_COLOR}✓  All done. Goodbye!{RESET}\n\n")
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