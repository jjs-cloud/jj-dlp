"""Atomic JSON read/write, backup rotation, and load-with-fallback recovery."""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("jj_dlp.storage")

BACKUP_KEEP = 5
BACKUP_DIRNAME = ".backups"


def _atomic_write(path: Path, data: Any) -> None:
    """Serialize data to a temp file, fsync, verify, then replace path atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=False)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp_name, "r", encoding="utf-8") as f:
            json.load(f)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def _read_json(path: Path) -> Optional[Any]:
    """Read and parse a JSON file, returning None if missing or unparsable."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _backup_dir(path: Path) -> Path:
    """Return the backup directory for a given config file path."""
    return path.parent / BACKUP_DIRNAME / path.name


def rotate_backups(path: Path, keep: int = BACKUP_KEEP) -> None:
    """Copy the current file into its backup dir, then prune to the newest `keep`."""
    path = Path(path)
    if not path.exists():
        return
    bdir = _backup_dir(path)
    bdir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    shutil.copy2(path, bdir / f"{path.name}.{stamp}.bak")
    backups = sorted(bdir.glob(f"{path.name}.*.bak"), key=lambda p: p.name)
    for old in backups[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def _newest_backup(path: Path) -> Optional[Path]:
    """Return the most recent backup file for path, or None if there is none."""
    bdir = _backup_dir(path)
    if not bdir.is_dir():
        return None
    backups = sorted(bdir.glob(f"{path.name}.*.bak"), key=lambda p: p.name)
    return backups[-1] if backups else None


def save_config(path: Path, data: Any) -> None:
    """Rotate backups, then atomically write a config/* file."""
    path = Path(path)
    rotate_backups(path)
    _atomic_write(path, data)


def load_config(
    path: Path,
    defaults: Callable[[], Any],
    validate: Optional[Callable[[Any], bool]] = None,
) -> Any:
    """Load a config/* file, falling back to the newest backup then defaults on failure."""
    path = Path(path)
    data = _read_json(path)
    if data is not None and (validate is None or validate(data)):
        return data

    # Missing file is expected on first run; failed validation on an existing file is a real problem.
    missing = data is None
    log_fn = log.debug if missing else log.warning
    if missing:
        log_fn("Config file %s missing or unreadable; trying backups.", path)
    else:
        log_fn("Config file %s failed validation; trying backups.", path)

    backup = _newest_backup(path)
    while backup is not None:
        data = _read_json(backup)
        if data is not None and (validate is None or validate(data)):
            log.warning("Recovered %s from backup %s.", path, backup.name)
            return data
        bdir = _backup_dir(path)
        remaining = sorted(bdir.glob(f"{path.name}.*.bak"), key=lambda p: p.name)
        remaining = [b for b in remaining if b != backup]
        backup = remaining[-1] if remaining else None

    log_fn("No valid backup for %s; using defaults.", path)
    return defaults()


def save_state(path: Path, data: Any) -> None:
    """Atomically write a state/* file. No backups."""
    _atomic_write(Path(path), data)


def load_state(path: Path, default: Callable[[], Any]) -> Any:
    """Load a state/* file; missing or corrupt silently returns the default."""
    data = _read_json(Path(path))
    return data if data is not None else default()
