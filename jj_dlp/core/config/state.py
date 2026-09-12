"""Typed wrappers for the state/*.json files (Doc 1 §3.8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from jj_dlp.core.config import storage

LIVE_STATUS_RELPATH = Path("state") / "live_status.json"
DISK_HISTORY_RELPATH = Path("state") / "disk_history.json"
UPDATE_CHECK_RELPATH = Path("state") / "update_check.json"
EVENTSUB_RELPATH = Path("state") / "eventsub.json"


# --- live_status.json ---

def _live_status_path(data_dir: Path) -> Path:
    """Return the full path to state/live_status.json under a data directory."""
    return Path(data_dir) / LIVE_STATUS_RELPATH


def _load_live_status(data_dir: Path) -> dict:
    """Load state/live_status.json; missing/corrupt returns an empty structure."""
    return storage.load_state(_live_status_path(data_dir), dict)


def _save_live_status(data_dir: Path, data: dict) -> None:
    """Atomically save state/live_status.json."""
    storage.save_state(_live_status_path(data_dir), data)


def get_live_status(data_dir: Path, site: str, streamer: str) -> Dict[str, Any]:
    """Return a streamer's live-status entry, or an empty dict if none recorded."""
    data = _load_live_status(data_dir)
    return data.get(site, {}).get(streamer, {})


def set_last_live(data_dir: Path, site: str, streamer: str, last_live: float) -> None:
    """Record the epoch timestamp a streamer was last seen live."""
    data = _load_live_status(data_dir)
    entry = data.setdefault(site, {}).setdefault(streamer, {})
    entry["last_live"] = last_live
    _save_live_status(data_dir, data)


def set_live_since(data_dir: Path, site: str, streamer: str, live_since: Optional[float]) -> None:
    """Set (or clear, if None) the epoch timestamp a streamer's current live session began."""
    data = _load_live_status(data_dir)
    entry = data.setdefault(site, {}).setdefault(streamer, {})
    if live_since is None:
        entry.pop("live_since", None)
    else:
        entry["live_since"] = live_since
    _save_live_status(data_dir, data)


def get_segment_continuation(data_dir: Path, site: str, streamer: str) -> Optional[Dict[str, Any]]:
    """Return {next_part, sidecar_path} for an in-progress split sequence, or None."""
    return get_live_status(data_dir, site, streamer).get("segment_continuation")


def set_segment_continuation(
    data_dir: Path, site: str, streamer: str, next_part: Optional[int], sidecar_path: Optional[str]
) -> None:
    """Set (or clear, if next_part is None) a streamer's segment-continuation state."""
    data = _load_live_status(data_dir)
    entry = data.setdefault(site, {}).setdefault(streamer, {})
    if next_part is None:
        entry.pop("segment_continuation", None)
    else:
        entry["segment_continuation"] = {"next_part": next_part, "sidecar_path": sidecar_path}
    _save_live_status(data_dir, data)


def remove_streamer_live_status(data_dir: Path, site: str, streamer: str) -> None:
    """Remove a site+streamer's live-status entry entirely (e.g. on streamer removal)."""
    data = _load_live_status(data_dir)
    data.get(site, {}).pop(streamer, None)
    _save_live_status(data_dir, data)


def remove_site_live_status(data_dir: Path, site: str) -> None:
    """Remove every live-status entry for a site (e.g. on site deletion)."""
    data = _load_live_status(data_dir)
    data.pop(site, None)
    _save_live_status(data_dir, data)


# --- disk_history.json ---

def _disk_history_path(data_dir: Path) -> Path:
    """Return the full path to state/disk_history.json under a data directory."""
    return Path(data_dir) / DISK_HISTORY_RELPATH


def _load_disk_history(data_dir: Path) -> dict:
    """Load state/disk_history.json; missing/corrupt returns an empty buffer."""
    return storage.load_state(_disk_history_path(data_dir), lambda: {"values": []})


def get_disk_history(data_dir: Path) -> List[float]:
    """Return the rolling list of historical disk-rate bar values."""
    return _load_disk_history(data_dir).get("values", [])


def append_disk_history(data_dir: Path, value: float, max_len: int) -> None:
    """Append a sample to the rolling buffer, trimming to max_len from the front."""
    data = _load_disk_history(data_dir)
    values = data.get("values", [])
    values.append(value)
    if len(values) > max_len:
        values = values[-max_len:]
    data["values"] = values
    storage.save_state(_disk_history_path(data_dir), data)


# --- update_check.json ---

def _update_check_path(data_dir: Path) -> Path:
    """Return the full path to state/update_check.json under a data directory."""
    return Path(data_dir) / UPDATE_CHECK_RELPATH


def _default_update_check() -> dict:
    """Return an empty update_check.json structure."""
    return {
        "last_check_ts": None,
        "latest_sha_seen": None,
        "installed_sha": None,
        "changelog_shown_for_version": None,
    }


def get_update_check(data_dir: Path) -> Dict[str, Any]:
    """Return the full update-check state."""
    return storage.load_state(_update_check_path(data_dir), _default_update_check)


def set_update_check(data_dir: Path, **kwargs: Any) -> None:
    """Update one or more fields of the update-check state and save."""
    data = get_update_check(data_dir)
    data.update(kwargs)
    storage.save_state(_update_check_path(data_dir), data)


# --- eventsub.json ---

def _eventsub_path(data_dir: Path) -> Path:
    """Return the full path to state/eventsub.json under a data directory."""
    return Path(data_dir) / EVENTSUB_RELPATH


def _load_eventsub(data_dir: Path) -> dict:
    """Load state/eventsub.json; missing/corrupt returns an empty structure."""
    return storage.load_state(_eventsub_path(data_dir), dict)


def _save_eventsub(data_dir: Path, data: dict) -> None:
    """Atomically save state/eventsub.json."""
    storage.save_state(_eventsub_path(data_dir), data)


def _default_site_eventsub() -> dict:
    """Return a fresh per-site eventsub state structure."""
    return {"server_status": "stopped", "subscription_ids": {}, "notifications": []}


def get_eventsub_state(data_dir: Path, site: str) -> Dict[str, Any]:
    """Return a site's eventsub state (server status, subscription ids, notification history)."""
    data = _load_eventsub(data_dir)
    return data.get(site, _default_site_eventsub())


def set_server_status(data_dir: Path, site: str, status: str) -> None:
    """Set a site's webhook server_status string."""
    data = _load_eventsub(data_dir)
    entry = data.setdefault(site, _default_site_eventsub())
    entry["server_status"] = status
    _save_eventsub(data_dir, data)


def set_subscription_id(data_dir: Path, site: str, login: str, sub_id: Optional[str]) -> None:
    """Set (or, if sub_id is None, remove) a login's EventSub subscription id."""
    data = _load_eventsub(data_dir)
    entry = data.setdefault(site, _default_site_eventsub())
    sub_ids = entry.setdefault("subscription_ids", {})
    if sub_id is None:
        sub_ids.pop(login, None)
    else:
        sub_ids[login] = sub_id
    _save_eventsub(data_dir, data)


def get_subscription_ids(data_dir: Path, site: str) -> Dict[str, str]:
    """Return a site's {login: subscription_id} map."""
    return get_eventsub_state(data_dir, site).get("subscription_ids", {})


def add_notification(data_dir: Path, site: str, notification: Dict[str, Any], max_len: int = 50) -> None:
    """Append a notification record to a site's history, trimming to max_len."""
    data = _load_eventsub(data_dir)
    entry = data.setdefault(site, _default_site_eventsub())
    history = entry.setdefault("notifications", [])
    history.append(notification)
    if len(history) > max_len:
        del history[: len(history) - max_len]
    _save_eventsub(data_dir, data)


def remove_site_eventsub(data_dir: Path, site: str) -> None:
    """Remove a site's eventsub state entirely (e.g. on site deletion)."""
    data = _load_eventsub(data_dir)
    data.pop(site, None)
    _save_eventsub(data_dir, data)
