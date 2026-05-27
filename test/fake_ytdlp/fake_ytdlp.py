#!/usr/bin/env python3
"""
fake_ytdlp.py  —  Drop-in yt-dlp replacement for testing jj-dlp

Ignores all real yt-dlp flags.  Reads fake_ytdlp.conf from the same
directory as this script and runs the scenario(s) enabled there.

Usage: point YT_DLP_PATH_LINUX / _WINDOWS / _MAC in your jj-dlp .conf at
       the absolute path to this script (or a wrapper shell/batch file).

Exit codes:
    0  normal (stream ended)
    1  simulated error exit
"""

import sys
import os
import time
import json
import random
import configparser
import threading
import traceback

# ── Locate our own config and log file ────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG_PATH = os.environ.get("FAKE_YTDLP_CONF", "")
if not _CFG_PATH or not os.path.isfile(_CFG_PATH):
    _CFG_PATH = os.path.join(_HERE, "fake_ytdlp.conf")

# ENV_OVERRIDE_PATCH
_env_conf = os.environ.get("FAKE_YTDLP_CONF", "")
if _env_conf and os.path.isfile(_env_conf):
    _CFG_PATH = _env_conf


# Default log file path
_LOG_PATH = os.path.join(_HERE, "fake_ytdlp.log")


def _log_to_file(msg: str) -> None:
    """Appends a timestamped message to the default log file."""
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except Exception:
        # Fallback to stderr if writing to the log file fails
        print(f"--- LOGGING ERROR: Could not write to {_LOG_PATH} ---", file=sys.stderr)


def _load_cfg() -> dict:
    """Load fake_ytdlp.conf into a plain dict with safe defaults."""
    parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
    parser.read(_CFG_PATH, encoding="utf-8")

    def _bool(section, key, default=False):
        raw = parser.get(section, key, fallback=str(default)).strip().lower()
        return raw in ("1", "true", "yes")

    def _int(section, key, default=0):
        try:
            return int(parser.get(section, key, fallback=str(default)).strip())
        except ValueError:
            return default

    def _float(section, key, default=0.0):
        try:
            return float(parser.get(section, key, fallback=str(default)).strip())
        except ValueError:
            return default

    def _str(section, key, default=""):
        return parser.get(section, key, fallback=default).strip().strip('"\'')

    # ── [Mode] ────────────────────────────────────────────────────────────────
    mode = _str("Mode", "mode", "checker")          # "checker" | "downloader"

    # ── [Checker] ─────────────────────────────────────────────────────────────
    checker_streamers_live = [
        s.strip().lower()
        for s in _str("Checker", "streamers_live", "").split(",")
        if s.strip()
    ]
    checker_delay = _float("Checker", "delay_seconds", 0.3)

    # ── [Downloader] ──────────────────────────────────────────────────────────
    dl_duration        = _int  ("Downloader", "stream_duration_seconds", 30)
    dl_write_interval  = _float("Downloader", "write_interval_seconds",  1.0)
    dl_chunk_bytes     = _int  ("Downloader", "chunk_bytes",              8192)
    dl_exit_code       = _int  ("Downloader", "exit_code",                0)

    # ── [Stall] ───────────────────────────────────────────────────────────────
    stall_enabled      = _bool ("Stall", "enabled",                False)
    stall_after        = _int  ("Stall", "stall_after_seconds",    10)

    # ── [FfmpegErrors] ────────────────────────────────────────────────────────
    ffmpeg_ts_disc_enabled  = _bool ("FfmpegErrors", "timestamp_discontinuity",        False)
    ffmpeg_ts_disc_interval = _float("FfmpegErrors", "timestamp_discontinuity_interval_seconds", 0.5)
    ffmpeg_ts_disc_count    = _int  ("FfmpegErrors", "timestamp_discontinuity_count",  600)

    ffmpeg_pkt_corrupt_enabled  = _bool ("FfmpegErrors", "packet_corrupt",                     False)
    ffmpeg_pkt_corrupt_interval = _float("FfmpegErrors", "packet_corrupt_interval_seconds",     0.5)
    ffmpeg_pkt_corrupt_count    = _int  ("FfmpegErrors", "packet_corrupt_count",                600)

    # ── [Progress] ────────────────────────────────────────────────────────────
    progress_enabled   = _bool ("Progress", "enabled",                True)
    progress_interval  = _float("Progress", "interval_seconds",       2.0)

    # ── [SlowStart] ───────────────────────────────────────────────────────────
    slow_start_enabled = _bool ("SlowStart", "enabled",               False)
    slow_start_delay   = _float("SlowStart", "delay_seconds",         8.0)

    # ── [CrashTest] ───────────────────────────────────────────────────────────
    crash_enabled      = _bool ("CrashTest", "enabled",               False)
    crash_after        = _int  ("CrashTest", "crash_after_seconds",   15)
    crash_exit_code    = _int  ("CrashTest", "exit_code",             1)

    return dict(
        mode                        = mode,
        checker_streamers_live      = checker_streamers_live,
        checker_delay               = checker_delay,
        dl_duration                 = dl_duration,
        dl_write_interval           = dl_write_interval,
        dl_chunk_bytes              = dl_chunk_bytes,
        dl_exit_code                = dl_exit_code,
        stall_enabled               = stall_enabled,
        stall_after                 = stall_after,
        ffmpeg_ts_disc_enabled      = ffmpeg_ts_disc_enabled,
        ffmpeg_ts_disc_interval     = ffmpeg_ts_disc_interval,
        ffmpeg_ts_disc_count        = ffmpeg_ts_disc_count,
        ffmpeg_pkt_corrupt_enabled  = ffmpeg_pkt_corrupt_enabled,
        ffmpeg_pkt_corrupt_interval = ffmpeg_pkt_corrupt_interval,
        ffmpeg_pkt_corrupt_count    = ffmpeg_pkt_corrupt_count,
        progress_enabled            = progress_enabled,
        progress_interval           = progress_interval,
        slow_start_enabled          = slow_start_enabled,
        slow_start_delay            = slow_start_delay,
        crash_enabled               = crash_enabled,
        crash_after                 = crash_after,
        crash_exit_code             = crash_exit_code,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _extract_output_path(argv: list) -> str:
    """Pull the value after -o / --output from the command line."""
    for i, arg in enumerate(argv):
        if arg in ("-o", "--output") and i + 1 < len(argv):
            return argv[i + 1]
    return ""


def _extract_urls(argv: list) -> list:
    """Return bare URL-like arguments (not flags, not flag values)."""
    urls = []
    skip_next = False
    flag_takes_value = {
        "-o", "--output", "-f", "--format", "--cookies", "--cookies-from-browser",
        "-P", "--paths", "--config-location", "-N", "--concurrent-fragments",
        "--merge-output-format", "--remux-video", "--recode-video",
        "--postprocessor-args", "--downloader", "--external-downloader",
    }
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg in flag_takes_value:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        urls.append(arg)
    return urls


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _stdout(msg: str) -> None:
    print(msg, flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# Checker mode
# ══════════════════════════════════════════════════════════════════════════════

def _run_checker(cfg: dict, argv: list) -> int:
    """
    Emit one JSON object per URL on stdout.
    Streamers whose name appears in cfg["checker_streamers_live"] get
    is_live=True; everyone else gets is_live=False.
    """
    time.sleep(cfg["checker_delay"])

    urls = _extract_urls(argv)
    if not urls:
        # Nothing to check — exit clean
        _log_to_file("[Checker] No URLs found to check.")
        return 0

    live_set = set(cfg["checker_streamers_live"])

    for url in urls:
        # Derive a streamer name from the URL (last path component, strip @)
        streamer = url.rstrip("/").split("/")[-1].lstrip("@").lower().strip()
        is_live = streamer in live_set

        info = {
            "id":          streamer,
            "title":       f"Fake stream — {streamer}",
            "description": "Generated by fake_ytdlp for testing",
            "webpage_url": url,
            "url":         url,
            "is_live":     is_live,
            "live_status": "is_live" if is_live else "not_live",
            "uploader":    streamer,
        }
        _stdout(json.dumps(info))
        _log_to_file(f"[Checker] Evaluated URL: {url} -> is_live={is_live}")

    return 0


# ══════════════════════════════════════════════════════════════════════════════
# Downloader mode
# ══════════════════════════════════════════════════════════════════════════════

def _ffmpeg_error_thread(enabled: bool, pattern: str, interval: float,
                         count: int, stop_evt: threading.Event) -> None:
    """Background thread: emit <count> ffmpeg-style error lines to stderr."""
    if not enabled:
        return
    emitted = 0
    while emitted < count and not stop_evt.is_set():
        stop_evt.wait(interval)
        if stop_evt.is_set():
            break
        # Realistic-looking ffmpeg stderr line
        pts = round(random.uniform(0, 9999999), 0)
        _stderr(
            f"[segment @ 0x{random.randint(0,0xffffffffffff):012x}] {pattern}: "
            f"dts {pts}, next:{pts + random.randint(1,500)}"
        )
        emitted += 1


def _progress_thread(enabled: bool, interval: float, stop_evt: threading.Event,
                     start_time: float) -> None:
    """Background thread: emit yt-dlp-style progress lines to stderr."""
    if not enabled:
        return
    speed_kbps = random.uniform(800, 4000)
    while not stop_evt.is_set():
        stop_evt.wait(interval)
        if stop_evt.is_set():
            break
        elapsed = time.time() - start_time
        downloaded_mib = speed_kbps * elapsed / 8 / 1024
        # Mimic yt-dlp's live progress line
        _stderr(
            f"[download] {downloaded_mib:.1f}MiB  "
            f"{speed_kbps:.0f}KiB/s  "
            f"ETA Unknown  "
            f"(frag {int(elapsed)})"
        )


def _run_downloader(cfg: dict, argv: list) -> int:
    """
    Simulate a live recording:
      1. Optionally slow-start (delay before creating the file).
      2. Write chunks to the output file until duration elapses.
      3. Optionally stall (stop writing) after stall_after seconds.
      4. Optionally emit ffmpeg error lines to stderr.
      5. Optionally crash early.
    """
    output_tmpl = _extract_output_path(argv)
    if not output_tmpl:
        _stderr("fake_ytdlp: no -o path found — writing to fake_output.ts in cwd")
        output_tmpl = os.path.join(os.getcwd(), "fake_output.ts")

    # Resolve %(title)s etc. with a fixed fake value so the file is creatable
    # immediately without a real info-dict expansion.
    output_path = output_tmpl
    for placeholder in ("%(title)s", "%(id)s", "%(ext)s", "%(uploader)s",
                         "%(upload_date)s", "%(timestamp)s"):
        key = placeholder.strip("%()")
        replacements = {
            "title":       "FakeStream",
            "id":          "fakeid123",
            "ext":         "ts",
            "uploader":    "fakeuser",
            "upload_date": "20250101",
            "timestamp":   str(int(time.time())),
        }
        output_path = output_path.replace(placeholder, replacements.get(key, "fake"))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # ── Slow start ────────────────────────────────────────────────────────────
    if cfg["slow_start_enabled"]:
        _stderr(f"fake_ytdlp: [SlowStart] waiting {cfg['slow_start_delay']}s before creating file…")
        _log_to_file(f"[Downloader] Slow-start enabled. Sleeping {cfg['slow_start_delay']}s")
        time.sleep(cfg["slow_start_delay"])

    start_time = time.time()
    stop_evt   = threading.Event()

    # ── Background threads ────────────────────────────────────────────────────
    ffmpeg_ts_t = threading.Thread(
        target=_ffmpeg_error_thread,
        args=(cfg["ffmpeg_ts_disc_enabled"], "timestamp discontinuity",
              cfg["ffmpeg_ts_disc_interval"], cfg["ffmpeg_ts_disc_count"], stop_evt),
        daemon=True,
    )
    ffmpeg_ts_t.start()

    ffmpeg_pk_t = threading.Thread(
        target=_ffmpeg_error_thread,
        args=(cfg["ffmpeg_pkt_corrupt_enabled"], "Packet corrupt",
              cfg["ffmpeg_pkt_corrupt_interval"], cfg["ffmpeg_pkt_corrupt_count"], stop_evt),
        daemon=True,
    )
    ffmpeg_pk_t.start()

    progress_t = threading.Thread(
        target=_progress_thread,
        args=(cfg["progress_enabled"], cfg["progress_interval"], stop_evt, start_time),
        daemon=True,
    )
    progress_t.start()

    _stderr(f"fake_ytdlp: opening output → {output_path}")
    _log_to_file(f"[Downloader] Target output file: {output_path}")

    exit_code = cfg["dl_exit_code"]

    try:
        with open(output_path, "wb") as fout:
            chunk     = bytes(random.getrandbits(8) for _ in range(cfg["dl_chunk_bytes"]))
            elapsed   = 0.0
            writing   = True
            stall_logged = False

            while True:
                now = time.time()
                elapsed = now - start_time

                # ── Crash test ──────────────────────────────────────────────
                if cfg["crash_enabled"] and elapsed >= cfg["crash_after"]:
                    _stderr(f"fake_ytdlp: [CrashTest] crashing after {elapsed:.1f}s")
                    _log_to_file(f"[Downloader] Crash test triggered after {elapsed:.1f}s with code {cfg['crash_exit_code']}.")
                    stop_evt.set()
                    return cfg["crash_exit_code"]

                # ── Duration expired ─────────────────────────────────────────
                if elapsed >= cfg["dl_duration"]:
                    _stderr(f"fake_ytdlp: stream ended normally after {elapsed:.1f}s")
                    _log_to_file(f"[Downloader] Stream finished naturally after {elapsed:.1f}s.")
                    break

                # ── Stall trigger ─────────────────────────────────────────────
                if cfg["stall_enabled"] and elapsed >= cfg["stall_after"]:
                    if not stall_logged:
                        _stderr(f"fake_ytdlp: [Stall] stalling after {elapsed:.1f}s — no more writes")
                        _log_to_file(f"[Downloader] Stall simulation active. Halting file writes at {elapsed:.1f}s.")
                        stall_logged = True
                    writing = False

                if writing:
                    # Re-randomise the chunk so the file keeps changing
                    chunk = bytes(random.getrandbits(8) for _ in range(cfg["dl_chunk_bytes"]))
                    fout.write(chunk)
                    fout.flush()

                time.sleep(cfg["dl_write_interval"])

    except KeyboardInterrupt:
        _stderr("fake_ytdlp: interrupted")
        _log_to_file("[Downloader] Interrupted via KeyboardInterrupt.")
        exit_code = 1
    finally:
        stop_evt.set()
        ffmpeg_ts_t.join(timeout=2)
        ffmpeg_pk_t.join(timeout=2)
        progress_t.join(timeout=2)

    return exit_code


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    argv = sys.argv[1:]
    _log_to_file(f"--- SCRIPT CALLED --- Arguments received: {argv}")

    try:
        cfg  = _load_cfg()
        mode = cfg["mode"].lower()

        if "--dump-json" in argv:
            mode = "checker"
        elif any(arg in argv for arg in ("-o", "--output")):
            mode = "downloader"

        _log_to_file(f"Selected Mode: {mode}")

        if mode == "checker":
            code = _run_checker(cfg, argv)
        elif mode == "downloader":
            code = _run_downloader(cfg, argv)
        else:
            _stderr(f"fake_ytdlp: unknown mode {mode!r} in fake_ytdlp.conf — doing nothing")
            _log_to_file(f"Unknown mode specified: '{mode}'. Script doing nothing.")
            code = 0

        _log_to_file(f"Script exiting normally with exit code: {code}")
        sys.exit(code)

    except Exception as e:
        # Capture unexpected errors or crashes inside main
        error_msg = f"CRITICAL CRASH: Unexpected exception occurred!\n{traceback.format_exc()}"
        _log_to_file(error_msg)
        _stderr(f"fake_ytdlp internal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()