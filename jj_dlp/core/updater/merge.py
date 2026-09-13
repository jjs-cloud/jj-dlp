"""Schema-driven config merge using fields.json's preserve flag (Doc 1 §9.3)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

from jj_dlp.core.config import app as app_config
from jj_dlp.core.config import schema
from jj_dlp.core.config import sites as sites_config
from jj_dlp.core.notify import logger
from jj_dlp.core.plugins import get_plugin

_MISSING = object()

# Top-level/dotted keys with no schema/fields.json entry; carried over verbatim.
APP_CARRY_OVER_PATHS = ["output.destinations"]
SITE_CARRY_OVER_KEYS = ["label", "plugin", "order", "streamers", "disabled", "blocked"]


def _get_nested(d: dict, dotted_path: str) -> Any:
    """Return the value at a dotted path inside a nested dict, or _MISSING if absent."""
    cur: Any = d
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _set_nested(d: dict, dotted_path: str, value: Any) -> None:
    """Set a value at a dotted path inside a nested dict, creating groups as needed."""
    parts = dotted_path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def merge_fields(old_data: dict, new_defaults: dict, field_defs: List[dict]) -> dict:
    """Overlay old_data's preserve:true field values onto a copy of new_defaults."""
    merged = copy.deepcopy(new_defaults)
    for f in field_defs:
        if not f.get("preserve"):
            continue
        old_value = _get_nested(old_data, f["path"])
        if old_value is not _MISSING:
            _set_nested(merged, f["path"], copy.deepcopy(old_value))
    return merged


def _carry_over(old_data: dict, merged: dict, paths: List[str]) -> None:
    """Copy each dotted path's value from old_data onto merged in place, if present."""
    for path in paths:
        value = _get_nested(old_data, path)
        if value is not _MISSING:
            _set_nested(merged, path, copy.deepcopy(value))


def merge_app_config(data_dir: Path, schema_path: Optional[Path] = None) -> dict:
    """Merge the existing app.json onto fresh shipped defaults; returns the merged dict."""
    data_dir = Path(data_dir)
    schema_path = Path(schema_path) if schema_path else data_dir / "schema" / "fields.json"
    old_data = app_config.load(data_dir, schema_path).to_dict()
    new_defaults = app_config.get_default(schema_path).to_dict()
    merged = merge_fields(old_data, new_defaults, schema.get_app_fields(schema_path))
    _carry_over(old_data, merged, APP_CARRY_OVER_PATHS)
    return merged


def merge_site_config(data_dir: Path, label: str, schema_path: Optional[Path] = None) -> dict:
    """Merge one site's existing config onto its plugin's fresh defaults; returns the merged dict."""
    data_dir = Path(data_dir)
    schema_path = Path(schema_path) if schema_path else data_dir / "schema" / "fields.json"
    old_data = sites_config.load_site(data_dir, label, schema_path)
    plugin_id = old_data.get("plugin")
    try:
        plugin = get_plugin(plugin_id)
    except KeyError:
        logger.dbg(f"unknown plugin '{plugin_id}' for site '{label}'; skipping merge", tag="updater")
        return old_data

    new_defaults = plugin.default_config()
    field_defs = list(schema.get_site_fields(schema_path)) + list(
        schema.get_plugin_fields(plugin_id, schema_path)
    )
    merged = merge_fields(old_data, new_defaults, field_defs)
    _carry_over(old_data, merged, SITE_CARRY_OVER_KEYS)
    return merged


def merge_all_site_configs(data_dir: Path, schema_path: Optional[Path] = None) -> Dict[str, dict]:
    """Merge every site's config file; returns {label: merged_dict}."""
    data_dir = Path(data_dir)
    return {
        label: merge_site_config(data_dir, label, schema_path)
        for label in sites_config.list_sites(data_dir)
    }


def apply_merge(data_dir: Path, schema_path: Optional[Path] = None) -> None:
    """Merge and save app.json plus every site file. schema/fields.json is never merged."""
    data_dir = Path(data_dir)
    merged_app = merge_app_config(data_dir, schema_path)
    app_config.save(data_dir, app_config.AppConfig.from_dict(merged_app))
    for label, merged_site in merge_all_site_configs(data_dir, schema_path).items():
        sites_config.save_site(data_dir, label, merged_site)
