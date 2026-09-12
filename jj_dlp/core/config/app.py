"""Load/save wrapper for config/app.json."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, fields as dc_fields
from pathlib import Path
from typing import Any, Dict, List, Optional

from jj_dlp.core.config import schema, storage

CONFIG_RELPATH = Path("config") / "app.json"
SCHEMA_RELPATH = Path("schema") / "fields.json"
SCHEMA_VERSION = 1

REQUIRED_GROUPS = {
    "updates", "web_ui", "recording", "output", "notifications",
    "ui", "disk", "prompts", "debug",
}


@dataclass
class UpdatesConfig:
    check_for_updates: bool = True
    update_interval_min: int = 30
    update_branch: str = "main"


@dataclass
class WebUiConfig:
    enabled: bool = False
    port: int = 8765
    user: str = ""
    password: str = ""  # JSON key is "pass" (reserved word in Python)


@dataclass
class RecordingConfig:
    max_concurrent: int = 0
    lq_downloader_enabled: bool = False
    ff_err_threshold: int = 200


@dataclass
class OutputConfig:
    subfolders: str = "streamer-only"
    delete_empty: bool = True
    collapsible_folders: bool = True
    # Not schema-driven: owned by the File Manager's Move popup, not the Config tab.
    destinations: List[dict] = field(default_factory=list)


@dataclass
class NotificationsConfig:
    ntfy_topic: str = ""
    notify_confirm_file: bool = True
    notify_no_confirm_file: bool = True


@dataclass
class UiConfig:
    site_sort: str = "last_live_desc"
    dashboard: str = "curses"
    rgb_mode: bool = True
    compact_view: str = "auto"


@dataclass
class DiskConfig:
    drives: List[str] = field(default_factory=list)
    graph_scale: int = 300
    sample_interval_sec: int = 5


@dataclass
class PromptsConfig:
    ask_for_config: bool = True


@dataclass
class DebugConfig:
    enabled: bool = False
    log_path: str = "logs/debug.log"


@dataclass
class AppConfig:
    """In-memory representation of config/app.json."""

    schema_version: int = SCHEMA_VERSION
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    web_ui: WebUiConfig = field(default_factory=WebUiConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    disk: DiskConfig = field(default_factory=DiskConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)

    # --- typed get/set helpers, one pair per field group ---

    def get_updates(self) -> UpdatesConfig:
        """Return the updates settings group."""
        return self.updates

    def set_updates(self, **kwargs: Any) -> None:
        """Update one or more fields in the updates settings group."""
        _apply_fields(self.updates, kwargs)

    def get_web_ui(self) -> WebUiConfig:
        """Return the web_ui settings group."""
        return self.web_ui

    def set_web_ui(self, **kwargs: Any) -> None:
        """Update one or more fields in the web_ui settings group."""
        _apply_fields(self.web_ui, kwargs)

    def get_recording(self) -> RecordingConfig:
        """Return the recording settings group."""
        return self.recording

    def set_recording(self, **kwargs: Any) -> None:
        """Update one or more fields in the recording settings group."""
        _apply_fields(self.recording, kwargs)

    def get_output(self) -> OutputConfig:
        """Return the output settings group."""
        return self.output

    def set_output(self, **kwargs: Any) -> None:
        """Update one or more fields in the output settings group."""
        _apply_fields(self.output, kwargs)

    def get_notifications(self) -> NotificationsConfig:
        """Return the notifications settings group."""
        return self.notifications

    def set_notifications(self, **kwargs: Any) -> None:
        """Update one or more fields in the notifications settings group."""
        _apply_fields(self.notifications, kwargs)

    def get_ui(self) -> UiConfig:
        """Return the ui settings group."""
        return self.ui

    def set_ui(self, **kwargs: Any) -> None:
        """Update one or more fields in the ui settings group."""
        _apply_fields(self.ui, kwargs)

    def get_disk(self) -> DiskConfig:
        """Return the disk settings group."""
        return self.disk

    def set_disk(self, **kwargs: Any) -> None:
        """Update one or more fields in the disk settings group."""
        _apply_fields(self.disk, kwargs)

    def get_prompts(self) -> PromptsConfig:
        """Return the prompts settings group."""
        return self.prompts

    def set_prompts(self, **kwargs: Any) -> None:
        """Update one or more fields in the prompts settings group."""
        _apply_fields(self.prompts, kwargs)

    def get_debug(self) -> DebugConfig:
        """Return the debug settings group."""
        return self.debug

    def set_debug(self, **kwargs: Any) -> None:
        """Update one or more fields in the debug settings group."""
        _apply_fields(self.debug, kwargs)

    # --- (de)serialization ---

    def to_dict(self) -> dict:
        """Serialize to the JSON-shaped dict, mapping password back to 'pass'."""
        return {
            "schema_version": self.schema_version,
            "updates": asdict(self.updates),
            "web_ui": _web_ui_to_dict(self.web_ui),
            "recording": asdict(self.recording),
            "output": asdict(self.output),
            "notifications": asdict(self.notifications),
            "ui": asdict(self.ui),
            "disk": asdict(self.disk),
            "prompts": asdict(self.prompts),
            "debug": asdict(self.debug),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AppConfig":
        """Build an AppConfig from a raw dict, filling missing groups/fields from defaults."""
        d = data or {}
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            updates=UpdatesConfig(**_merged(UpdatesConfig, d.get("updates"))),
            web_ui=WebUiConfig(**_merged_web_ui(d.get("web_ui"))),
            recording=RecordingConfig(**_merged(RecordingConfig, d.get("recording"))),
            output=OutputConfig(**_merged(OutputConfig, d.get("output"))),
            notifications=NotificationsConfig(**_merged(NotificationsConfig, d.get("notifications"))),
            ui=UiConfig(**_merged(UiConfig, d.get("ui"))),
            disk=DiskConfig(**_merged(DiskConfig, d.get("disk"))),
            prompts=PromptsConfig(**_merged(PromptsConfig, d.get("prompts"))),
            debug=DebugConfig(**_merged(DebugConfig, d.get("debug"))),
        )


def _apply_fields(group_obj: Any, updates: Dict[str, Any]) -> None:
    """Set only the given, valid attribute names on a config group object."""
    valid = {f.name for f in dc_fields(group_obj)}
    for key, value in updates.items():
        if key not in valid:
            raise AttributeError(f"'{type(group_obj).__name__}' has no field '{key}'")
        setattr(group_obj, key, value)


def _merged(dc_type: type, sub: Optional[dict]) -> dict:
    """Return sub's known fields overlaid onto dc_type's own defaults."""
    sub = sub or {}
    valid = {f.name for f in dc_fields(dc_type)}
    result = {f.name: getattr(dc_type(), f.name) for f in dc_fields(dc_type)}
    for key, value in sub.items():
        if key in valid:
            result[key] = value
    return result


def _merged_web_ui(sub: Optional[dict]) -> dict:
    """Return web_ui fields overlaid onto defaults, mapping JSON 'pass' to 'password'."""
    sub = dict(sub or {})
    if "pass" in sub:
        sub["password"] = sub.pop("pass")
    return _merged(WebUiConfig, sub)


def _web_ui_to_dict(cfg: WebUiConfig) -> dict:
    """Serialize WebUiConfig, mapping 'password' back to the JSON key 'pass'."""
    return {"enabled": cfg.enabled, "port": cfg.port, "user": cfg.user, "pass": cfg.password}


def _set_nested(d: dict, dotted_path: str, value: Any) -> None:
    """Set value at a dotted path inside a nested dict, creating groups as needed."""
    parts = dotted_path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def get_default(schema_path: Optional[Path] = None) -> AppConfig:
    """Build a fresh AppConfig from schema/fields.json's app_fields defaults."""
    nested: Dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for f in schema.get_app_fields(schema_path):
        _set_nested(nested, f["path"], copy.deepcopy(f.get("default")))
    # output.destinations has no schema entry; seed it directly (Doc 1 §3.3).
    nested.setdefault("output", {})["destinations"] = []
    return AppConfig.from_dict(nested)


def _validate(data: Any) -> bool:
    """Check that data is a dict containing every required top-level settings group."""
    return isinstance(data, dict) and REQUIRED_GROUPS.issubset(data.keys())


def _config_path(data_dir: Path) -> Path:
    """Return the full path to config/app.json under a data directory."""
    return Path(data_dir) / CONFIG_RELPATH


def _default_schema_path(data_dir: Path) -> Path:
    """Return the default schema/fields.json path under a data directory."""
    return Path(data_dir) / SCHEMA_RELPATH


def load(data_dir: Path, schema_path: Optional[Path] = None) -> AppConfig:
    """Load config/app.json, recovering via backups then schema-derived defaults on failure."""
    data_dir = Path(data_dir)
    schema_path = Path(schema_path) if schema_path else _default_schema_path(data_dir)
    raw = storage.load_config(
        _config_path(data_dir),
        lambda: get_default(schema_path).to_dict(),
        _validate,
    )
    return AppConfig.from_dict(raw)


def save(data_dir: Path, config: AppConfig) -> None:
    """Atomically save an AppConfig to config/app.json, rotating backups first."""
    storage.save_config(_config_path(data_dir), config.to_dict())
