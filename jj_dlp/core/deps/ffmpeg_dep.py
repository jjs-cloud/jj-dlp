"""Dependency check/install for ffmpeg."""

import os
import shutil
import subprocess
import sys

from jj_dlp.core.deps.registry import Dependency, ProgressCallback, register

# Common per-OS install locations checked when ffmpeg isn't on PATH.
_FALLBACK_PATHS = {
    "win32": [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ],
    "darwin": [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ],
    "linux": [
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/snap/bin/ffmpeg",
    ],
}

# Linux package managers to auto-detect, in preference order.
_LINUX_PKG_MANAGERS = [
    ("apt-get", ["apt-get", "install", "-y", "ffmpeg"]),
    ("dnf", ["dnf", "install", "-y", "ffmpeg"]),
    ("yum", ["yum", "install", "-y", "ffmpeg"]),
    ("pacman", ["pacman", "-S", "--noconfirm", "ffmpeg"]),
    ("zypper", ["zypper", "install", "-y", "ffmpeg"]),
    ("apk", ["apk", "add", "ffmpeg"]),
]


def _run_streaming(cmd, progress_cb: ProgressCallback = None):
    """Run cmd, streaming stdout/stderr lines to progress_cb, return (ok, message)."""
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        for line in process.stdout:
            if progress_cb:
                progress_cb(line.rstrip("\n"))
        process.wait()
    except OSError as exc:
        return False, f"Failed to run {cmd[0]}: {exc}"
    if process.returncode != 0:
        return False, f"{' '.join(cmd)} failed with exit code {process.returncode}."
    return True, "ffmpeg installed successfully."


class FfmpegDependency(Dependency):
    """Ensures ffmpeg is present on PATH or a known fallback location."""

    name = "ffmpeg"

    def check(self):
        """Return (is_present, message)."""
        if shutil.which("ffmpeg"):
            return True, "ffmpeg found on PATH."
        for path in _FALLBACK_PATHS.get(sys.platform, []):
            if os.path.isfile(path):
                return True, f"ffmpeg found at {path}."
        return False, "ffmpeg was not found on PATH or in common install locations."

    def install(self, progress_cb: ProgressCallback = None):
        """Install ffmpeg using the platform's native package manager."""
        if sys.platform == "win32":
            return _run_streaming(
                ["winget", "install", "--id", "Gyan.FFmpeg", "-e"], progress_cb
            )
        if sys.platform == "darwin":
            if not shutil.which("brew"):
                return False, "Homebrew not found. Install it from https://brew.sh, then retry."
            return _run_streaming(["brew", "install", "ffmpeg"], progress_cb)

        # Linux: auto-detect an available package manager.
        for manager, cmd in _LINUX_PKG_MANAGERS:
            if shutil.which(manager):
                if os.geteuid() != 0:
                    cmd = ["sudo"] + cmd
                return _run_streaming(cmd, progress_cb)
        return False, "No supported package manager (apt-get/dnf/yum/pacman/zypper/apk) found."


register(FfmpegDependency())
