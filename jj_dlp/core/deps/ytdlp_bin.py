"""Bundled per-platform yt-dlp binary resolution (Doc 1 Addendum §A.3)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("jj_dlp.deps.ytdlp_bin")

# (bin/ subdirectory, executable filename) per sys.platform value.
_PLATFORM_LAYOUT = {
    "win32": ("windows", "yt-dlp.exe"),
    "cygwin": ("windows", "yt-dlp.exe"),
    "darwin": ("macos", "yt-dlp"),
}
_DEFAULT_LAYOUT = ("linux", "yt-dlp")


def app_root() -> Path:
    """Return the install root directory that bin/ sits alongside the jj_dlp package in."""
    import jj_dlp

    return Path(jj_dlp.__file__).resolve().parent.parent


def resolve_yt_dlp_path() -> Path:
    """Return the bundled yt-dlp binary path for the current platform under bin/."""
    subdir, filename = _PLATFORM_LAYOUT.get(sys.platform, _DEFAULT_LAYOUT)
    path = app_root() / "bin" / subdir / "yt-dlp" / filename
    if not path.exists():
        log.warning("Bundled yt-dlp binary not found at %s", path)
    return path
