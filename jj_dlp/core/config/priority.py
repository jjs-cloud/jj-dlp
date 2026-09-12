"""Wrapper for config/priority.json: order, bypass, and per-streamer overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from jj_dlp.core.config import storage

PRIORITY_RELPATH = Path("config") / "priority.json"

OVERRIDE_FIELDS = (
    "quality",
    "notifications",
    "auto_suffix",
    "output_dir",
    "split",
    "intro_delay",
    "schedule",
)

PriorityEntry = Dict[str, Any]


def _default_overrides() -> Dict[str, Any]:
    """Return a fresh all-null overrides map (Doc 1 §3.5)."""
    return {field: None for field in OVERRIDE_FIELDS}


def _default_data() -> dict:
    """Return an empty config/priority.json structure."""
    return {"schema_version": 1, "entries": []}


def _validate(data: Any) -> bool:
    """Check that loaded data has the required top-level shape."""
    return isinstance(data, dict) and isinstance(data.get("entries"), list)


def _path(data_dir: Path) -> Path:
    """Return the full path to config/priority.json under a data directory."""
    return Path(data_dir) / PRIORITY_RELPATH


def _load(data_dir: Path) -> dict:
    """Load config/priority.json, recovering via backups then an empty default."""
    return storage.load_config(_path(data_dir), _default_data, _validate)


def _save(data_dir: Path, data: dict) -> None:
    """Atomically save config/priority.json, rotating backups first."""
    storage.save_config(_path(data_dir), data)


def _sort_key(entry: PriorityEntry):
    """Sort key: bypass entries first, then by position."""
    return (0 if entry.get("bypass") else 1, entry.get("position", 0))


def _find_entry(data: dict, site: str, streamer: str) -> Optional[PriorityEntry]:
    """Return the matching entry dict from loaded data, or None."""
    for entry in data["entries"]:
        if entry.get("site") == site and entry.get("streamer") == streamer:
            return entry
    return None


def _get_or_create_entry(data: dict, site: str, streamer: str) -> PriorityEntry:
    """Return the matching entry, creating one at the end of the order if missing."""
    entry = _find_entry(data, site, streamer)
    if entry is not None:
        return entry
    next_position = max((e.get("position", 0) for e in data["entries"]), default=-1) + 1
    entry = {
        "site": site,
        "streamer": streamer,
        "position": next_position,
        "bypass": False,
        "overrides": _default_overrides(),
    }
    data["entries"].append(entry)
    return entry


def _validate_field(field: str) -> None:
    """Raise ValueError if field isn't one of the seven override types."""
    if field not in OVERRIDE_FIELDS:
        raise ValueError(f"Unknown override field: {field!r}")


def get_order(data_dir: Path) -> List[PriorityEntry]:
    """Return every entry, bypass entries first, then sorted by position."""
    data = _load(data_dir)
    return sorted(data["entries"], key=_sort_key)


def set_order(data_dir: Path, entries: List[PriorityEntry]) -> None:
    """Replace the full entry list, renumbering position by list order."""
    data = _load(data_dir)
    reordered = []
    for i, entry in enumerate(entries):
        new_entry = dict(entry)
        new_entry["position"] = i
        reordered.append(new_entry)
    data["entries"] = reordered
    _save(data_dir, data)


def set_bypass(data_dir: Path, site: str, streamer: str, bypass: bool) -> None:
    """Set (or clear) the bypass flag for a site+streamer, creating the entry if needed."""
    data = _load(data_dir)
    entry = _get_or_create_entry(data, site, streamer)
    entry["bypass"] = bool(bypass)
    _save(data_dir, data)


def get_override(data_dir: Path, site: str, streamer: str, field: str) -> Any:
    """Return a streamer's raw override value for field, or None if unset/absent."""
    _validate_field(field)
    data = _load(data_dir)
    entry = _find_entry(data, site, streamer)
    if entry is None:
        return None
    return entry.get("overrides", {}).get(field)


def set_override(data_dir: Path, site: str, streamer: str, field: str, value: Any) -> None:
    """Set a streamer's override value for field, creating the entry if needed."""
    _validate_field(field)
    data = _load(data_dir)
    entry = _get_or_create_entry(data, site, streamer)
    entry.setdefault("overrides", _default_overrides())[field] = value
    _save(data_dir, data)


def clear_override(data_dir: Path, site: str, streamer: str, field: str) -> None:
    """Reset a streamer's override for field back to null (inherit site default)."""
    _validate_field(field)
    data = _load(data_dir)
    entry = _find_entry(data, site, streamer)
    if entry is None:
        return
    entry.setdefault("overrides", _default_overrides())[field] = None
    _save(data_dir, data)


def resolve_effective(data_dir: Path, site: str, streamer: str, field: str, site_default: Any) -> Any:
    """Return the streamer's override for field if set, else the given site default."""
    value = get_override(data_dir, site, streamer, field)
    return value if value is not None else site_default
