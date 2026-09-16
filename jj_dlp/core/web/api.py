"""Status snapshot and write endpoints."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict

from jj_dlp.core.config import sites as sites_config
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


def handle_status(handler: BaseHTTPRequestHandler, app_state: AppState, data_dir: Path) -> None:
    """Write a GET /api/status JSON response from the current status snapshot."""
    payload = build_status_snapshot(app_state, data_dir)
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
