"""Changelog diff generation between shipped defaults (Doc 1 §9.4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def _flatten_fields(schema_data: dict) -> Dict[str, Any]:
    """Flatten a shipped fields.json structure into {dotted_path: default_value}."""
    flat: Dict[str, Any] = {}
    for f in schema_data.get("app_fields", []):
        flat[f["path"]] = f.get("default")
    for f in schema_data.get("site_fields", []):
        flat[f["path"]] = f.get("default")
    for plugin_id, fields in schema_data.get("plugin_fields", {}).items():
        for f in fields:
            flat[f"{plugin_id}.{f['path']}"] = f.get("default")
    return flat


def diff_shipped_defaults(old_schema_data: dict, new_schema_data: dict) -> List[str]:
    """Return one line per added/removed/changed-default field between two shipped schemas."""
    old_flat = _flatten_fields(old_schema_data)
    new_flat = _flatten_fields(new_schema_data)
    lines: List[str] = []

    for path in sorted(set(new_flat) - set(old_flat)):
        lines.append(f"+ {path} (default: {new_flat[path]!r})")
    for path in sorted(set(old_flat) - set(new_flat)):
        lines.append(f"- {path}")
    for path in sorted(set(old_flat) & set(new_flat)):
        if old_flat[path] != new_flat[path]:
            lines.append(f"~ {path}: {old_flat[path]!r} -> {new_flat[path]!r}")

    return lines


def format_diff(lines: List[str]) -> str:
    """Join diff lines into one pretty-printed block, or a no-changes message."""
    if not lines:
        return "No configuration schema changes."
    return "\n".join(lines)


def diff_shipped_files(old_path: Path, new_path: Path) -> List[str]:
    """Load two shipped fields.json files from disk and diff them."""
    with open(Path(old_path), "r", encoding="utf-8") as f:
        old_data = json.load(f)
    with open(Path(new_path), "r", encoding="utf-8") as f:
        new_data = json.load(f)
    return diff_shipped_defaults(old_data, new_data)
