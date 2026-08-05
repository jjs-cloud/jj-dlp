"""
http_server.py — Embedded web dashboard for jj-dlp.

Design (v2 — read-only status + add/remove/disable streamer):
    * Zero external dependencies (stdlib http.server only).
    * Runs as a single daemon thread launched from main(), alongside the
      existing monitor/watcher threads. Reads the *same* SiteState objects
      the curses dashboard reads — no separate state, no IPC.
    * Opt-in: only starts if WEB_UI = true in global.conf [General].
    * HTTP Basic Auth is required whenever the server is enabled — there is
      no "no-auth" mode. Local WiFi is a soft boundary, not a trust boundary.
    * Binds 0.0.0.0 (reachable from other devices on the WiFi) but also
      works via 127.0.0.1 on the same machine, since it's a single listen
      socket on all interfaces.

Write support (this module): add / remove / disable a streamer, mirroring
what the curses management overlay ('a'/'r'/'d' keys) does. The actual
config-file mutation logic (_modify_config_streamer) still lives in
main.py and is handed in as a callback (modify_streamer_fn) so this module
doesn't import main.py — main.py already imports this module, and keeping
the dependency one-directional avoids a circular import.

Still NOT implemented: priority reorder, general config editing. Those
are meaningfully more complex (ordering semantics / arbitrary key-value
edits) and are left for a later pass.

Concurrency notes for the write path:
    * _CONFIG_WRITE_LOCK below serializes all streamer-management calls
      made through this server (ThreadingHTTPServer means concurrent
      requests are otherwise on separate threads). It does not coordinate
      with the curses TUI's own 'a'/'r'/'d' handling, but that runs on
      the single main/curses thread and only ever does one edit at a time,
      so the risk is limited to two web requests racing each other.
    * After a successful edit we call site.invalidate_config_cache() and
      site.trigger_event.set(), exactly like the curses handler does, so
      the monitor thread picks up the change and dash_all_streamers /
      dash_blocked (what /api/status reports) reflect it on the next
      snapshot. We do NOT reach into the curses ConfigEditor's own cache
      (ConfigEditor.load_config / priority_editor.force_reload) — this
      server has no reference to it, since the TUI object is constructed
      after start_web_server() is called. Practical effect: if someone is
      simultaneously looking at the Config tab in curses, it may show
      stale data until it's reopened. The Dashboard tab, activity log,
      and this web UI are unaffected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional

try:
    from . import logger as _logger
except ImportError:
    import logger as _logger

dbg = _logger.dbg


# ══════════════════════════════════════════════════════════════════════════
# Status snapshot — reads SiteState the same way the curses renderer does
# ══════════════════════════════════════════════════════════════════════════

def _build_status_snapshot(sites: List) -> dict:
    """Return a JSON-serializable snapshot of all sites' dashboard state.

    Mirrors what the curses renderer reads: everything here is taken under
    each site's dash_lock, matching the locking discipline documented on
    SiteState (dash_lock guards dash_* attributes).
    """
    now = time.time()
    out_sites = []

    for site in sites:
        with site.dash_lock:
            all_streamers = list(site.dash_all_streamers)
            live_since = dict(site.dash_live_since)
            blocked = set(site.dash_blocked)
            log_lines = list(site.dash_log_lines)[-100:]

        # currently_recording / evicted_streamers live under site.lock, not
        # dash_lock — mirror the renderer's practice of taking both.
        with site.lock:
            recording = set(site.currently_recording)
            evicted = set(site.evicted_streamers)

        streamers = []
        for name in all_streamers:
            is_live = name in live_since
            is_recording = name in recording
            since = live_since.get(name)
            streamers.append({
                "name": name,
                "live": is_live,
                "recording": is_recording,
                "blocked": name in blocked,
                "evicted": name in evicted,
                "live_since": since,
                "duration_s": (now - since) if since else None,
            })

        # site.label is fixed at startup to the config filename; the
        # user-facing title (SITE_LABEL in the config's [General] section,
        # defaulting to the filename) can change at runtime, so pull it
        # from the cached config the same way the rest of the app does.
        try:
            display_label = site.get_cached_config().get("site_label", site.label)
        except Exception:
            display_label = site.label

        out_sites.append({
            # site.label is the config filename — fixed at startup, unique,
            # and never changes at runtime, so it's used as the stable
            # identifier for API calls (add/remove/disable). display_label
            # (below) is only for showing to the user, and can change if
            # SITE_LABEL is edited in the config.
            "id": site.label,
            "label": display_label,
            "streamers": streamers,
            "log": log_lines,
        })

    return {"generated_at": now, "sites": out_sites}


# ══════════════════════════════════════════════════════════════════════════
# Streamer management (add / remove / disable) — mirrors the curses 'a' /
# 'r' / 'd' management overlay, driven over HTTP instead of the keyboard.
# ══════════════════════════════════════════════════════════════════════════

_ALLOWED_ACTIONS = ("add", "remove", "disable")
_MAX_USERNAME_LEN = 100
_MAX_BODY_BYTES = 4096

# Serializes config-file writes triggered through this server. See the
# "Concurrency notes" section of the module docstring.
_CONFIG_WRITE_LOCK = threading.Lock()

# ──────────────────────────────────────────────────────────────────────────
# Persistent session store (client remembers login via a cookie)
# ──────────────────────────────────────────────────────────────────────────
# HTTP Basic Auth is only remembered by browsers for the current browser
# session — close the browser (or launch as a mobile "standalone" PWA, where
# many browsers don't persist it at all) and you're asked for the username +
# password again. To fix that, the first successful Basic login issues a
# long-lived random session cookie; subsequent requests with a valid cookie
# are accepted without prompting for Basic Auth again. Credentials are still
# required on first login (auth stays mandatory — nothing becomes anonymous).
#
# The token store is *persisted to disk* so that active logins survive a
# server restart: tokens live for _SESSION_TTL after last use, get refreshed
# on each authenticated request, and are written to a JSON cache file in the
# jj_dlp package folder whenever the store changes. On startup the
# cache is reloaded, so a previously-logged-in client keeps its cookie and
# is not asked for the username/password again. Session tokens are random
# URL-safe secrets — not the
# user's credentials — though anyone who can read the file could impersonate
# a logged-in client, which is the same scope as the old in-memory store.
_SESSION_TTL = 30 * 24 * 3600            # 30 days of inactivity
_SESSION_COOKIE = "jj_dlp_session"
_SESSION_CACHE_FILE = ".web_sessions.json"
_SESSIONS: dict = {}
_SESSION_LOCK = threading.Lock()
_SESSIONS_FILE_LOCK = threading.Lock()
_SAVE_INTERVAL = 60                      # throttle sliding-expiry writes to 1/60s
_last_save_ts = 0.0


def _sessions_path() -> str:
    """Path of the persistent session cache, inside the jj_dlp package folder."""
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(pkg_dir, _SESSION_CACHE_FILE)


def _load_sessions() -> None:
    """Restore the persisted session store at startup so already-logged-in
    clients keep their cookie without re-entering their credentials."""
    path = _sessions_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    now = time.time()
    with _SESSION_LOCK:
        for sid, expiry in raw.items():
            try:
                exp = float(expiry)
            except (TypeError, ValueError):
                continue
            if exp > now:
                _SESSIONS[sid] = exp


def _save_sessions(force: bool = False) -> None:
    """Snapshot the session store to the cache file. Sliding-expiry refresh
    writes are throttled (_SAVE_INTERVAL); mints and expirations always force
    a write so the durable copy stays current. Writes are atomic (tmp+rename)
    so a crash mid-write never leaves a corrupt file behind."""
    global _last_save_ts
    now = time.time()
    with _SESSIONS_FILE_LOCK:
        if not force and (now - _last_save_ts) < _SAVE_INTERVAL:
            return
        _last_save_ts = now
        with _SESSION_LOCK:
            snapshot = dict(_SESSIONS)
        try:
            path = _sessions_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f)
            os.replace(tmp, path)
        except OSError:
            pass


def _valid_username(name: str) -> bool:
    """Match what curses' text-input already allows: non-empty, printable
    ASCII only (curses' handler only accepts 32 <= key < 127), and capped
    at a sane length. This also rules out embedded newlines, which would
    otherwise let a crafted username inject extra lines/sections into the
    config file (_modify_config_streamer writes the raw string as a line)."""
    if not name or len(name) > _MAX_USERNAME_LEN:
        return False
    return all(32 <= ord(ch) < 127 for ch in name)


# ══════════════════════════════════════════════════════════════════════════
# Minimal HTML page (polling, not SSE, for v1 — simplest thing that works)
# ══════════════════════════════════════════════════════════════════════════

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>jj-dlp</title>
<link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="jj-dlp">
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, system-ui, sans-serif; background:#111; color:#eee;
         margin:0; padding:16px; padding-bottom:calc(16px + env(safe-area-inset-bottom)); }
  h1 { font-size:1.1rem; opacity:.7; margin:0 0 12px; }
  .site { margin-bottom:20px; }
  .site h2 { font-size:0.95rem; opacity:.8; border-bottom:1px solid #333; padding-bottom:4px; }
  .streamer { display:flex; align-items:center; gap:8px; padding:6px 2px; font-size:0.95rem; }
  .dot { width:10px; height:10px; border-radius:50%; background:#444; flex:none; }
  .dot.live { background:#e5a000; }
  .dot.rec { background:#e33; }
  .name { flex:1; }
  .dur { opacity:.6; font-variant-numeric:tabular-nums; font-size:0.85rem; }
  .actions { display:flex; gap:6px; flex:none; }
  .btn { font-size:0.78rem; padding:4px 9px; border-radius:6px; border:1px solid #444;
         background:#1c1c1c; color:#ddd; }
  .btn:active { background:#2a2a2a; }
  .btn:disabled { opacity:.4; }
  .btn-danger { border-color:#5c2222; color:#e88; }
  .btn-primary { border-color:#3a5c22; color:#9e8; }
  .add-row { display:flex; gap:6px; margin-top:10px; }
  .add-row input { flex:1; font-size:16px; padding:6px 8px; border-radius:6px;
                    border:1px solid #444; background:#1a1a1a; color:#eee; }
  .msg { font-size:0.78rem; margin-top:6px; min-height:1em; }
  .msg.err { color:#e77; }
  .msg.ok { color:#8c8; }
  .log { margin-top:8px; font-size:0.75rem; opacity:.6; max-height:120px; overflow-y:auto;
         white-space:pre-wrap; font-family:ui-monospace, monospace; }
  .stale { color:#e33; }
  @media (min-width: 641px) {
    body { max-width: 760px; margin: 0 auto; }
  }
</style>
</head>
<body>
<h1 id="status">jj-dlp — connecting…</h1>
<div id="sites"></div>
<script>
// Result of the most recent add/remove/disable action per site, so it
// survives the innerHTML rebuild that refresh() does right after firing
// (fetching /api/status doesn't know about it otherwise). Faded out after
// a few seconds so it doesn't linger indefinitely.
const siteMessages = {};

async function postStreamerAction(siteId, username, action, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/streamer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({site_id: siteId, username: username, action: action}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      throw new Error(data.error || data.message || ('HTTP ' + res.status));
    }
    return {ok: true, message: data.message || ''};
  } catch (e) {
    return {ok: false, message: String(e.message || e)};
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function refresh() {
  try {
    const res = await fetch('/api/status', {cache: 'no-store'});
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    document.getElementById('status').textContent =
      'jj-dlp — updated ' + new Date(data.generated_at * 1000).toLocaleTimeString();
    document.getElementById('status').classList.remove('stale');
    const root = document.getElementById('sites');

    // The whole #sites subtree gets rebuilt below, which would otherwise
    // reset every .log div's scroll position, every add-streamer input,
    // and — with a long streamer list — the page's own scroll position
    // on each refresh. Capture that state first (keyed by site id, plus
    // the page scroll) so it can be restored — or, for the log, so a
    // user already at the bottom stays pinned there as new lines arrive.
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    const prevLogState = {};
    for (const logEl of root.querySelectorAll('.log')) {
      prevLogState[logEl.dataset.site] = {
        distanceFromBottom: logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight,
      };
    }
    const prevAddInput = {};
    for (const inputEl of root.querySelectorAll('.add-row input')) {
      if (inputEl.value) prevAddInput[inputEl.dataset.site] = inputEl.value;
    }
    // Same problem as above, but for focus: rebuilding the DOM below
    // destroys and recreates the add-streamer <input> elements, which
    // silently drops keyboard focus (and kicks the on-screen keyboard away
    // on mobile) out of the box a user is actively typing in. Remember
    // which one (if any) was focused, plus the cursor/selection position,
    // so it can be restored after rebuild.
    const active = document.activeElement;
    const activeSite = (active && active.matches && active.matches('.add-row input'))
      ? active.dataset.site : null;
    const activeSelStart = activeSite ? active.selectionStart : null;
    const activeSelEnd = activeSite ? active.selectionEnd : null;

    root.innerHTML = '';
    for (const site of data.sites) {
      const div = document.createElement('div');
      div.className = 'site';
      const h2 = document.createElement('h2');
      h2.textContent = site.label;
      div.appendChild(h2);

      for (const s of site.streamers) {
        const row = document.createElement('div');
        row.className = 'streamer';
        const dot = document.createElement('div');
        dot.className = 'dot' + (s.recording ? ' rec' : (s.live ? ' live' : ''));
        const name = document.createElement('div');
        name.className = 'name';
        name.textContent = s.name + (s.blocked ? ' (disabled)' : '');
        const dur = document.createElement('div');
        dur.className = 'dur';
        if (s.duration_s != null) {
          const m = Math.floor(s.duration_s / 60);
          dur.textContent = (m >= 60 ? Math.floor(m/60)+'h '+(m%60)+'m' : m+'m');
        }

        const actions = document.createElement('div');
        actions.className = 'actions';
        const toggleBtn = document.createElement('button');
        toggleBtn.className = 'btn' + (s.blocked ? ' btn-primary' : '');
        toggleBtn.textContent = s.blocked ? 'Enable' : 'Disable';
        toggleBtn.onclick = async () => {
          const result = await postStreamerAction(site.id, s.name, s.blocked ? 'add' : 'disable', toggleBtn);
          siteMessages[site.id] = {text: result.message, ok: result.ok, ts: Date.now()};
          refresh();
        };
        const removeBtn = document.createElement('button');
        removeBtn.className = 'btn btn-danger';
        removeBtn.textContent = 'Remove';
        removeBtn.onclick = async () => {
          const result = await postStreamerAction(site.id, s.name, 'remove', removeBtn);
          siteMessages[site.id] = {text: result.message, ok: result.ok, ts: Date.now()};
          refresh();
        };
        actions.append(toggleBtn, removeBtn);

        row.append(dot, name, dur, actions);
        div.appendChild(row);
      }

      const addRow = document.createElement('div');
      addRow.className = 'add-row';
      const addInput = document.createElement('input');
      addInput.type = 'text';
      addInput.placeholder = 'add streamer…';
      addInput.dataset.site = site.id;
      addInput.value = prevAddInput[site.id] || '';
      const addBtn = document.createElement('button');
      addBtn.className = 'btn btn-primary';
      addBtn.textContent = 'Add';
      const doAdd = async () => {
        const username = addInput.value.trim();
        if (!username) return;
        const result = await postStreamerAction(site.id, username, 'add', addBtn);
        siteMessages[site.id] = {text: result.message, ok: result.ok, ts: Date.now()};
        if (result.ok) addInput.value = '';
        refresh();
      };
      addBtn.onclick = doAdd;
      addInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doAdd(); });
      addRow.append(addInput, addBtn);
      div.appendChild(addRow);

      if (site.id === activeSite) {
        // Restore focus after the element is attached below.
        queueMicrotask(() => {
          addInput.focus({preventScroll: true});
          try { addInput.setSelectionRange(activeSelStart, activeSelEnd); } catch (_) {}
        });
      }

      const msg = document.createElement('div');
      const sm = siteMessages[site.id];
      if (sm && (Date.now() - sm.ts) < 8000) {
        msg.textContent = sm.text;
        msg.className = 'msg ' + (sm.ok ? 'ok' : 'err');
      } else {
        msg.className = 'msg';
      }
      div.appendChild(msg);

      const log = document.createElement('div');
      log.className = 'log';
      log.dataset.site = site.id;
      log.textContent = site.log.slice(-20).join('\\n');
      div.appendChild(log);
      root.appendChild(div);

      // Default to showing the most recent lines. If the user had scrolled
      // up to read older lines, leave them roughly where they were instead
      // of snapping back to the top (or the bottom) on every refresh.
      const prev = prevLogState[site.id];
      if (!prev || prev.distanceFromBottom <= 4) {
        log.scrollTop = log.scrollHeight;
      } else {
        log.scrollTop = Math.max(0, log.scrollHeight - log.clientHeight - prev.distanceFromBottom);
      }
    }

    // Rebuild is done and the page is back to its full height — put the
    // scroll position back where the user had it.
    window.scrollTo(scrollX, scrollY);
  } catch (e) {
    document.getElementById('status').textContent = 'jj-dlp — connection lost, retrying…';
    document.getElementById('status').classList.add('stale');
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

_MANIFEST_JSON = json.dumps({
    "name": "jj-dlp",
    "short_name": "jj-dlp",
    "display": "standalone",
    "background_color": "#111111",
    "theme_color": "#111111",
    "start_url": "/",
})


# ══════════════════════════════════════════════════════════════════════════
# HTTP handler
# ══════════════════════════════════════════════════════════════════════════

class _Handler(BaseHTTPRequestHandler):
    sites: List = []
    auth_user: str = ""
    auth_pass: str = ""
    # Callback: (config_path: str, username: str, action: str) -> str.
    # Set to main._modify_config_streamer by start_web_server(). Left as
    # None means write endpoints are disabled (defense in depth — should
    # never happen in practice since main.py always passes it).
    modify_streamer_fn: Optional[Callable[[str, str, str], str]] = None

    # Silence default stderr access logging; route through jj-dlp's logger.
    def log_message(self, fmt, *args):
        dbg(f"[WEBUI] {self.address_string()} " + (fmt % args))

    def _check_auth(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pw = decoded.partition(":")
        except Exception:
            return False
        # Constant-time comparisons to avoid timing side-channels.
        user_ok = hmac.compare_digest(user, self.auth_user)
        pw_ok = hmac.compare_digest(pw, self.auth_pass)
        return user_ok and pw_ok

    # ── Session-cookie authentication ───────────────────────────────────
    # Lets a previously-logged-in client bypass the repeated Basic Auth
    # prompt using the long-lived cookie issued after their first login.

    def _session_cookie_value(self) -> Optional[str]:
        header = self.headers.get("Cookie", "")
        for part in header.split(";"):
            part = part.strip()
            if part.startswith(_SESSION_COOKIE + "="):
                return part[len(_SESSION_COOKIE + "="):]
        return None

    def _has_valid_session(self) -> bool:
        sid = self._session_cookie_value()
        if not sid:
            return False
        with _SESSION_LOCK:
            expiry = _SESSIONS.get(sid)
            if expiry is None:
                return False
            if expiry < time.time():
                _SESSIONS.pop(sid, None)
                _save_sessions(force=True)
                return False
            # Sliding expiry — a session stays alive as long as it's used.
            _SESSIONS[sid] = time.time() + _SESSION_TTL
        _save_sessions(force=False)
        return True

    def _set_session_cookie(self) -> str:
        """Mint a fresh session token and return the Set-Cookie header *value*.
        The caller emits it via _send(..., extra_headers=...) so it is written
        after the HTTP status line — send_header() before send_response()
        would buffer it ahead of the status line and produce a malformed
        response (which is what broke the page on Safari/Firefox)."""
        sid = secrets.token_urlsafe(32)
        with _SESSION_LOCK:
            _SESSIONS[sid] = time.time() + _SESSION_TTL
        _save_sessions(force=True)
        return f"{_SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax; " \
               f"Max-Age={_SESSION_TTL}"

    def _authenticated(self) -> bool:
        """Return True if this request may proceed (valid session cookie OR
        correct Basic credentials). Basic Auth remains the only way to obtain
        a session in the first place; the cookie is just the remembered proof."""
        if self._has_valid_session():
            return True
        if self._check_auth():
            return True
        return False

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="jj-dlp"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: Optional[List[tuple]] = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for name, value in extra_headers:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authenticated():
            self._require_auth()
            return

        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            # Auth passed (via a session cookie or a fresh Basic login). If
            # there wasn't already a valid session cookie, mint one now so
            # the browser remembers us for future visits.
            cookie = None
            if not self._has_valid_session():
                cookie = self._set_session_cookie()
            extra = [("Set-Cookie", cookie)] if cookie else None
            self._send(200, _INDEX_HTML.encode("utf-8"),
                       "text/html; charset=utf-8", extra_headers=extra)
        elif self.path == "/manifest.json":
            self._send(200, _MANIFEST_JSON.encode("utf-8"), "application/manifest+json")
        elif self.path == "/api/status":
            try:
                snapshot = _build_status_snapshot(self.sites)
                body = json.dumps(snapshot).encode("utf-8")
                self._send(200, body, "application/json")
            except Exception as e:
                dbg(f"[WEBUI] /api/status error: {type(e).__name__}: {e}")
                self._send(500, b'{"error":"internal error"}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def _json_error(self, status: int, error: str):
        self._send(status, json.dumps({"error": error}).encode("utf-8"), "application/json")

    def do_POST(self):
        if not self._authenticated():
            self._require_auth()
            return

        if self.path != "/api/streamer":
            self._send(404, b"not found", "text/plain")
            return

        if self.modify_streamer_fn is None:
            self._json_error(503, "write operations are not available")
            return

        # Browsers attach cached HTTP Basic Auth credentials to requests
        # automatically whenever the origin matches — including requests
        # triggered by a *different* site the browser happens to have open,
        # not just this one. That makes state-changing endpoints a CSRF
        # target for anyone else on the WiFi who can get the user's browser
        # to submit a POST here. Requiring Origin/Referer (when the browser
        # sends one) to match Host is a cheap check that blocks that case;
        # it does nothing for non-browser clients, but those need the Basic
        # Auth credentials directly anyway.
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin") or self.headers.get("Referer", "")
        if origin:
            origin_host = urllib.parse.urlparse(origin).netloc
            if origin_host and origin_host != host:
                self._json_error(403, "cross-origin request rejected")
                return

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._json_error(400, "invalid request body")
            return

        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json_error(400, "malformed JSON body")
            return

        site_id = str(payload.get("site_id", ""))
        username = str(payload.get("username", "")).strip()
        action = str(payload.get("action", ""))

        if action not in _ALLOWED_ACTIONS:
            self._json_error(400, "action must be one of: " + ", ".join(_ALLOWED_ACTIONS))
            return
        if not _valid_username(username):
            self._json_error(400, "invalid username")
            return

        site = next((s for s in self.sites if s.label == site_id), None)
        if site is None:
            self._json_error(404, "unknown site")
            return

        try:
            with _CONFIG_WRITE_LOCK:
                result = self.modify_streamer_fn(site.config_path, username, action)
        except Exception as e:
            dbg(f"[WEBUI] streamer action error: {type(e).__name__}: {e}")
            self._json_error(500, "internal error")
            return

        # Same follow-up the curses management overlay does after a
        # successful edit: invalidate the cached config and wake the
        # monitor thread so dash_all_streamers/dash_blocked (what
        # /api/status reports) pick up the change promptly.
        site.invalidate_config_cache()
        site.trigger_event.set()
        site.log_line(f"[WebUI {self.client_address[0]}] {action} '{username}': {result}")

        body = json.dumps({"ok": True, "message": result}).encode("utf-8")
        self._send(200, body, "application/json")


# ══════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════

def start_web_server(
    sites: List,
    global_cfg: dict,
    log_fn=None,
    modify_streamer_fn: Optional[Callable[[str, str, str], str]] = None,
) -> Optional[threading.Thread]:
    """Launch the embedded web UI as a daemon thread, if enabled.

    Reads from *global_cfg* (the dict returned by load_global_config()):
        web_ui        – bool, defaults to False (opt-in)
        web_ui_port   – int, defaults to 8765
        web_ui_user   – str
        web_ui_pass   – str

    *log_fn*, if given, is called with a short string for the essential
    startup outcomes (started / refused / bind failure) so they show up in
    the dashboard's Activity Log regardless of debug-tag settings — those
    are easy to silence and shouldn't be the only place a user can learn
    the web UI didn't come up. Pass main()'s _dash_log helper here.

    *modify_streamer_fn*, if given, enables the add/remove/disable-streamer
    endpoint (POST /api/streamer). Pass main()'s _modify_config_streamer
    here. If omitted, /api/streamer responds 503 and the dashboard stays
    read-only, same as v1.

    If web_ui is False, or web_ui_user/web_ui_pass are not both set,
    the server does not start (auth is mandatory, not optional).
    Returns the started Thread, or None if the server was not started.
    """
    def _announce(msg: str) -> None:
        dbg(f"[WEBUI] {msg}")
        if log_fn is not None:
            try:
                log_fn(f"[WEBUI] {msg}")
            except Exception:
                pass

    if not global_cfg.get("web_ui", False):
        return None

    user = global_cfg.get("web_ui_user", "").strip()
    pw = global_cfg.get("web_ui_pass", "").strip()
    if not user or not pw:
        _announce("WEB_UI is enabled but WEB_UI_USER/WEB_UI_PASS are not both "
                   "set in global.conf — refusing to start server without auth.")
        return None

    port = global_cfg.get("web_ui_port", 8765)

    # Reload the persisted session store so clients who logged in before a
    # restart keep their cookie and don't need to enter credentials again.
    try:
        _load_sessions()
    except Exception as e:
        _announce(f"failed to load session cache: {type(e).__name__}: {e}")

    handler_cls = type("_ConfiguredHandler", (_Handler,), {
        "sites": sites,
        "auth_user": user,
        "auth_pass": pw,
        "modify_streamer_fn": staticmethod(modify_streamer_fn) if modify_streamer_fn else None,
    })

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    except OSError as e:
        _announce(f"failed to bind port {port}: {e}")
        return None

    def _run():
        _announce(f"listening on http://0.0.0.0:{port} "
                   f"(also reachable via http://127.0.0.1:{port})")
        try:
            httpd.serve_forever()
        except Exception as e:
            _announce(f"server thread crashed: {type(e).__name__}: {e}")

    thread = threading.Thread(target=_run, daemon=True, name="webui-http")
    thread.start()
    return thread
