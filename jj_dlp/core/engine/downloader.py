"""yt-dlp command construction and process launch."""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Deque, List, Optional

from jj_dlp.core.config import schema
from jj_dlp.core.deps.ytdlp_bin import resolve_yt_dlp_path

log = logging.getLogger("jj_dlp.engine.downloader")

_RING_BUFFER_SIZE = 500

# (stream_name, line) -> None
LineCallback = Callable[[str, str], None]


class _LineRingBuffer:
    """Minimal stand-in ring buffer for one stdout/stderr stream.

    Placeholder until notify/pipe_capture.py's real ring buffers are wired in.
    """

    def __init__(self, maxlen: int = _RING_BUFFER_SIZE) -> None:
        self._lines: Deque[str] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        """Add one line to the buffer."""
        with self._lock:
            self._lines.append(line)

    def get_lines(self) -> List[str]:
        """Return a snapshot list of currently buffered lines."""
        with self._lock:
            return list(self._lines)


class DownloaderProcess:
    """Wraps a launched yt-dlp process plus its captured stdout/stderr lines."""

    def __init__(self, popen: "subprocess.Popen[str]", cmd: List[str]) -> None:
        self.popen = popen
        self.cmd = cmd
        self.stdout_buffer = _LineRingBuffer()
        self.stderr_buffer = _LineRingBuffer()

    @property
    def pid(self) -> int:
        """Return the OS process id of the launched yt-dlp process."""
        return self.popen.pid

    def poll(self) -> Optional[int]:
        """Return the process's exit code, or None if still running."""
        return self.popen.poll()

    def wait(self, timeout: Optional[float] = None) -> int:
        """Block until the process exits (or timeout elapses) and return its exit code."""
        return self.popen.wait(timeout=timeout)

    def terminate(self) -> None:
        """Request a graceful stop of the process."""
        self.popen.terminate()

    def kill(self) -> None:
        """Force-kill the process and its process group (PyInstaller yt-dlp spawns a bootloader + worker)."""
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.popen.pid)], capture_output=True)
            return
        try:
            os.killpg(os.getpgid(self.popen.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            self.popen.kill()


def _block_name(lq: bool) -> str:
    """Return the active site-config block name for normal or LQ quality mode."""
    return "lq_downloader" if lq else "downloader"


def _flag_args(site_config: dict, block: str, schema_path: Optional[Path]) -> List[str]:
    """Build CLI flag args for one config block from its schema yt_dlp_flag mappings."""
    args: List[str] = []
    block_data = site_config.get(block, {})
    prefix = f"{block}."
    for field in schema.get_site_fields(schema_path):
        path = field["path"]
        if not path.startswith(prefix):
            continue
        flag = field.get("yt_dlp_flag")
        if not flag:
            continue
        key = path[len(prefix):]
        if key not in block_data:
            continue
        value = block_data[key]
        if key == "cookies_from_browser":
            if value:
                browser = site_config.get("browser", "")
                if browser:
                    args += [flag, browser]
            continue
        if field.get("type") == "bool":
            if value:
                args.append(flag)
        else:
            args += [flag, str(value)]
    return args


def build_command(
    site_config: dict,
    streamer: str,
    output_path: str,
    lq: bool = False,
    schema_path: Optional[Path] = None,
) -> List[str]:
    """Build the full yt-dlp CLI argument list for one streamer's recording."""
    block = _block_name(lq)
    block_data = site_config.get(block, {})
    url = site_config["url_template"].format(username=streamer)

    cmd = [str(resolve_yt_dlp_path())]
    cmd += _flag_args(site_config, block, schema_path)
    cmd += ["-o", output_path]

    downloader_args = block_data.get("downloader_args", "")
    if downloader_args:
        cmd += ["--downloader-args", downloader_args]

    extra_args = block_data.get("extra_args", "")
    if extra_args:
        cmd += shlex.split(extra_args)

    cmd.append(url)
    return cmd


def _pump(stream, buffer: _LineRingBuffer, stream_name: str, on_line: Optional[LineCallback]) -> None:
    """Read lines from a process stream into its ring buffer until the stream closes."""
    for raw_line in iter(stream.readline, ""):
        line = raw_line.rstrip("\n")
        buffer.append(line)
        if on_line is not None:
            try:
                on_line(stream_name, line)
            except Exception:
                log.exception("on_line callback raised for stream %s", stream_name)
    stream.close()


def launch(cmd: List[str], on_line: Optional[LineCallback] = None) -> DownloaderProcess:
    """Launch a yt-dlp command with piped stdout/stderr and start line-capture threads."""
    popen = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=sys.platform != "win32",
    )
    handle = DownloaderProcess(popen, cmd)
    threading.Thread(
        target=_pump, args=(popen.stdout, handle.stdout_buffer, "stdout", on_line), daemon=True
    ).start()
    threading.Thread(
        target=_pump, args=(popen.stderr, handle.stderr_buffer, "stderr", on_line), daemon=True
    ).start()
    return handle


def start_recording(
    site_config: dict,
    streamer: str,
    output_path: str,
    lq: bool = False,
    schema_path: Optional[Path] = None,
    on_line: Optional[LineCallback] = None,
) -> DownloaderProcess:
    """Build the yt-dlp command for one streamer and launch it, returning the process handle."""
    cmd = build_command(site_config, streamer, output_path, lq=lq, schema_path=schema_path)
    log.info("Starting recording for %s: %s", streamer, " ".join(shlex.quote(part) for part in cmd))
    return launch(cmd, on_line=on_line)
