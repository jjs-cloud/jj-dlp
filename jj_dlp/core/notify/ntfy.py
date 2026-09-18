"""ntfy.sh push notifications."""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger("jj_dlp.notify.ntfy")

NTFY_BASE_URL = "https://ntfy.sh"
REQUEST_TIMEOUT_SEC = 10


def notify_recording_started(
    site: str,
    streamer: str,
    ntfy_enabled: bool,
    ntfy_topic: str,
) -> None:
    """Push an ntfy.sh notification for a streamer starting to record, if enabled and configured."""
    if not ntfy_enabled or not ntfy_topic:
        return
    message = f"{streamer} is now recording ({site})"
    threading.Thread(
        target=_post, args=(ntfy_topic, message), daemon=True
    ).start()


def _post(topic: str, message: str) -> None:
    """Send the notification body to ntfy.sh, logging instead of raising on failure."""
    url = f"{NTFY_BASE_URL}/{topic}"
    request = urllib.request.Request(
        url, data=message.encode("utf-8"), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC):
            pass
    except (urllib.error.URLError, OSError) as exc:
        log.warning("ntfy notification failed: %s", exc)
