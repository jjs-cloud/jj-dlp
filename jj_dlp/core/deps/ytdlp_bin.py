"""Bundled per-platform yt-dlp binary resolution (Doc 1 Addendum §A.3)."""

from __future__ import annotations

import sys
from pathlib import Path

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
    return app_root() / "bin" / subdir / filename
