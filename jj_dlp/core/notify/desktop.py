"""Cross-platform desktop popup notifications."""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import threading
import time
from typing import Dict, Tuple

log = logging.getLogger("jj_dlp.notify.desktop")

_lock = threading.Lock()
_last_fired: Dict[Tuple[str, str], float] = {}


def notify_recording_started(
    site: str,
    streamer: str,
    popup_enabled: bool,
    popup_timeout_sec: int,
    popup_cooldown_sec: int,
) -> None:
    """Fire a desktop popup for a streamer starting to record, honoring enabled/cooldown gates."""
    if not popup_enabled:
        return
    now = time.monotonic()
    with _lock:
        last = _last_fired.get((site, streamer))
        if last is not None and now - last < popup_cooldown_sec:
            return
        _last_fired[(site, streamer)] = now
    title = "jj-dlp"
    message = f"{streamer} is now recording ({site})"
    threading.Thread(
        target=_show_popup, args=(title, message, popup_timeout_sec), daemon=True
    ).start()


def clear_cooldowns() -> None:
    """Forget every recorded per-streamer cooldown timestamp."""
    with _lock:
        _last_fired.clear()


def _show_popup(title: str, message: str, timeout_sec: int) -> None:
    """Dispatch to the current platform's native notifier, logging instead of raising on failure."""
    system = platform.system()
    try:
        if system == "Linux":
            _show_linux(title, message, timeout_sec)
        elif system == "Darwin":
            _show_macos(title, message)
        elif system == "Windows":
            _show_windows(title, message, timeout_sec)
        else:
            log.warning("no desktop notifier for platform %r", system)
    except Exception:
        log.exception("desktop notification failed")


def _show_linux(title: str, message: str, timeout_sec: int) -> None:
    """Show a notification via notify-send, if it's installed."""
    if shutil.which("notify-send") is None:
        log.warning("notify-send not found, skipping popup")
        return
    subprocess.run(
        ["notify-send", "-t", str(int(timeout_sec * 1000)), title, message],
        check=False,
    )


def _show_macos(title: str, message: str) -> None:
    """Show a notification via osascript's Notification Center integration."""
    script = f"display notification {_osa_quote(message)} with title {_osa_quote(title)}"
    subprocess.run(["osascript", "-e", script], check=False)


def _osa_quote(text: str) -> str:
    """Escape and quote a string for embedding in an AppleScript command."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _show_windows(title: str, message: str, timeout_sec: int) -> None:
    """Show a balloon-tip notification via a small PowerShell/WinForms script."""
    hold_sec = max(1, int(timeout_sec))
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip({int(timeout_sec * 1000)}, {_ps_quote(title)}, {_ps_quote(message)}, "
        "[System.Windows.Forms.ToolTipIcon]::Info); "
        f"Start-Sleep -Seconds {hold_sec}; "
        "$n.Dispose()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
    )


def _ps_quote(text: str) -> str:
    """Escape and quote a string for embedding in a PowerShell command."""
    escaped = text.replace("'", "''")
    return f"'{escaped}'"
