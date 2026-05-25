"""
jj_dlp/twitch_eventsub.py
────────────────────────────────────────────────────────────────────────────────
Twitch EventSub integration — instant "stream.online" notifications.

How it works:
  1. A tiny HTTP server listens on WEBHOOK_PORT (default 8888).
  2. On startup (and whenever the streamer list changes) we:
       a. Get a Twitch app-access token via client_credentials OAuth.
       b. Resolve each streamer's login name → user_id via the Helix API.
       c. Subscribe to "stream.online" for every user_id via EventSub.
  3. Twitch sends an HMAC-signed POST to CALLBACK_URL when a streamer goes
     live.  We verify the signature, then immediately call the
     on_stream_online callback — no waiting for the 60-second poll.
  4. All subscriptions are cleaned up when the process exits.

Requirements:  Python ≥ 3.8 standard library only (no extra packages).
Optional:      If CALLBACK_URL uses https, you'll need a reverse proxy
               (e.g. nginx + Let's Encrypt) or ngrok in front of the server.

Usage from jj-dlp.py
─────────────────────
    from jj_dlp.twitch_eventsub import TwitchEventSub, EventSubState

    state = EventSubState()          # dashboard-visible status container
    es = TwitchEventSub(
        cfg          = initial_cfg,
        state        = state,
        on_stream_online = my_callback,   # called with (login, cfg)
        load_config_fn   = load_config,   # so the module can reload config
        dbg_fn           = dbg,           # optional debug logger
        log_fn           = log,           # optional info logger
    )
    es.start()   # launches daemon threads; non-blocking
    es.stop()    # signal shutdown and block until threads exit (optional)
────────────────────────────────────────────────────────────────────────────────
"""

import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Optional


# ── Public state container ────────────────────────────────────────────────────

class EventSubState:
    """
    Thread-safe container for dashboard-visible EventSub status.
    jj-dlp.py reads these fields directly from its render_dashboard() function.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.server_status: str  = "not started"   # e.g. "listening on port 8888"
        self.server_port: int    = 0
        self.last_notification: str  = ""           # human-readable last event
        self.notifications_total: int = 0           # count of verified push events
        # login → subscription_id (managed internally, read by dashboard)
        self._sub_lock = threading.Lock()
        self.subscription_ids: dict = {}

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_server_status(self) -> str:
        with self._lock:
            return self.server_status

    def set_server_status(self, status: str, port: int = 0) -> None:
        with self._lock:
            self.server_status = status
            if port:
                self.server_port = port

    def get_notification_info(self) -> tuple:
        """Returns (last_notification_str, total_count)."""
        with self._lock:
            return self.last_notification, self.notifications_total

    def record_notification(self, msg: str) -> int:
        """Increment counter, store last message. Returns new total."""
        with self._lock:
            self.notifications_total += 1
            self.last_notification = msg
            return self.notifications_total

    def get_subscription_ids(self) -> dict:
        with self._sub_lock:
            return dict(self.subscription_ids)

    def set_subscription(self, login: str, sub_id: str) -> None:
        with self._sub_lock:
            self.subscription_ids[login] = sub_id

    def remove_subscription(self, login: str) -> Optional[str]:
        with self._sub_lock:
            return self.subscription_ids.pop(login, None)

    def remove_subscription_by_id(self, sub_id: str) -> Optional[str]:
        """Remove the subscription matching sub_id. Returns the login or None."""
        with self._sub_lock:
            for login, sid in list(self.subscription_ids.items()):
                if sid == sub_id:
                    del self.subscription_ids[login]
                    return login
        return None


# ── Main integration class ────────────────────────────────────────────────────

class TwitchEventSub:
    """
    Manages Twitch EventSub subscriptions and the local webhook HTTP server.

    Parameters
    ──────────
    cfg              : initial config dict (same shape as load_config() returns)
    state            : EventSubState instance (shared with dashboard renderer)
    on_stream_online : callable(login: str, cfg: dict) — fired when a streamer
                       goes live; runs inside the HTTP-server thread.
    load_config_fn   : callable(path: str) → dict — used to reload config on
                       each poll and on live notifications.
    dbg_fn           : optional debug logger callable(msg)
    log_fn           : optional info logger callable(msg)
    """

    RESYNC_INTERVAL = 3600   # seconds — token refresh + subscription sanity check

    def __init__(
        self,
        cfg: dict,
        state: EventSubState,
        on_stream_online: Callable[[str, dict], None],
        load_config_fn: Callable[[str], dict],
        dbg_fn: Optional[Callable[[str], None]] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._initial_cfg    = cfg
        self.state           = state
        self._on_stream_online = on_stream_online
        self._load_config    = load_config_fn
        self._dbg            = dbg_fn or (lambda _: None)
        self._log            = log_fn or print

        self._stop_event = threading.Event()

        # Token cache — shared between manager and HTTP-handler threads
        self._token: str = ""
        self._token_lock = threading.Lock()

        # Live cfg snapshot — written by manager, read by HTTP handler
        self._cfg_snapshot: dict = dict(cfg)
        self._cfg_snapshot_lock = threading.Lock()

        self._http_thread: Optional[threading.Thread] = None
        self._mgr_thread:  Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch daemon threads; non-blocking."""
        with self._cfg_snapshot_lock:
            self._cfg_snapshot.update(self._initial_cfg)

        self._http_thread = threading.Thread(
            target=self._http_server_loop,
            args=(self._initial_cfg, self._stop_event),
            daemon=True,
            name="eventsub-http",
        )
        self._http_thread.start()

        self._mgr_thread = threading.Thread(
            target=self._manager_loop,
            args=(self._initial_cfg.get("config_path", ""), self._stop_event),
            daemon=True,
            name="eventsub-manager",
        )
        self._mgr_thread.start()

        self._dbg("[TWITCH][EventSub] start(): HTTP and manager threads launched")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal shutdown; optionally wait for threads to exit."""
        self._stop_event.set()
        if self._http_thread:
            self._http_thread.join(timeout=timeout)
        if self._mgr_thread:
            self._mgr_thread.join(timeout=timeout)

    # ── Token helpers ─────────────────────────────────────────────────────────

    def _get_token(self, client_id: str, client_secret: str) -> str:
        """Fetch a fresh app-access token from Twitch via client_credentials."""
        url  = "https://id.twitch.tv/oauth2/token"
        body = (
            f"client_id={client_id}&client_secret={client_secret}"
            "&grant_type=client_credentials"
        ).encode()
        self._dbg(f"[TWITCH] token: POST {url}")
        req = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data       = json.loads(resp.read())
                token      = data.get("access_token", "")
                expires_in = data.get("expires_in", "?")
                if token:
                    self._dbg(
                        f"[TWITCH] token: obtained OK  "
                        f"(expires_in={expires_in}s, prefix={token[:8]}...)"
                    )
                else:
                    self._dbg(f"[TWITCH] token: no access_token in response — body: {data}")
                return token
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            self._log(f"[Twitch] token fetch failed: HTTP {e.code} — {detail}")
            self._dbg(f"[TWITCH] token: HTTPError {e.code}: {detail}")
            return ""
        except Exception as e:
            self._log(f"[Twitch] token fetch failed: {e}")
            self._dbg(f"[TWITCH] token: exception: {type(e).__name__}: {e}")
            return ""

    def _ensure_token(self, client_id: str, client_secret: str) -> str:
        """Return cached token, or fetch a new one if the cache is empty."""
        with self._token_lock:
            if self._token:
                self._dbg(f"[TWITCH] token: using cached (prefix={self._token[:8]}...)")
                return self._token
        new_token = self._get_token(client_id, client_secret)
        with self._token_lock:
            self._token = new_token
        return new_token

    def _invalidate_token(self) -> None:
        with self._token_lock:
            self._token = ""
        self._dbg("[TWITCH] token: cache invalidated — will re-fetch next sync")

    # ── Helix API helper ──────────────────────────────────────────────────────

    def _api(
        self,
        path: str,
        client_id: str,
        token: str,
        method: str = "GET",
        data: bytes = None,
        params: dict = None,
    ) -> dict:
        """Minimal Twitch Helix API helper. Returns parsed JSON dict or {}."""
        base = "https://api.twitch.tv/helix"
        if params:
            qs  = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{base}{path}?{qs}"
        else:
            url = f"{base}{path}"
        self._dbg(
            f"[TWITCH] API {method} {url}  (body_len={len(data) if data else 0})"
        )
        headers = {
            "Client-Id":    client_id,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw    = resp.read()
                result = json.loads(raw)
                self._dbg(
                    f"[TWITCH] API {method} {path} → HTTP 200  ({len(raw)} bytes)"
                )
                return result
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            self._dbg(f"[TWITCH] API {method} {path} → HTTP {e.code}: {body}")
            try:
                return json.loads(body)
            except Exception:
                return {"_http_status": e.code}
        except Exception as e:
            self._dbg(
                f"[TWITCH] API {method} {path} → exception: {type(e).__name__}: {e}"
            )
            return {}

    # ── User-id resolution ────────────────────────────────────────────────────

    def _resolve_user_ids(
        self, logins: list, client_id: str, token: str
    ) -> dict:
        """Return {login_lower: user_id} for all resolved logins."""
        result = {}
        self._dbg(
            f"[TWITCH] resolve_user_ids: resolving {len(logins)} login(s): {logins}"
        )
        for i in range(0, len(logins), 100):
            chunk     = logins[i : i + 100]
            params_str = "&".join(f"login={l}" for l in chunk)
            url       = f"https://api.twitch.tv/helix/users?{params_str}"
            self._dbg(f"[TWITCH] resolve_user_ids: GET {url}")
            req = urllib.request.Request(
                url,
                headers={
                    "Client-Id":    client_id,
                    "Authorization": f"Bearer {token}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data        = json.loads(resp.read())
                    users_found = data.get("data", [])
                    self._dbg(
                        f"[TWITCH] resolve_user_ids: API returned "
                        f"{len(users_found)} user(s)"
                    )
                    for u in users_found:
                        login_lower = u["login"].lower()
                        uid         = u["id"]
                        result[login_lower] = uid
                        self._dbg(
                            f"[TWITCH] resolve_user_ids:   "
                            f"{login_lower!r} → user_id={uid}"
                        )
                    found_logins = {u["login"].lower() for u in users_found}
                    for missing in chunk:
                        if missing.lower() not in found_logins:
                            self._dbg(
                                f"[TWITCH] resolve_user_ids:   {missing!r} → "
                                "NOT FOUND (check spelling / account exists?)"
                            )
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                self._log(f"[Twitch] user-id resolve failed: HTTP {e.code} — {body}")
                self._dbg(f"[TWITCH] resolve_user_ids: HTTPError {e.code}: {body}")
            except Exception as e:
                self._log(f"[Twitch] user-id resolve failed: {e}")
                self._dbg(
                    f"[TWITCH] resolve_user_ids: exception: {type(e).__name__}: {e}"
                )
        self._dbg(f"[TWITCH] resolve_user_ids: final result = {result}")
        return result

    # ── Subscription management ───────────────────────────────────────────────

    def _subscribe(
        self,
        user_id: str,
        client_id: str,
        token: str,
        callback_url: str,
        webhook_secret: str,
    ) -> str:
        """
        Subscribe to stream.online for user_id via EventSub.
        Returns the subscription id string, or '' on failure.
        """
        self._dbg(
            f"[TWITCH] subscribe: creating stream.online subscription "
            f"for user_id={user_id}  callback={callback_url}"
        )
        payload = json.dumps({
            "type":    "stream.online",
            "version": "1",
            "condition": {"broadcaster_user_id": user_id},
            "transport": {
                "method":   "webhook",
                "callback": callback_url,
                "secret":   webhook_secret,
            },
        }).encode()
        resp = self._api(
            "/eventsub/subscriptions", client_id, token,
            method="POST", data=payload
        )
        subs = resp.get("data", [])
        if subs:
            sub    = subs[0]
            sub_id = sub.get("id", "")
            status = sub.get("status", "?")
            self._dbg(f"[TWITCH] subscribe: OK — sub_id={sub_id}  status={status}")
            return sub_id

        # HTTP 409: subscription already exists — reuse the existing id
        if resp.get("status") == 409 or resp.get("error") == "Conflict":
            msg         = resp.get("message", "")
            existing_id = ""
            if "id=" in msg:
                existing_id = msg.split("id=", 1)[1].strip()
            if existing_id:
                self._dbg(
                    f"[TWITCH] subscribe: 409 Conflict — "
                    f"reusing existing sub_id={existing_id}"
                )
                return existing_id
            self._dbg(
                f"[TWITCH] subscribe: 409 Conflict but could not parse "
                f"existing id from message={msg!r}"
            )
            return ""

        error_msg  = resp.get("message", "")
        error_code = resp.get("error", "")
        self._dbg(
            f"[TWITCH] subscribe: FAILED for user_id={user_id} — "
            f"error={error_code!r}  message={error_msg!r}  full_resp={resp}"
        )
        return ""

    def _unsubscribe(self, sub_id: str, client_id: str, token: str) -> None:
        """Delete an EventSub subscription by id."""
        self._dbg(f"[TWITCH] unsubscribe: deleting sub_id={sub_id}")
        self._api(
            f"/eventsub/subscriptions?id={sub_id}", client_id, token,
            method="DELETE"
        )
        self._dbg(f"[TWITCH] unsubscribe: done sub_id={sub_id}")

    def _sync_subscriptions(self, cfg: dict) -> None:
        """
        Ensure we have active EventSub subscriptions for all configured
        streamers and that subscriptions for removed streamers are deleted.
        """
        client_id      = cfg["twitch_client_id"]
        client_secret  = cfg["twitch_client_secret"]
        webhook_secret = cfg["twitch_webhook_secret"]
        callback_url   = cfg["twitch_callback_url"]
        streamers      = [s for s in cfg["streamers"] if s not in cfg["blocked"]]

        self._dbg(
            f"[TWITCH] sync_subscriptions: entry  streamers={streamers}  "
            f"callback_url={callback_url!r}"
        )

        token = self._ensure_token(client_id, client_secret)
        if not token:
            self._log(
                "[Twitch] EventSub: could not obtain access token — "
                "skipping subscription sync this cycle (will retry in 15s)"
            )
            self._dbg("[TWITCH] sync_subscriptions: aborting — no token")
            return

        # Update the cfg snapshot used by the HTTP server
        with self._cfg_snapshot_lock:
            self._cfg_snapshot.update(cfg)
        self._dbg("[TWITCH] sync_subscriptions: cfg snapshot updated")

        already_subscribed = set(self.state.get_subscription_ids().keys())
        to_subscribe       = [s for s in streamers if s not in already_subscribed]
        to_unsubscribe     = [s for s in already_subscribed if s not in streamers]

        self._dbg(
            f"[TWITCH] sync_subscriptions: "
            f"already_subscribed={sorted(already_subscribed)}  "
            f"to_subscribe={to_subscribe}  to_unsubscribe={to_unsubscribe}"
        )

        # Remove stale subscriptions
        for login in to_unsubscribe:
            sub_id = self.state.remove_subscription(login)
            if sub_id:
                self._dbg(
                    f"[TWITCH] sync_subscriptions: "
                    f"unsubscribing {login!r}  sub_id={sub_id}"
                )
                self._unsubscribe(sub_id, client_id, token)
                self._log(
                    f"[Twitch] EventSub: unsubscribed {login} "
                    "(removed from [Streamers])"
                )
            else:
                self._dbg(
                    f"[TWITCH] sync_subscriptions: "
                    f"{login!r} had no sub_id stored — nothing to delete"
                )

        if not to_subscribe:
            self._dbg("[TWITCH] sync_subscriptions: nothing new to subscribe — done")
            return

        self._dbg(
            f"[TWITCH] sync_subscriptions: resolving user IDs for {to_subscribe}"
        )
        login_to_id = self._resolve_user_ids(to_subscribe, client_id, token)

        for login in to_subscribe:
            user_id = login_to_id.get(login)
            if not user_id:
                self._log(
                    f"[Twitch] EventSub: could not resolve user_id for '{login}' "
                    "— check spelling / the account exists on Twitch"
                )
                self._dbg(
                    f"[TWITCH] sync_subscriptions: "
                    f"skipping {login!r} — no user_id resolved"
                )
                continue

            self._dbg(
                f"[TWITCH] sync_subscriptions: "
                f"subscribing {login!r} (user_id={user_id})"
            )
            sub_id = self._subscribe(
                user_id, client_id, token, callback_url, webhook_secret
            )
            if sub_id:
                self.state.set_subscription(login, sub_id)
                self._log(
                    f"[Twitch] EventSub: subscribed to stream.online for {login} "
                    f"(sub_id={sub_id})"
                )
                self._dbg(
                    f"[TWITCH] sync_subscriptions: "
                    f"{login!r} subscribed OK  sub_id={sub_id}"
                )
            else:
                self._log(
                    f"[Twitch] EventSub: subscription FAILED for {login} "
                    "(check CALLBACK_URL is reachable from the internet)"
                )
                self._dbg(
                    f"[TWITCH] sync_subscriptions: "
                    f"{login!r} subscription returned empty id"
                )

        final_subs = self.state.get_subscription_ids()
        self._dbg(
            f"[TWITCH] sync_subscriptions: done — active subscriptions: {final_subs}"
        )

    # ── Signature verification ────────────────────────────────────────────────

    @staticmethod
    def _verify_signature(
        secret: str,
        msg_id: str,
        msg_timestamp: str,
        body: bytes,
        twitch_sig: str,
    ) -> bool:
        """Return True if the HMAC-SHA256 signature from Twitch is valid."""
        hmac_message = (msg_id + msg_timestamp).encode() + body
        expected     = "sha256=" + hmac.new(
            secret.encode(), hmac_message, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, twitch_sig)

    # ── HTTP request handler ──────────────────────────────────────────────────

    def _handle_request(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        cfg: dict,
    ) -> tuple:
        """
        Process one inbound HTTP request from Twitch.
        Returns (status_code: int, response_body: bytes).
        """
        secret    = cfg.get("twitch_webhook_secret", "")
        msg_type  = headers.get("twitch-eventsub-message-type", "")
        msg_id    = headers.get("twitch-eventsub-message-id", "")
        msg_ts    = headers.get("twitch-eventsub-message-timestamp", "")
        signature = headers.get("twitch-eventsub-message-signature", "")

        self._dbg(
            f"[TWITCH] http_handler: {method} {path}  "
            f"msg-type={msg_type!r}  msg-id={msg_id!r}  "
            f"body_len={len(body)}  sig_present={bool(signature)}"
        )

        if method != "POST":
            self._dbg(
                f"[TWITCH] http_handler: rejecting non-POST request ({method})"
            )
            return 405, b"Method Not Allowed"

        if not signature:
            self._dbg(
                "[TWITCH] http_handler: no Twitch signature header present — "
                "this may not be a real Twitch request, or HMAC headers are missing"
            )

        if not self._verify_signature(secret, msg_id, msg_ts, body, signature):
            self._log(
                "[Twitch] EventSub: signature verification FAILED — "
                "ignoring request (wrong WEBHOOK_SECRET, or not from Twitch)"
            )
            self._dbg("[TWITCH] http_handler: 403 — signature mismatch")
            return 403, b"Forbidden"

        self._dbg("[TWITCH] http_handler: signature OK")

        try:
            payload = json.loads(body)
        except Exception as e:
            self._dbg(
                f"[TWITCH] http_handler: JSON parse error: {e}  body={body[:200]!r}"
            )
            return 400, b"Bad Request"

        # ── Challenge (sent once when we subscribe) ───────────────────────────
        if msg_type == "webhook_callback_verification":
            challenge = payload.get("challenge", "")
            sub_info  = payload.get("subscription", {})
            sub_type  = sub_info.get("type", "?")
            sub_cond  = sub_info.get("condition", {})
            self._dbg(
                f"[TWITCH] http_handler: challenge verification request "
                f"type={sub_type}  condition={sub_cond}  challenge={challenge!r}"
            )
            self._log(
                f"[Twitch] EventSub: challenge verified for {sub_type} "
                f"(condition={sub_cond}) — subscription is now active"
            )
            self.state.record_notification(f"challenge OK for {sub_type}")
            return 200, challenge.encode()

        # ── Live notification ─────────────────────────────────────────────────
        if msg_type == "notification":
            event             = payload.get("event", {})
            broadcaster_login = event.get("broadcaster_user_login", "").lower()
            broadcaster_id    = event.get("broadcaster_user_id", "?")
            stream_type       = event.get("type", "?")
            started_at        = event.get("started_at", "")

            self._dbg(
                f"[TWITCH] http_handler: NOTIFICATION  "
                f"login={broadcaster_login!r}  id={broadcaster_id}  "
                f"stream_type={stream_type!r}  started_at={started_at!r}"
            )

            if broadcaster_login:
                ts_str = datetime.now().strftime("%H:%M:%S")
                total  = self.state.record_notification(
                    f"{broadcaster_login} went live at {ts_str} "
                    f"(#{self.state.notifications_total})"
                )
                self._log(
                    f"[Twitch] EventSub: *** {broadcaster_login} just went live "
                    f"(stream_type={stream_type}) — triggering recording immediately ***\n"
                )
                # Fire the callback provided by jj-dlp.py
                self._dbg(
                    f"[TWITCH] http_handler: calling on_stream_online "
                    f"for {broadcaster_login!r}"
                )
                self._on_stream_online(broadcaster_login, cfg)
                self._dbg(
                    "[TWITCH] http_handler: on_stream_online returned"
                )
            else:
                self._dbg(
                    "[TWITCH] http_handler: notification had no "
                    f"broadcaster_user_login — full event: {event}"
                )
            return 200, b"OK"

        # ── Subscription revocation ───────────────────────────────────────────
        if msg_type == "revocation":
            sub_info   = payload.get("subscription", {})
            sub_type   = sub_info.get("type", "?")
            sub_status = sub_info.get("status", "?")
            sub_cond   = sub_info.get("condition", {})
            sub_id     = sub_info.get("id", "?")
            self._dbg(
                f"[TWITCH] http_handler: REVOCATION  sub_id={sub_id}  "
                f"type={sub_type}  status={sub_status}  condition={sub_cond}"
            )
            self._log(
                f"[Twitch] EventSub: subscription REVOKED "
                f"(type={sub_type}  status={sub_status}  condition={sub_cond}) "
                "— will resubscribe within 15 seconds"
            )
            login = self.state.remove_subscription_by_id(sub_id)
            if login:
                self._dbg(
                    f"[TWITCH] http_handler: "
                    f"removed revoked sub for login={login!r}"
                )
            return 200, b"OK"

        # ── Unknown message type ──────────────────────────────────────────────
        self._dbg(
            f"[TWITCH] http_handler: unhandled msg_type={msg_type!r} "
            "— returning 200 anyway"
        )
        return 200, b"OK"

    # ── HTTP server loop ──────────────────────────────────────────────────────

    def _http_server_loop(
        self, cfg: dict, stop_event: threading.Event
    ) -> None:
        """
        Blocking HTTP server — run in a daemon thread.
        Handles one request at a time (sufficient for EventSub volumes).
        """
        port         = cfg.get("twitch_webhook_port", 8888)
        callback_url = cfg.get("twitch_callback_url", "?")
        self._dbg(
            f"[TWITCH] http_server: starting — binding to 0.0.0.0:{port}  "
            f"callback_url={callback_url!r}"
        )

        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", port))
            srv.listen(16)
            srv.settimeout(1.0)
            self.state.set_server_status(f"listening on port {port}", port=port)
            self._log(
                f"[Twitch] EventSub webhook server listening on 0.0.0.0:{port}"
            )
            self._dbg(f"[TWITCH] http_server: bind+listen OK on port {port}")
        except Exception as e:
            err = f"ERROR: could not bind port {port}: {e}"
            self.state.set_server_status(err)
            self._log(f"[Twitch] EventSub: {err}")
            self._dbg(
                f"[TWITCH] http_server: fatal bind error: "
                f"{type(e).__name__}: {e}"
            )
            return

        req_count = 0
        while not stop_event.is_set():
            try:
                conn, addr = srv.accept()
            except OSError:
                continue   # accept() times out every 1 s — normal

            req_count += 1
            self._dbg(
                f"[TWITCH] http_server: accepted connection #{req_count} from {addr}"
            )

            try:
                data = b""
                conn.settimeout(5.0)
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\r\n\r\n" in data:
                        header_part, _, body_so_far = data.partition(b"\r\n\r\n")
                        cl = 0
                        for hline in header_part.split(b"\r\n"):
                            if hline.lower().startswith(b"content-length:"):
                                try:
                                    cl = int(hline.split(b":", 1)[1].strip())
                                except Exception:
                                    pass
                        if len(body_so_far) >= cl:
                            self._dbg(
                                f"[TWITCH] http_server: req #{req_count} "
                                f"headers_len={len(header_part)} "
                                f"body_len={len(body_so_far)} "
                                f"content-length={cl}"
                            )
                            break

                if not data:
                    self._dbg(
                        f"[TWITCH] http_server: req #{req_count} from {addr} "
                        "— empty data, closing"
                    )
                    conn.close()
                    continue

                header_part, _, body = data.partition(b"\r\n\r\n")
                header_lines = header_part.decode(errors="replace").splitlines()
                request_line = header_lines[0] if header_lines else ""
                parts        = request_line.split(" ")
                method       = parts[0] if parts else "GET"
                path         = parts[1] if len(parts) > 1 else "/"

                headers = {}
                for hl in header_lines[1:]:
                    if ":" in hl:
                        k, _, v = hl.partition(":")
                        headers[k.strip().lower()] = v.strip()

                self._dbg(
                    f"[TWITCH] http_server: req #{req_count} parsed — "
                    f"{method} {path}  headers_count={len(headers)}"
                )

                with self._cfg_snapshot_lock:
                    active_cfg = dict(self._cfg_snapshot)

                status, resp_body = self._handle_request(
                    method, path, headers, body, active_cfg
                )

                response = (
                    f"HTTP/1.1 {status} OK\r\n"
                    f"Content-Length: {len(resp_body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode() + resp_body
                conn.sendall(response)
                self._dbg(
                    f"[TWITCH] http_server: req #{req_count} → responded {status}"
                )

            except Exception as e:
                self._dbg(
                    f"[TWITCH] http_server: req #{req_count} handler exception: "
                    f"{type(e).__name__}: {e}"
                )
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        srv.close()
        self.state.set_server_status("stopped")
        self._log("[Twitch] EventSub webhook server stopped")
        self._dbg("[TWITCH] http_server: thread exiting")

    # ── Manager loop ──────────────────────────────────────────────────────────

    def _manager_loop(
        self, config_path: str, stop_event: threading.Event
    ) -> None:
        """
        Long-running thread that keeps EventSub subscriptions in sync with the
        current streamer list.  Re-syncs on streamer changes and once per hour
        to rotate the access token.
        """
        last_streamers: set  = set()
        last_sync_time: float = 0.0
        loop_count            = 0

        self._dbg("[TWITCH] manager_thread: started")
        self._log(
            "[Twitch] EventSub manager started — will sync subscriptions now"
        )

        while not stop_event.is_set():
            loop_count += 1
            self._dbg(f"[TWITCH] manager_thread: loop #{loop_count}")
            try:
                cfg = self._load_config(config_path)
                if not cfg.get("twitch_enabled"):
                    self._dbg(
                        "[TWITCH] manager_thread: "
                        "twitch_enabled=False in config — sleeping 30s"
                    )
                    stop_event.wait(timeout=30)
                    continue

                current_streamers = set(
                    s for s in cfg["streamers"] if s not in cfg["blocked"]
                )
                now              = time.time()
                time_since_sync  = now - last_sync_time
                streamers_changed = current_streamers != last_streamers
                token_stale       = time_since_sync >= self.RESYNC_INTERVAL

                self._dbg(
                    f"[TWITCH] manager_thread: "
                    f"current_streamers={sorted(current_streamers)}  "
                    f"last_streamers={sorted(last_streamers)}  "
                    f"streamers_changed={streamers_changed}  "
                    f"time_since_sync={time_since_sync:.0f}s  "
                    f"token_stale={token_stale}"
                )

                if streamers_changed or token_stale or last_sync_time == 0.0:
                    reason = []
                    if last_sync_time == 0.0:
                        reason.append("first run")
                    if streamers_changed:
                        reason.append(
                            f"streamers changed "
                            f"({sorted(last_streamers)} → {sorted(current_streamers)})"
                        )
                    if token_stale:
                        reason.append(
                            f"token refresh due ({time_since_sync:.0f}s elapsed)"
                        )
                        self._invalidate_token()

                    self._dbg(
                        f"[TWITCH] manager_thread: syncing — reason: "
                        f"{'; '.join(reason)}"
                    )
                    self._log(
                        f"[Twitch] EventSub: syncing subscriptions "
                        f"({', '.join(reason)})"
                    )
                    self._sync_subscriptions(cfg)
                    last_streamers = current_streamers
                    last_sync_time = now
                    self._dbg("[TWITCH] manager_thread: sync complete")
                else:
                    self._dbg(
                        "[TWITCH] manager_thread: no sync needed — "
                        f"next token refresh in "
                        f"{self.RESYNC_INTERVAL - time_since_sync:.0f}s"
                    )

            except Exception as e:
                import traceback
                self._log(f"[Twitch] EventSub manager error: {e}")
                self._dbg(
                    f"[TWITCH] manager_thread: exception in loop #{loop_count}: "
                    f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                )

            self._dbg("[TWITCH] manager_thread: sleeping 15s before next check")
            stop_event.wait(timeout=15)

        # ── Shutdown: clean up all subscriptions ──────────────────────────────
        self._dbg(
            "[TWITCH] manager_thread: stop_event set — cleaning up subscriptions"
        )
        self._log("[Twitch] EventSub manager: cleaning up subscriptions...")
        try:
            cfg       = self._load_config(config_path)
            client_id = cfg["twitch_client_id"]
            with self._token_lock:
                token = self._token
            subs = self.state.get_subscription_ids()
            self._dbg(
                f"[TWITCH] manager_thread: deleting {len(subs)} subscription(s): "
                f"{list(subs.keys())}"
            )
            for login, sub_id in subs.items():
                try:
                    self._dbg(
                        f"[TWITCH] manager_thread: "
                        f"deleting sub for {login!r}  sub_id={sub_id}"
                    )
                    self._unsubscribe(sub_id, client_id, token)
                except Exception as e:
                    self._dbg(
                        f"[TWITCH] manager_thread: error deleting {login!r}: {e}"
                    )
        except Exception as e:
            self._dbg(f"[TWITCH] manager_thread: error during cleanup: {e}")
        self._log("[Twitch] EventSub manager: done")
        self._dbg("[TWITCH] manager_thread: exiting")
