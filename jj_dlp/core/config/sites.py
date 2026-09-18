"""CRUD and plugin dispatch for config/sites/<label>.json."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, List, Optional

from jj_dlp.core.config import schema, storage
from jj_dlp.core.plugins import get_plugin

log = logging.getLogger("jj_dlp.config.sites")

SITES_RELDIR = Path("config") / "sites"
PRIORITY_RELPATH = Path("config") / "priority.json"
LIVE_STATUS_RELPATH = Path("state") / "live_status.json"
EVENTSUB_RELPATH = Path("state") / "eventsub.json"

LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")

REQUIRED_KEYS = {
    "schema_version", "label", "plugin", "order", "url_template",
    "streamers", "disabled", "blocked", "timing", "output", "checker",
    "downloader", "lq_downloader", "notifications", "display", "browser",
    "upgrade_quality", "plugin_settings",
}

_TYPE_MAP = {"bool": bool, "int": int, "float": (int, float), "str": str}
_MISSING = object()


def _sites_dir(data_dir: Path) -> Path:
    """Return the config/sites/ directory under a data directory."""
    return Path(data_dir) / SITES_RELDIR


def _site_path(data_dir: Path, label: str) -> Path:
    """Return the full path to a site's config file."""
    return _sites_dir(data_dir) / f"{label}.json"


def list_sites(data_dir: Path) -> List[str]:
    """Return every site label found under config/sites/, sorted."""
    d = _sites_dir(data_dir)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def _get_nested(d: dict, dotted_path: str) -> Any:
    """Return the value at a dotted path inside a nested dict, or _MISSING."""
    cur: Any = d
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _validate_structure(data: Any) -> bool:
    """Check that data is a dict containing every required top-level site key."""
    return isinstance(data, dict) and REQUIRED_KEYS.issubset(data.keys())


def _validate_fields(data: dict, plugin_id: Optional[str], schema_path: Optional[Path]) -> bool:
    """Loosely check known site/plugin fields have the right Python type where present."""
    plugin_id = plugin_id or data.get("plugin")
    field_defs = list(schema.get_site_fields(schema_path))
    if plugin_id:
        field_defs += schema.get_plugin_fields(plugin_id, schema_path)
    for f in field_defs:
        value = _get_nested(data, f["path"])
        if value is _MISSING:
            continue
        if f["type"] == "list[str]":
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                return False
        elif f["type"] == "enum":
            continue
        else:
            expected = _TYPE_MAP.get(f["type"])
            if expected is not None and not isinstance(value, expected):
                return False
    return True


def validate_site(data: Any, plugin_id: Optional[str] = None, schema_path: Optional[Path] = None) -> bool:
    """Validate a site dict's structure and known field types."""
    if not _validate_structure(data):
        return False
    return _validate_fields(data, plugin_id, schema_path)


def _guess_plugin_from_raw(path: Path) -> Optional[str]:
    """Best-effort scrape of a 'plugin' value from an unparsable site file."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(r'"plugin"\s*:\s*"([A-Za-z0-9_-]+)"', text)
    return m.group(1) if m else None


def load_site(data_dir: Path, label: str, schema_path: Optional[Path] = None) -> dict:
    """Load one site's config, recovering via backups then plugin defaults on failure."""
    data_dir = Path(data_dir)
    path = _site_path(data_dir, label)

    def _fallback_defaults() -> dict:
        plugin_id = _guess_plugin_from_raw(path) or "chatsite"
        try:
            plugin = get_plugin(plugin_id)
        except KeyError:
            plugin = get_plugin("chatsite")
        cfg = plugin.default_config()
        cfg["label"] = label
        return cfg

    return storage.load_config(
        path,
        _fallback_defaults,
        lambda d: validate_site(d, schema_path=schema_path),
    )


def save_site(data_dir: Path, label: str, data: dict) -> None:
    """Atomically save a site's config, rotating backups first."""
    storage.save_config(_site_path(Path(data_dir), label), data)


def _validate_label(label: str) -> None:
    """Raise ValueError if label is empty or not filesystem-safe."""
    if not label or not LABEL_RE.match(label):
        raise ValueError(f"Invalid site label: {label!r}")


def create_site(data_dir: Path, label: str, plugin_id: str) -> dict:
    """Create a new site from a plugin's defaults and save it under this label."""
    data_dir = Path(data_dir)
    _validate_label(label)
    if label in list_sites(data_dir):
        log.warning("Rejected create-site '%s': label already exists", label)
        raise ValueError(f"A site named '{label}' already exists")
    plugin = get_plugin(plugin_id)
    cfg = plugin.default_config()
    cfg["label"] = label
    cfg["plugin"] = plugin_id
    save_site(data_dir, label, cfg)
    log.info("Created site '%s' (plugin=%s)", label, plugin_id)
    return cfg


def rename_site(data_dir: Path, old_label: str, new_label: str) -> None:
    """Rename a site's file and every reference to it in priority/state data."""
    data_dir = Path(data_dir)
    if old_label == new_label:
        return
    if old_label not in list_sites(data_dir):
        log.warning("Rejected rename-site '%s' -> '%s': no such site", old_label, new_label)
        raise ValueError(f"No such site: {old_label!r}")
    if new_label in list_sites(data_dir):
        log.warning("Rejected rename-site '%s' -> '%s': label already exists", old_label, new_label)
        raise ValueError(f"A site named '{new_label}' already exists")
    _validate_label(new_label)

    data = load_site(data_dir, old_label)
    data["label"] = new_label
    save_site(data_dir, new_label, data)
    _site_path(data_dir, old_label).unlink(missing_ok=True)

    _rename_priority_references(data_dir, old_label, new_label)
    _rename_state_references(data_dir, old_label, new_label)
    log.info("Renamed site '%s' to '%s'", old_label, new_label)


def delete_site(data_dir: Path, label: str) -> None:
    """Delete a site's file and every reference to it in priority/state data."""
    data_dir = Path(data_dir)
    _site_path(data_dir, label).unlink(missing_ok=True)
    _remove_priority_references(data_dir, label)
    _remove_state_references(data_dir, label)
    log.info("Deleted site '%s'", label)


def add_streamer(data_dir: Path, label: str, streamer: str) -> None:
    """Append a streamer to a site's streamers list, rejecting empty or duplicate names."""
    data_dir = Path(data_dir)
    site = load_site(data_dir, label)
    existing = list(site.get("streamers", [])) + list(site.get("disabled", []))
    if not streamer:
        log.warning("Rejected add-streamer on site '%s': empty name", label)
        raise ValueError("Streamer name cannot be empty.")
    if streamer in existing:
        log.warning("Rejected add-streamer '%s' on site '%s': already present", streamer, label)
        raise ValueError(f"'{streamer}' is already on {label}.")
    site.setdefault("streamers", []).append(streamer)
    save_site(data_dir, label, site)
    log.info("Added streamer '%s' to site '%s'", streamer, label)


# --- priority.json / state/*.json reference cleanup ---
# core/config/priority.py (Step 1.7) and state.py (Step 1.8) own these files
# going forward; the raw shapes touched here match Doc 1 §3.5 and §3.8.

def _default_priority() -> dict:
    """Return an empty config/priority.json structure."""
    return {"schema_version": 1, "entries": []}


def _load_priority(data_dir: Path) -> dict:
    """Load config/priority.json, defaulting to an empty entry list."""
    return storage.load_config(Path(data_dir) / PRIORITY_RELPATH, _default_priority)


def _save_priority(data_dir: Path, data: dict) -> None:
    """Atomically save config/priority.json."""
    storage.save_config(Path(data_dir) / PRIORITY_RELPATH, data)


def _rename_priority_references(data_dir: Path, old_label: str, new_label: str) -> None:
    """Point every priority.json entry for old_label at new_label."""
    data = _load_priority(data_dir)
    changed = False
    for entry in data.get("entries", []):
        if entry.get("site") == old_label:
            entry["site"] = new_label
            changed = True
    if changed:
        _save_priority(data_dir, data)


def _remove_priority_references(data_dir: Path, label: str) -> None:
    """Drop every priority.json entry belonging to a deleted site."""
    data = _load_priority(data_dir)
    entries = data.get("entries", [])
    kept = [e for e in entries if e.get("site") != label]
    if len(kept) != len(entries):
        data["entries"] = kept
        _save_priority(data_dir, data)


def _state_paths(data_dir: Path) -> List[Path]:
    """Return the state/*.json files that key data by site label."""
    return [Path(data_dir) / LIVE_STATUS_RELPATH, Path(data_dir) / EVENTSUB_RELPATH]


def _rename_state_references(data_dir: Path, old_label: str, new_label: str) -> None:
    """Move each state file's old_label key to new_label, if present."""
    for path in _state_paths(data_dir):
        data = storage.load_state(path, dict)
        if isinstance(data, dict) and old_label in data:
            data[new_label] = data.pop(old_label)
            storage.save_state(path, data)


def _remove_state_references(data_dir: Path, label: str) -> None:
    """Drop a deleted site's key from each state file, if present."""
    for path in _state_paths(data_dir):
        data = storage.load_state(path, dict)
        if isinstance(data, dict) and label in data:
            data.pop(label, None)
            storage.save_state(path, data)
