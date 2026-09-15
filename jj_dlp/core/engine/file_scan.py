"""Output-dir scanning for the File Manager tab."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from jj_dlp.core.engine.site_state import SiteState

_NATURAL_SPLIT = re.compile(r"(\d+)")

_MODE_DEPTH = {"none": 0, "site-only": 1, "streamer-only": 1, "site-and-streamer": 2}


@dataclass(frozen=True)
class FileRow:
    """One scanned output file: identity, ownership, and write-activity state."""

    path: str
    size: int
    modified: float
    site: Optional[str]
    streamer: Optional[str]
    state: str  # "WRITING" or "IDLE"
    group_key: str
    sort_key: tuple


def natural_sort_key(name: str) -> tuple:
    """Build a numeric-aware sort key so 'part2' sorts before 'part10'."""
    parts = _NATURAL_SPLIT.split(name)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def _unique_dirs(sites: Dict[str, dict]) -> Dict[str, List[str]]:
    """Map each distinct configured output.dir string to the site labels using it."""
    dirs: Dict[str, List[str]] = {}
    for label, config in sites.items():
        out_dir = config.get("output", {}).get("dir", "recordings")
        dirs.setdefault(out_dir, []).append(label)
    return dirs


def _streamer_owners(sites: Dict[str, dict]) -> Dict[str, List[str]]:
    """Map each streamer name to the site label(s) it's configured under."""
    owners: Dict[str, List[str]] = {}
    for label, config in sites.items():
        for streamer in list(config.get("streamers", [])) + list(config.get("disabled", [])):
            owners.setdefault(streamer, []).append(label)
    return owners


def _writing_paths(site_states: Dict[str, SiteState]) -> Dict[str, Tuple[str, str]]:
    """Map each currently in-progress absolute path to its owning (site, streamer)."""
    writing: Dict[str, Tuple[str, str]] = {}
    for site_label, state in site_states.items():
        for streamer in state.known_streamers():
            path = state.get_in_progress_path(streamer)
            if path:
                writing[str(Path(path).resolve())] = (site_label, streamer)
    return writing


def _identify(
    rel_parts: Tuple[str, ...],
    mode: str,
    dir_sites: List[str],
    streamer_owners: Dict[str, List[str]],
) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort (site, streamer) from a file's path components and the subfolders mode."""
    depth = _MODE_DEPTH.get(mode, 0)
    if mode == "site-and-streamer" and len(rel_parts) > depth:
        return rel_parts[0], rel_parts[1]
    if mode == "site-only" and len(rel_parts) > depth:
        return rel_parts[0], None
    if mode == "streamer-only" and len(rel_parts) > depth:
        streamer = rel_parts[0]
        owners = streamer_owners.get(streamer, [])
        site = owners[0] if len(owners) == 1 else (dir_sites[0] if len(dir_sites) == 1 else None)
        return site, streamer
    site = dir_sites[0] if len(dir_sites) == 1 else None
    return site, None


def scan(
    sites: Dict[str, dict],
    site_states: Dict[str, SiteState],
    subfolders_mode: str,
    collapsible_folders: bool = True,
) -> List[FileRow]:
    """Scan every loaded site's output.dir (deduplicated) and return file rows."""
    dir_sites_map = _unique_dirs(sites)
    owners = _streamer_owners(sites)
    writing = _writing_paths(site_states)

    rows: List[FileRow] = []
    seen_roots: set = set()
    for out_dir, dir_sites in dir_sites_map.items():
        root = Path(out_dir)
        if not root.is_dir():
            continue
        resolved_root = root.resolve()
        if resolved_root in seen_roots:
            continue
        seen_roots.add(resolved_root)

        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                full = Path(dirpath) / filename
                try:
                    stat = full.stat()
                except OSError:
                    continue
                resolved = str(full.resolve())
                rel_parts = full.relative_to(root).parts
                site, streamer = _identify(rel_parts, subfolders_mode, dir_sites, owners)
                state = "IDLE"
                owner = writing.get(resolved)
                if owner is not None:
                    site, streamer = owner
                    state = "WRITING"
                group_key = str(full.parent) if collapsible_folders else str(root)
                rows.append(
                    FileRow(
                        path=resolved,
                        size=stat.st_size,
                        modified=stat.st_mtime,
                        site=site,
                        streamer=streamer,
                        state=state,
                        group_key=group_key,
                        sort_key=natural_sort_key(filename),
                    )
                )
    return rows
