"""Registry of themeable UI elements and each one's default role."""

from __future__ import annotations

from typing import Dict, List

# element id -> default role name (Doc 1 §10.2's minimum starting set).
_DEFAULT_ROLES: Dict[str, str] = {
    "dashboard.system_panel.border": "chrome",
    "dashboard.system_panel.text": "chrome",
    "dashboard.graph.bar_low": "highlight",
    "dashboard.graph.bar_high": "accent",
    "dashboard.site_panel.border": "chrome",
    "dashboard.site_panel.title": "highlight",
    "dashboard.streamer.row_offline": "muted",
    "dashboard.streamer.row_live": "chrome",
    "dashboard.streamer.row_recording": "recording",
    "dashboard.streamer.recording_dot": "recording",
    "dashboard.streamer.last_live_highlight": "accent",
    "tabs.bar.active": "highlight",
    "tabs.bar.inactive": "muted",
    "footer.hint_key": "accent",
    "footer.hint_text": "chrome",
    "log.line_default": "chrome",
    "log.line_error": "warning",
    "priority.row_normal": "chrome",
    "priority.row_bypass": "accent",
    "priority.override_marker": "warning",
    "config.field_label": "chrome",
    "config.field_value": "highlight",
    "config.field_modified": "warning",
    "file_manager.row_writing": "recording",
    "file_manager.row_idle": "chrome",
    "file_manager.folder_header": "highlight",
    "file_manager.delete_confirm": "warning",
    "popup.border": "chrome",
    "popup.title": "highlight",
    "popup.button_focused": "accent",
    "eventsub.status_ok": "highlight",
    "eventsub.status_error": "warning",
}


def get_elements() -> List[str]:
    """Return every registered element id, in registry order."""
    return list(_DEFAULT_ROLES.keys())


def get_default_role(element_id: str) -> str:
    """Return an element's default role name. Raises KeyError if unknown."""
    return _DEFAULT_ROLES[element_id]


def get_default_elements_map() -> Dict[str, Dict[str, str]]:
    """Return a fresh {element_id: {"role": default_role}} map for seeding a theme."""
    return {element_id: {"role": role} for element_id, role in _DEFAULT_ROLES.items()}
