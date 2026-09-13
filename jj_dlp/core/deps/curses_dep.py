"""Dependency check/install for curses."""

import subprocess
import sys

from jj_dlp.core.deps.registry import Dependency, ProgressCallback, register


class CursesDependency(Dependency):
    """Ensures a working curses module is importable."""

    name = "curses"

    def check(self):
        """Return (is_present, message)."""
        if sys.platform != "win32":
            return True, "curses is part of the standard library on this OS."
        try:
            import curses  # noqa: F401
        except ImportError:
            return False, "curses is not available (windows-curses not installed)."
        return True, "curses is available."

    def install(self, progress_cb: ProgressCallback = None):
        """Install windows-curses via pip on Windows."""
        if sys.platform != "win32":
            return True, "No install needed on this OS."
        cmd = [sys.executable, "-m", "pip", "install", "windows-curses"]
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in process.stdout:
                if progress_cb:
                    progress_cb(line.rstrip("\n"))
            process.wait()
        except OSError as exc:
            return False, f"Failed to run pip: {exc}"
        if process.returncode != 0:
            return False, "pip install windows-curses failed."
        return True, "windows-curses installed successfully."


register(CursesDependency())
