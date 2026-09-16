"""EventSub subscribe/unsubscribe/reconcile (Doc 1 §21)."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jj_dlp.core.config import state
from jj_dlp.core.eventsub import token
from jj_dlp.core.notify import logger

USERS_URL = "https://api.twitch.tv/helix/users"
SUBSCRIPTIONS_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"
REQUEST_TIMEOUT_SEC = 10
SUB_TYPES = ("stream.online", "stream.offline")


class SubscriptionError(Exception):
    """Raised when an EventSub/Helix API call fails unrecoverably."""


def derive_secret(client_secret: str, site: str) -> str:
    """Deterministically derive this site's webhook HMAC secret from its client secret."""
    return hmac.new(client_secret.encode("utf-8"), site.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers(client_id: str, access_token: str) -> Dict[str, str]:
    """Build standard Helix API auth headers."""
    return {
        "Client-Id": client_id,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _request_json(req: urllib.request.Request) -> Optional[dict]:
    """Send a request and parse a JSON response, logging and returning None on failure."""
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.dbg(f"eventsub request failed: {req.full_url}: {exc}", tag="eventsub")
        return None


def get_user_id(client_id: str, access_token: str, login: str) -> Optional[str]:
    """Resolve a streamer's numeric Twitch user id from their login name."""
    url = f"{USERS_URL}?{urllib.parse.urlencode({'login': login})}"
    payload = _request_json(urllib.request.Request(url, headers=_headers(client_id, access_token)))
    data = (payload or {}).get("data") or []
    return data[0]["id"] if data else None


def _list_subscriptions(client_id: str, access_token: str) -> List[Dict[str, Any]]:
    """Return every EventSub subscription currently registered under this client id."""
    results: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params = {"first": "100"}
        if cursor:
            params["after"] = cursor
        url = f"{SUBSCRIPTIONS_URL}?{urllib.parse.urlencode(params)}"
        payload = _request_json(urllib.request.Request(url, headers=_headers(client_id, access_token)))
        if payload is None:
            break
        results.extend(payload.get("data") or [])
        cursor = (payload.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    return results


def _create_subscription(
    client_id: str, access_token: str, sub_type: str, user_id: str, callback_url: str, secret: str
) -> Optional[str]:
    """Create one webhook subscription, returning its id, or None on failure."""
    body = json.dumps(
        {
            "type": sub_type,
            "version": "1",
            "condition": {"broadcaster_user_id": user_id},
            "transport": {"method": "webhook", "callback": callback_url, "secret": secret},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        SUBSCRIPTIONS_URL, data=body, headers=_headers(client_id, access_token), method="POST"
    )
    payload = _request_json(req)
    data = (payload or {}).get("data") or []
    return data[0]["id"] if data else None


def _delete_subscription(client_id: str, access_token: str, sub_id: str) -> None:
    """Delete one EventSub subscription by id."""
    url = f"{SUBSCRIPTIONS_URL}?{urllib.parse.urlencode({'id': sub_id})}"
    req = urllib.request.Request(url, headers=_headers(client_id, access_token), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC):
            pass
    except (urllib.error.URLError, OSError) as exc:
        logger.dbg(f"eventsub unsubscribe failed for {sub_id}: {exc}", tag="eventsub")


def _index_remote(
    remote_subs: List[Dict[str, Any]], callback_url: str
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Map (broadcaster_user_id, type) -> subscription for this callback's live subs."""
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for sub in remote_subs:
        transport = sub.get("transport", {})
        if transport.get("callback") != callback_url:
            continue
        if sub.get("status") not in ("enabled", "webhook_callback_verification_pending"):
            continue
        user_id = sub.get("condition", {}).get("broadcaster_user_id")
        sub_type = sub.get("type")
        if user_id and sub_type:
            index[(user_id, sub_type)] = sub
    return index


def _eventsub_settings(plugin_settings: Dict[str, Any]) -> Dict[str, str]:
    """Pull client_id/client_secret/callback_url out of a twitch site's plugin_settings."""
    cfg = plugin_settings.get("eventsub", {})
    return {
        "client_id": cfg.get("client_id", ""),
        "client_secret": cfg.get("client_secret", ""),
        "callback_url": cfg.get("callback_url", ""),
    }


def reconcile(
    data_dir: Path, site: str, plugin_settings: Dict[str, Any], streamers: List[str]
) -> None:
    """Unsubscribe stale and subscribe missing stream.online/offline hooks for a site's streamers."""
    creds = _eventsub_settings(plugin_settings)
    client_id, client_secret, callback_url = (
        creds["client_id"],
        creds["client_secret"],
        creds["callback_url"],
    )
    if not client_id or not client_secret or not callback_url:
        logger.dbg(f"eventsub reconcile skipped for {site}: missing client_id/secret/callback_url", tag="eventsub")
        return

    try:
        access_token = token.get_token(client_id, client_secret)
    except token.TokenError as exc:
        logger.dbg(f"eventsub reconcile aborted for {site}: {exc}", tag="eventsub")
        return

    secret = derive_secret(client_secret, site)
    remote_subs = _list_subscriptions(client_id, access_token)
    remote_index = _index_remote(remote_subs, callback_url)

    wanted_logins = set(streamers)
    tracked_logins = set(state.get_subscription_ids(data_dir, site).keys())

    # Unsubscribe stale: streamers no longer tracked for this site.
    for login in tracked_logins - wanted_logins:
        user_id = get_user_id(client_id, access_token, login)
        if user_id:
            for sub_type in SUB_TYPES:
                sub = remote_index.pop((user_id, sub_type), None)
                if sub:
                    _delete_subscription(client_id, access_token, sub["id"])
        state.set_subscription_id(data_dir, site, login, None)

    # Subscribe missing (or re-create anything Twitch no longer has for a tracked login).
    for login in wanted_logins:
        user_id = get_user_id(client_id, access_token, login)
        if not user_id:
            logger.dbg(f"eventsub: could not resolve user id for {login}", tag="eventsub")
            continue

        online_sub = remote_index.get((user_id, "stream.online"))
        if online_sub is None:
            online_id = _create_subscription(
                client_id, access_token, "stream.online", user_id, callback_url, secret
            )
        else:
            online_id = online_sub["id"]

        if (user_id, "stream.offline") not in remote_index:
            _create_subscription(
                client_id, access_token, "stream.offline", user_id, callback_url, secret
            )

        if online_id:
            state.set_subscription_id(data_dir, site, login, online_id)
        else:
            logger.dbg(f"eventsub: failed to subscribe {login}", tag="eventsub")

    logger.dbg(f"eventsub reconcile complete for {site}", tag="eventsub")
