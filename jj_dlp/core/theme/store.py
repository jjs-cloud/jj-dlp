"""Load/save config/theme.json and active theme selection."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, List, Optional

from jj_dlp.core.config import storage

THEME_RELPATH = Path("config") / "theme.json"

# Self-contained fallback used only if config/theme.json is missing/corrupt
# with no valid backup. The full three-preset set is presets.py's job (§10.5).
_FALLBACK_THEME = {
    "id": "dark",
    "name": "Dark",
    "rgb_mode_colors": {
        "chrome": {"fg": "#d0d0d0", "bg": "#000000"},
        "highlight": {"fg": "#5fd7ff", "bg": "#000000"},
        "recording": {"fg": "#ff5f5f", "bg": "#000000"},
        "warning": {"fg": "#ffd75f", "bg": "#000000"},
        "muted": {"fg": "#6c6c6c", "bg": "#000000"},
        "accent": {"fg": "#d787ff", "bg": "#000000"},
    },
    "roles": {
        "chrome": {"fg": "white", "bg": "black", "bold": False},
        "highlight": {"fg": "cyan", "bg": "black", "bold": True},
        "recording": {"fg": "red", "bg": "black", "bold": True},
        "warning": {"fg": "yellow", "bg": "black", "bold": True},
        "muted": {"fg": "black", "bg": "black", "bold": True},
        "accent": {"fg": "magenta", "bg": "black", "bold": False},
    },
    "elements": {
        "dashboard.system_panel.border": {"role": "chrome"},
        "dashboard.streamer.recording_dot": {"role": "recording"},
    },
}


def _default_data() -> dict:
    """Return a fresh config/theme.json structure with just the fallback theme."""
    return {"schema_version": 1, "active_theme": "dark", "themes": [copy.deepcopy(_FALLBACK_THEME)]}


def _validate(data: Any) -> bool:
    """Check that loaded data has the required top-level theme.json shape."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("active_theme"), str)
        and isinstance(data.get("themes"), list)
    )


def _path(data_dir: Path) -> Path:
    """Return the full path to config/theme.json under a data directory."""
    return Path(data_dir) / THEME_RELPATH


def _load(data_dir: Path) -> dict:
    """Load config/theme.json, recovering via backups then the fallback default."""
    return storage.load_config(_path(data_dir), _default_data, _validate)


def _save(data_dir: Path, data: dict) -> None:
    """Atomically save config/theme.json, rotating backups first."""
    storage.save_config(_path(data_dir), data)


def _find(data: dict, theme_id: str) -> Optional[dict]:
    """Return the theme dict matching theme_id, or None."""
    for theme in data["themes"]:
        if theme.get("id") == theme_id:
            return theme
    return None


def list_themes(data_dir: Path) -> List[dict]:
    """Return every theme in config/theme.json."""
    return _load(data_dir)["themes"]


def get_theme(data_dir: Path, theme_id: str) -> Optional[dict]:
    """Return one theme by id, or None if it doesn't exist."""
    return _find(_load(data_dir), theme_id)


def get_active_theme(data_dir: Path) -> dict:
    """Return the full theme dict for the currently active theme."""
    data = _load(data_dir)
    theme = _find(data, data["active_theme"])
    if theme is not None:
        return theme
    # Active theme id is dangling (e.g. it was deleted out of band); fall back.
    return data["themes"][0] if data["themes"] else copy.deepcopy(_FALLBACK_THEME)


def set_active_theme(data_dir: Path, theme_id: str) -> None:
    """Set the active theme by id. Raises ValueError if that id doesn't exist."""
    data = _load(data_dir)
    if _find(data, theme_id) is None:
        raise ValueError(f"Unknown theme id: {theme_id!r}")
    data["active_theme"] = theme_id
    _save(data_dir, data)


def save_theme(data_dir: Path, theme: dict) -> None:
    """Create or update a theme by its id (upsert)."""
    data = _load(data_dir)
    existing = _find(data, theme.get("id"))
    if existing is not None:
        data["themes"][data["themes"].index(existing)] = theme
    else:
        data["themes"].append(theme)
    _save(data_dir, data)


def delete_theme(data_dir: Path, theme_id: str) -> None:
    """Delete a theme by id. Raises ValueError if it's the only remaining theme."""
    data = _load(data_dir)
    theme = _find(data, theme_id)
    if theme is None:
        return
    if len(data["themes"]) <= 1:
        raise ValueError("Cannot delete the only remaining theme.")
    data["themes"].remove(theme)
    if data["active_theme"] == theme_id:
        data["active_theme"] = data["themes"][0]["id"]
    _save(data_dir, data)
