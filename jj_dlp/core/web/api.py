"""Status snapshot and write endpoints."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict

from jj_dlp.core.config import priority as priority_config
from jj_dlp.core.config import sites as sites_config
from jj_dlp.core.config import state as state_config
from jj_dlp.core.engine.app_state import AppState
from jj_dlp.core.engine.site_state import StreamerSnapshot


def _offline_streamer_dict() -> Dict[str, Any]:
    """Default status dict for a configured streamer with no runtime data yet."""
    return {
        "recording": False,
        "in_progress_path": None,
        "last_live": None,
        "live_since": None,
        "stall_since": None,
        "ffmpeg_error_count": 0,
        "ad_alert_active": False,
        "write_failure": False,
    }


def _streamer_dict(snap: StreamerSnapshot) -> Dict[str, Any]:
    """Convert a StreamerSnapshot into a JSON-serializable dict."""
    return {
        "recording": snap.recording,
        "in_progress_path": snap.in_progress_path,
        "last_live": snap.last_live,
        "live_since": snap.live_since,
        "stall_since": snap.stall_since,
        "ffmpeg_error_count": snap.ffmpeg_error_count,
        "ad_alert_active": snap.ad_alert_active,
        "write_failure": snap.write_failure,
    }


def _site_status(app_state: AppState, data_dir: Path, label: str) -> Dict[str, Any]:
    """Build one site's status dict, covering every configured streamer."""
    config = sites_config.load_site(data_dir, label)
    site_state = app_state.get_site_state(label)
    snapshot = site_state.snapshot() if site_state is not None else None

    names = set(config.get("streamers", [])) | set(config.get("disabled", []))
    if snapshot is not None:
        names |= set(snapshot.streamers)

    streamers: Dict[str, Any] = {}
    for name in sorted(names):
        if snapshot is not None and name in snapshot.streamers:
            streamers[name] = _streamer_dict(snapshot.streamers[name])
        else:
            streamers[name] = _offline_streamer_dict()

    return {
        "plugin": config.get("plugin"),
        "disabled": sorted(config.get("disabled", [])),
        "streamers": streamers,
    }


def build_status_snapshot(app_state: AppState, data_dir: Path) -> Dict[str, Any]:
    """Build a read-only status snapshot of every loaded site's streamers."""
    return {
        "sites": {
            label: _site_status(app_state, data_dir, label)
            for label in app_state.loaded_sites
        }
    }


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    """Send a JSON response body with the given HTTP status code."""
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _write_error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    """Send a JSON error response body with the given HTTP status code."""
    _write_json(handler, status, {"error": message})


def handle_status(handler: BaseHTTPRequestHandler, app_state: AppState, data_dir: Path) -> None:
    """Write a GET /api/status JSON response from the current status snapshot."""
    _write_json(handler, 200, build_status_snapshot(app_state, data_dir))


def _read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    """Read and parse a request's JSON body; raises ValueError if missing or malformed."""
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        raise ValueError("Request body is required")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Request body must be a JSON object")
    return parsed


def _require_site_and_streamer(body: Dict[str, Any]) -> tuple:
    """Extract non-empty 'site'/'streamer' strings from a parsed body, or raise ValueError."""
    site = body.get("site")
    streamer = body.get("streamer")
    if not isinstance(site, str) or not site:
        raise ValueError("'site' is required")
    if not isinstance(streamer, str) or not streamer:
        raise ValueError("'streamer' is required")
    return site, streamer


def add_streamer(data_dir: Path, site: str, streamer: str) -> None:
    """Append a streamer to a site's list, matching the curses Add Streamer flow (Doc 1 §17)."""
    if site not in sites_config.list_sites(data_dir):
        raise KeyError(f"No such site: {site}")
    config = sites_config.load_site(data_dir, site)
    existing = list(config.get("streamers", [])) + list(config.get("disabled", []))
    if streamer in existing:
        raise ValueError(f"'{streamer}' is already on {site}")
    config.setdefault("streamers", []).append(streamer)
    sites_config.save_site(data_dir, site, config)


def remove_streamer(data_dir: Path, site: str, streamer: str) -> None:
    """Remove a streamer and its priority/live-status references, matching Doc 1 §17."""
    if site not in sites_config.list_sites(data_dir):
        raise KeyError(f"No such site: {site}")
    config = sites_config.load_site(data_dir, site)
    streamers = config.get("streamers", [])
    if streamer not in streamers:
        raise KeyError(f"'{streamer}' is not an active streamer on {site}")
    streamers.remove(streamer)
    sites_config.save_site(data_dir, site, config)
    priority_config.remove_entry(data_dir, site, streamer)
    state_config.remove_streamer_live_status(data_dir, site, streamer)


def disable_streamer(data_dir: Path, site: str, streamer: str) -> bool:
    """Toggle a streamer between a site's streamers/disabled lists; returns the new enabled state."""
    if site not in sites_config.list_sites(data_dir):
        raise KeyError(f"No such site: {site}")
    config = sites_config.load_site(data_dir, site)
    streamers = config.setdefault("streamers", [])
    disabled = config.setdefault("disabled", [])
    if streamer in disabled:
        disabled.remove(streamer)
        streamers.append(streamer)
        now_enabled = True
    elif streamer in streamers:
        streamers.remove(streamer)
        disabled.append(streamer)
        now_enabled = False
    else:
        raise KeyError(f"'{streamer}' is not on {site}")
    sites_config.save_site(data_dir, site, config)
    return now_enabled


def handle_add_streamer(handler: BaseHTTPRequestHandler, app_state: AppState, data_dir: Path) -> None:
    """Handle POST /api/streamers/add: append a new streamer to a site."""
    try:
        site, streamer = _require_site_and_streamer(_read_json_body(handler))
        add_streamer(data_dir, site, streamer)
    except (ValueError, json.JSONDecodeError) as exc:
        _write_error(handler, 400, str(exc))
        return
    except KeyError as exc:
        _write_error(handler, 404, exc.args[0])
        return
    _write_json(handler, 200, {"site": site, "streamer": streamer, "added": True})


def handle_remove_streamer(handler: BaseHTTPRequestHandler, app_state: AppState, data_dir: Path) -> None:
    """Handle POST /api/streamers/remove: remove a streamer and its cross-file references."""
    try:
        site, streamer = _require_site_and_streamer(_read_json_body(handler))
        remove_streamer(data_dir, site, streamer)
    except (ValueError, json.JSONDecodeError) as exc:
        _write_error(handler, 400, str(exc))
        return
    except KeyError as exc:
        _write_error(handler, 404, exc.args[0])
        return
    _write_json(handler, 200, {"site": site, "streamer": streamer, "removed": True})


def handle_disable_streamer(handler: BaseHTTPRequestHandler, app_state: AppState, data_dir: Path) -> None:
    """Handle POST /api/streamers/disable: toggle a streamer between enabled and disabled."""
    try:
        site, streamer = _require_site_and_streamer(_read_json_body(handler))
        now_enabled = disable_streamer(data_dir, site, streamer)
    except (ValueError, json.JSONDecodeError) as exc:
        _write_error(handler, 400, str(exc))
        return
    except KeyError as exc:
        _write_error(handler, 404, exc.args[0])
        return
    _write_json(handler, 200, {"site": site, "streamer": streamer, "enabled": now_enabled})
