"""Web server startup, gated on app.json's web_ui.enabled."""

from __future__ import annotations

import logging
from typing import Optional

from jj_dlp.core.config import app as app_config
from jj_dlp.core.engine.app_state import AppState
from jj_dlp.core.web.server import WebServer, build_server

log = logging.getLogger("jj_dlp.web")


def start_web_server(app_state: AppState) -> Optional[WebServer]:
    """Start the web UI if web_ui.enabled is true, returning the running server (or None)."""
    config = app_config.load(app_state.data_dir)
    web_ui = config.get_web_ui()
    if not web_ui.enabled:
        log.info("Web UI disabled; not starting.")
        return None
    server = build_server(web_ui, app_state, app_state.data_dir)
    server.start()
    log.info("Web UI reachable at %s", server.url)
    return server


def stop_web_server(server: Optional[WebServer]) -> None:
    """Stop a previously started web server, if any."""
    if server is not None:
        server.stop()
