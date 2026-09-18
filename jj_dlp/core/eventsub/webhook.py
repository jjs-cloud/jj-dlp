"""EventSub HTTP callback server and signature verification (Doc 1 §21)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional, Set, Type

from jj_dlp.core.config import state as config_state
from jj_dlp.core.engine.checker import Checker
from jj_dlp.core.engine.site_state import SiteState
from jj_dlp.core.eventsub.subscriptions import derive_secret

log = logging.getLogger("jj_dlp.eventsub.webhook")

MESSAGE_ID_HEADER = "Twitch-Eventsub-Message-Id"
MESSAGE_TIMESTAMP_HEADER = "Twitch-Eventsub-Message-Timestamp"
MESSAGE_SIGNATURE_HEADER = "Twitch-Eventsub-Message-Signature"
MESSAGE_TYPE_HEADER = "Twitch-Eventsub-Message-Type"
SUBSCRIPTION_TYPE_HEADER = "Twitch-Eventsub-Subscription-Type"

TYPE_VERIFICATION = "webhook_callback_verification"
TYPE_NOTIFICATION = "notification"
TYPE_REVOCATION = "revocation"

MAX_SEEN_MESSAGE_IDS = 500

# Called with a streamer login on stream.online / stream.offline.
StreamerCallback = Callable[[str], None]


def verify_signature(
    secret: str, message_id: str, timestamp: str, body: bytes, signature: Optional[str]
) -> bool:
    """Return whether a request's HMAC-SHA256 signature matches the subscription secret."""
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), (message_id + timestamp).encode("utf-8") + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


class _SeenMessageIds:
    """Small FIFO set of recently processed message ids, to skip Twitch's retried notifications."""

    def __init__(self, max_len: int = MAX_SEEN_MESSAGE_IDS) -> None:
        self.max_len = max_len
        self._lock = threading.Lock()
        self._order: List[str] = []
        self._seen: Set[str] = set()

    def seen_before(self, message_id: str) -> bool:
        """Return True if already processed; otherwise record it now and return False."""
        with self._lock:
            if message_id in self._seen:
                return True
            self._seen.add(message_id)
            self._order.append(message_id)
            if len(self._order) > self.max_len:
                oldest = self._order.pop(0)
                self._seen.discard(oldest)
            return False


def _make_handler_class(
    secret: str,
    on_online: StreamerCallback,
    on_offline: StreamerCallback,
    seen: _SeenMessageIds,
    record_notification: Callable[[str, str], None],
) -> Type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass for one site's EventSub webhook."""

    class WebhookHandler(BaseHTTPRequestHandler):
        server_version = "jj-dlp-eventsub/1.0"

        def do_POST(self) -> None:
            """Verify, classify, and dispatch one incoming EventSub callback request."""
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""

            message_id = self.headers.get(MESSAGE_ID_HEADER, "")
            timestamp = self.headers.get(MESSAGE_TIMESTAMP_HEADER, "")
            signature = self.headers.get(MESSAGE_SIGNATURE_HEADER)
            message_type = self.headers.get(MESSAGE_TYPE_HEADER, "")

            if not verify_signature(secret, message_id, timestamp, body, signature):
                log.warning("eventsub webhook: rejected request with bad signature")
                self._respond(403, b"")
                return

            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                self._respond(400, b"")
                return

            if message_type == TYPE_VERIFICATION:
                challenge = payload.get("challenge", "")
                self._respond(200, challenge.encode("utf-8"), content_type="text/plain")
                return

            if message_type == TYPE_REVOCATION:
                log.warning("eventsub subscription revoked: %s", payload.get("subscription", {}))
                self._respond(200, b"")
                return

            if message_type != TYPE_NOTIFICATION:
                self._respond(200, b"")
                return

            if seen.seen_before(message_id):
                self._respond(200, b"")
                return

            subscription = payload.get("subscription", {})
            sub_type = self.headers.get(SUBSCRIPTION_TYPE_HEADER) or subscription.get("type")
            event = payload.get("event", {})
            login = event.get("broadcaster_user_login")

            if login and sub_type in ("stream.online", "stream.offline"):
                record_notification(sub_type, login)
                callback = on_online if sub_type == "stream.online" else on_offline
                try:
                    callback(login)
                except Exception:  # noqa: BLE001 - a bad hook must not break the webhook
                    log.exception("eventsub callback failed for %s", login)

            self._respond(200, b"")

        def _respond(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            """Send a status line, headers, and body for one response."""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            """Route request logs through the standard logging module instead of stderr."""
            log.debug("%s - %s", self.address_string(), fmt % args)

    return WebhookHandler


class EventSubWebhookServer:
    """Local HTTP server receiving one site's Twitch EventSub webhook notifications."""

    def __init__(
        self,
        data_dir: Path,
        site: str,
        port: int,
        secret: str,
        on_online: StreamerCallback,
        on_offline: StreamerCallback,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.site = site
        self.port = port
        seen = _SeenMessageIds()
        handler_cls = _make_handler_class(secret, on_online, on_offline, seen, self._record_notification)
        self._httpd = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
        self._thread: Optional[threading.Thread] = None

    def _record_notification(self, sub_type: str, login: str) -> None:
        """Append a received notification to this site's eventsub history."""
        config_state.add_notification(self.data_dir, self.site, {"type": sub_type, "login": login, "ts": time.time()})

    def start(self) -> None:
        """Start serving requests on a background daemon thread."""
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        config_state.set_server_status(self.data_dir, self.site, "running")
        log.info("EventSub webhook for %s listening on port %d", self.site, self.port)

    def stop(self) -> None:
        """Shut down the server and wait for its serving thread to exit."""
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        config_state.set_server_status(self.data_dir, self.site, "stopped")


def dispatch_to_checker(checker: Checker) -> StreamerCallback:
    """Return an on_online callback feeding straight into a Checker's live-detection path."""
    return lambda streamer: checker.on_live(streamer)


def dispatch_offline_to_site_state(site_state: SiteState) -> StreamerCallback:
    """Return an on_offline callback that clears a streamer's live_since via SiteState."""
    return lambda streamer: site_state.mark_offline(streamer)


def build_webhook_server(
    data_dir: Path,
    site: str,
    plugin_settings: dict,
    checker: Checker,
    site_state: SiteState,
) -> Optional[EventSubWebhookServer]:
    """Construct an EventSubWebhookServer for a twitch site from its plugin_settings, or None if unconfigured."""
    cfg = plugin_settings.get("eventsub", {})
    client_secret = cfg.get("client_secret", "")
    port = cfg.get("webhook_port", 8888)
    if not client_secret:
        log.warning("eventsub webhook not started for %s: missing client_secret", site)
        return None
    secret = derive_secret(client_secret, site)
    return EventSubWebhookServer(
        data_dir=data_dir,
        site=site,
        port=port,
        secret=secret,
        on_online=dispatch_to_checker(checker),
        on_offline=dispatch_offline_to_site_state(site_state),
    )
