"""ThreadingHTTPServer with HTTP Basic Auth and session-cookie issuance."""

from __future__ import annotations

import base64
import http.cookies
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, Type

from jj_dlp.core.config import app as app_config
from jj_dlp.core.engine.app_state import AppState
from jj_dlp.core.web import api

log = logging.getLogger("jj_dlp.web.server")

SESSION_COOKIE_NAME = "jjdlp_session"
SESSION_TTL_SEC = 24 * 60 * 60
REALM = "jj-dlp"


class SessionStore:
    """In-memory session tokens with server-side expiry."""

    def __init__(self, ttl_sec: float = SESSION_TTL_SEC) -> None:
        self.ttl_sec = ttl_sec
        self._lock = threading.Lock()
        self._sessions: Dict[str, float] = {}

    def create(self) -> str:
        """Issue a new random session token, valid for ttl_sec seconds from now."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.time() + self.ttl_sec
        return token

    def is_valid(self, token: Optional[str]) -> bool:
        """Return whether a session token exists and hasn't expired, dropping it if it has."""
        if not token:
            return False
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry < time.time():
                del self._sessions[token]
                return False
            return True

    def revoke(self, token: Optional[str]) -> None:
        """Remove a session token, if present."""
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def purge_expired(self) -> None:
        """Drop every expired session token."""
        now = time.time()
        with self._lock:
            for token in [t for t, exp in self._sessions.items() if exp < now]:
                del self._sessions[token]


def _check_basic_auth(header: Optional[str], user: str, password: str) -> bool:
    """Return whether an Authorization header carries valid HTTP Basic credentials."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    if ":" not in decoded:
        return False
    supplied_user, supplied_pass = decoded.split(":", 1)
    return secrets.compare_digest(supplied_user, user) and secrets.compare_digest(supplied_pass, password)


def _get_session_token(handler: BaseHTTPRequestHandler) -> Optional[str]:
    """Extract the session cookie's value from a request's Cookie header, if present."""
    header = handler.headers.get("Cookie")
    if not header:
        return None
    cookie: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
    cookie.load(header)
    morsel = cookie.get(SESSION_COOKIE_NAME)
    return morsel.value if morsel else None


RouteHandler = Callable[[BaseHTTPRequestHandler], None]


def _build_routes(app_state: AppState, data_dir: Path) -> Dict[Tuple[str, str], RouteHandler]:
    """Map (method, path) to the api.py handler that serves it."""
    return {
        ("GET", "/api/status"): lambda h: api.handle_status(h, app_state, data_dir),
        ("POST", "/api/streamers/add"): lambda h: api.handle_add_streamer(h, app_state, data_dir),
        ("POST", "/api/streamers/remove"): lambda h: api.handle_remove_streamer(h, app_state, data_dir),
        ("POST", "/api/streamers/disable"): lambda h: api.handle_disable_streamer(h, app_state, data_dir),
    }


def make_handler_class(
    user: str,
    password: str,
    sessions: SessionStore,
    app_state: AppState,
    data_dir: Path,
) -> Type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass enforcing auth/sessions and routing for one server instance."""

    routes = _build_routes(app_state, data_dir)

    class AuthHandler(BaseHTTPRequestHandler):
        server_version = "jj-dlp/1.0"
        _session_cookie_header: Optional[str] = None

        def end_headers(self) -> None:
            """Inject a pending Set-Cookie header, if one was queued for this response."""
            if self._session_cookie_header is not None:
                self.send_header("Set-Cookie", self._session_cookie_header)
                self._session_cookie_header = None
            super().end_headers()

        def _has_valid_session(self) -> bool:
            """Return whether this request carries an unexpired session cookie."""
            return sessions.is_valid(_get_session_token(self))

        def _has_valid_basic_auth(self) -> bool:
            """Return whether this request carries valid HTTP Basic credentials."""
            return _check_basic_auth(self.headers.get("Authorization"), user, password)

        def _send_auth_challenge(self) -> None:
            """Respond 401 with a Basic Auth challenge and a JSON error body."""
            body = b'{"error": "Authentication required"}'
            self.send_response(401)
            self.send_header("WWW-Authenticate", f'Basic realm="{REALM}"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _queue_session_cookie(self) -> None:
            """Issue a fresh session token and queue it to go out with this response's headers."""
            token = sessions.create()
            cookie: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
            cookie[SESSION_COOKIE_NAME] = token
            cookie[SESSION_COOKIE_NAME]["path"] = "/"
            cookie[SESSION_COOKIE_NAME]["max-age"] = int(sessions.ttl_sec)
            cookie[SESSION_COOKIE_NAME]["httponly"] = True
            self._session_cookie_header = cookie[SESSION_COOKIE_NAME].OutputString()

        def _route(self) -> None:
            """Look up this request's (method, path) and dispatch to its handler, or 404."""
            path = self.path.split("?", 1)[0]
            route = routes.get((self.command, path))
            if route is None:
                api.write_error(self, 404, f"No such route: {self.command} {path}")
                return
            route(self)

        def _handle(self) -> None:
            """Authenticate the request, queue a session cookie if newly authenticated, then route it."""
            has_session = self._has_valid_session()
            if not has_session and not self._has_valid_basic_auth():
                self._send_auth_challenge()
                return
            if not has_session:
                self._queue_session_cookie()
            self._route()

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def log_message(self, fmt: str, *args) -> None:
            """Route request logs through the standard logging module instead of stderr."""
            log.debug("%s - %s", self.address_string(), fmt % args)

    return AuthHandler


class WebServer:
    """Threaded HTTP server enforcing auth/sessions and routing requests to core/web/api.py."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        app_state: AppState,
        data_dir: Path,
    ) -> None:
        self.host = host
        self.port = port
        self.sessions = SessionStore()
        handler_cls = make_handler_class(user, password, self.sessions, app_state, data_dir)
        self._httpd = ThreadingHTTPServer((host, port), handler_cls)
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start serving requests on a background daemon thread."""
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        log.info("Web server listening on %s", self.url)

    def stop(self) -> None:
        """Shut down the server and wait for its serving thread to exit."""
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        """Return the base URL this server is reachable at."""
        return f"http://{self.host}:{self.port}/"


def build_server(
    config: app_config.WebUiConfig,
    app_state: AppState,
    data_dir: Path,
    host: str = "0.0.0.0",
) -> WebServer:
    """Construct a WebServer bound to a WebUiConfig's port and basic-auth credentials."""
    return WebServer(
        host=host,
        port=config.port,
        user=config.user,
        password=config.password,
        app_state=app_state,
        data_dir=data_dir,
    )
