"""Loader and validator for schema/fields.json."""

import json
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional

from jj_dlp.core.config import storage

_SHIPPED_PACKAGE = "jj_dlp.core.config"
_SHIPPED_NAME = "shipped_fields.json"

_cache: Dict[str, Any] = {"path": None, "data": None}


def load_shipped_defaults() -> dict:
    """Return the app's bundled default schema/fields.json content."""
    ref = resources.files(_SHIPPED_PACKAGE).joinpath(_SHIPPED_NAME)
    with ref.open("r", encoding="utf-8") as f:
        return json.load(f)


def _validate(data: Any) -> bool:
    """Check that loaded data has the required top-level schema shape."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("app_fields"), list)
        and isinstance(data.get("site_fields"), list)
        and isinstance(data.get("plugin_fields"), dict)
    )


def load(schema_path: Path) -> dict:
    """Load schema/fields.json (with backup/shipped-default fallback) and cache it."""
    data = storage.load_config(Path(schema_path), load_shipped_defaults, _validate)
    _cache["path"] = Path(schema_path)
    _cache["data"] = data
    return data


def _data(schema_path: Optional[Path]) -> dict:
    """Return the cached schema, loading first if a new path is given."""
    if schema_path is not None and (_cache["data"] is None or Path(schema_path) != _cache["path"]):
        return load(schema_path)
    if _cache["data"] is None:
        raise RuntimeError("schema.load(path) must be called before use")
    return _cache["data"]


def get_app_fields(schema_path: Optional[Path] = None) -> List[dict]:
    """Return the list of app-level field definitions."""
    return _data(schema_path)["app_fields"]


def get_site_fields(schema_path: Optional[Path] = None) -> List[dict]:
    """Return the list of site-level field definitions."""
    return _data(schema_path)["site_fields"]


def get_plugin_fields(plugin_id: str, schema_path: Optional[Path] = None) -> List[dict]:
    """Return the field definitions contributed by one plugin, or [] if none."""
    return _data(schema_path).get("plugin_fields", {}).get(plugin_id, [])


def _all_fields(schema_path: Optional[Path]) -> List[dict]:
    """Return every field definition across app, site, and plugin field lists."""
    data = _data(schema_path)
    fields = list(data["app_fields"]) + list(data["site_fields"])
    for plugin_fields in data.get("plugin_fields", {}).values():
        fields.extend(plugin_fields)
    return fields


def is_preserved(path: str, schema_path: Optional[Path] = None) -> bool:
    """Return whether the field at this dotted path is marked preserve: true."""
    for field in _all_fields(schema_path):
        if field["path"] == path:
            return bool(field.get("preserve", False))
    return False


def get_yt_dlp_flag(path: str, schema_path: Optional[Path] = None) -> Optional[str]:
    """Return the yt-dlp CLI flag mapped to this dotted field path, if any."""
    for field in _all_fields(schema_path):
        if field["path"] == path:
            return field.get("yt_dlp_flag")
    return None
