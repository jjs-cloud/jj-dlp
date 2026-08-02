"""
http_server.py — Embedded, read-only web dashboard for jj-dlp.

Design (v1 — read-only):
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

Write operations (add/remove/disable streamer, priority reorder, config
edit) are intentionally NOT implemented yet. This module should only ever
*read* SiteState under its locks; it must never call into config-writing
code until v2.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

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

        out_sites.append({
            "label": site.label,
            "streamers": streamers,
            "log": log_lines,
        })

    return {"generated_at": now, "sites": out_sites}


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
  .log { margin-top:8px; font-size:0.75rem; opacity:.6; max-height:120px; overflow-y:auto;
         white-space:pre-wrap; font-family:ui-monospace, monospace; }
  .stale { color:#e33; }
</style>
</head>
<body>
<h1 id="status">jj-dlp — connecting…</h1>
<div id="sites"></div>
<script>
async function refresh() {
  try {
    const res = await fetch('/api/status', {cache: 'no-store'});
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    document.getElementById('status').textContent =
      'jj-dlp — updated ' + new Date(data.generated_at * 1000).toLocaleTimeString();
    document.getElementById('status').classList.remove('stale');
    const root = document.getElementById('sites');
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
        name.textContent = s.name + (s.blocked ? ' (blocked)' : '');
        const dur = document.createElement('div');
        dur.className = 'dur';
        if (s.duration_s != null) {
          const m = Math.floor(s.duration_s / 60);
          dur.textContent = (m >= 60 ? Math.floor(m/60)+'h '+(m%60)+'m' : m+'m');
        }
        row.append(dot, name, dur);
        div.appendChild(row);
      }
      const log = document.createElement('div');
      log.className = 'log';
      log.textContent = site.log.slice(-20).join('\\n');
      div.appendChild(log);
      root.appendChild(div);
    }
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

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="jj-dlp"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_auth():
            self._require_auth()
            return

        if self.path == "/" or self.path == "/index.html":
            self._send(200, _INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
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


# ══════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════

def start_web_server(sites: List, global_cfg: dict, log_fn=None) -> Optional[threading.Thread]:
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

    handler_cls = type("_ConfiguredHandler", (_Handler,), {
        "sites": sites,
        "auth_user": user,
        "auth_pass": pw,
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
