"""Twitch OAuth client-credentials token management (Doc 1 §21)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

from jj_dlp.core.notify import logger

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
REQUEST_TIMEOUT_SEC = 10
# refresh this many seconds before actual expiry, to avoid using a token
# that expires mid-request
EXPIRY_SAFETY_MARGIN_SEC = 60

_lock = threading.Lock()
# client_id -> (access_token, expires_at_epoch)
_cache: Dict[str, Tuple[str, float]] = {}


class TokenError(Exception):
    """Raised when a token could not be obtained from Twitch."""


def get_token(client_id: str, client_secret: str) -> str:
    """Return a cached or freshly-fetched app access token for client_id."""
    with _lock:
        cached = _cache.get(client_id)
        if cached is not None and cached[1] > time.time():
            return cached[0]
    return _refresh(client_id, client_secret)


def invalidate(client_id: str) -> None:
    """Drop any cached token for client_id, forcing a refresh on next use."""
    with _lock:
        _cache.pop(client_id, None)


def _refresh(client_id: str, client_secret: str) -> str:
    """Fetch a new token from Twitch, cache it, and return it."""
    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.dbg(f"eventsub token fetch failed: {exc}", tag="eventsub")
        raise TokenError(str(exc)) from exc

    access_token: Optional[str] = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not access_token or not isinstance(expires_in, (int, float)):
        logger.dbg(f"eventsub token response missing fields: {payload}", tag="eventsub")
        raise TokenError("malformed token response")

    expires_at = time.time() + expires_in - EXPIRY_SAFETY_MARGIN_SEC
    with _lock:
        _cache[client_id] = (access_token, expires_at)
    return access_token
