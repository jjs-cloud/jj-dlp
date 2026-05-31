import os
import shutil
import curses
import hashlib
import threading
from datetime import datetime
from typing import NamedTuple

try:
    from .logger import dbg as _dbg
except ImportError:
    try:
        from logger import dbg as _dbg
    except ImportError:
        def _dbg(msg: str, site_name: str = "") -> None:  # type: ignore[misc]
            pass


class ConfigItem:
    def __init__(self, line_idx: int, is_section: bool, key: str, value: str, has_equals: bool, raw_line: str, comment: str = ""):
        self.line_idx = line_idx
        self.is_section = is_section
        self.key = key
        self.value = value
        self.has_equals = has_equals
        self.raw_line = raw_line
        self.comment = comment  # Help text parsed from the # line(s) above this key


# ══════════════════════════════════════════════════════════════════════════════
# Single source of truth for all config keys
#
# scope    : "global"  → lives in global.conf, shown in GlobalConfigEditor
#            "site"    → lives in per-site .conf, shown in site ConfigEditor
# default  : value written when the key is missing / a fresh file is created
# preserve : True  → value is carried over from the user's file during an update
#            False → value is reset to the new template default on update
# comment  : help text shown in the edit popup
# ══════════════════════════════════════════════════════════════════════════════

class _KeyDef(NamedTuple):
    name:     str
    scope:    str   # "global" | "site"
    default:  str
    preserve: bool
    comment:  str


CONFIG_KEYS: tuple[_KeyDef, ...] = (
    # ── Global keys (global.conf) ─────────────────────────────────────────────
    _KeyDef("DISK_DRIVES",           "global", "",      True,
            "Comma-separated list of drives or paths to show disk info in the system panel. "
            "(e.g. C:\\,D:\\ or /home,/mnt/data)."),
    _KeyDef("DEBUG_LOGS",            "global", "false", True,
            "Enable debug logging to a file (true/false)."),
    _KeyDef("DEBUG_LOG_PATH",        "global", "",      True,
            "Path for the debug log file. Can be a relative or absolute path (e.g. logs/debug.log)."),
    _KeyDef("CHECK_FOR_UPDATES",     "global", "true",  True,
            "Whether to periodically check for app updates (true/false)."),
    _KeyDef("UPDATE_INTERVAL",       "global", "30",    True,
            "Number of minutes between app update checks."),
    _KeyDef("ASK_FOR_BROWSER",       "global", "true",  True,
            "Show the browser chooser on startup (true/false)."),
    _KeyDef("ASK_FOR_CONFIG",        "global", "true",  True,
            "Show the config file chooser on startup (true/false)."),
    _KeyDef("UPDATE_BRANCH",         "global", "main",  True,
            "Which branch of jj-dlp to update to. (main, testing, or experimental)."),
    _KeyDef("MAX_CONCURRENT_REC",    "global", "0",     True,
            "Maximum number of streamers to record simultaneously (0 = unlimited)."),

    # ── Site keys (per-site .conf) ────────────────────────────────────────────
    _KeyDef("SITE_LABEL",            "site",   "",      True,
            "Display label for this site in the dashboard."),
    _KeyDef("SITE_ORDER",            "site",   "999",   True,
            "Sort position of this site in the dashboard (lower = further left)."),
    _KeyDef("CHECK_INTERVAL",        "site",   "60",    False,
            "Seconds between liveness checks for this site."),
    _KeyDef("OUTPUT_DIR",            "site",   "recordings", True,
            "Directory where recordings are saved."),
    _KeyDef("OUTPUT_TMPL",           "site",   "%(title)s [%(id)s].%(ext)s", True,
            "yt-dlp output filename template."),
    _KeyDef("COOLDOWN_AFTER_RECORDING", "site", "5",   False,
            "Seconds to wait after a recording ends before checking again."),
    _KeyDef("SPLIT_AFTER",           "site",   "0",    True,
            "Split recordings after this many seconds (0 = disabled)."),
    _KeyDef("STALL_CHECK_INTERVAL",  "site",   "30",   False,
            "Seconds between stall-detection checks."),
    _KeyDef("STALL_TIMEOUT",         "site",   "120",  False,
            "Seconds without output before a recording is considered stalled."),
    _KeyDef("CONFIG_CHECK_INTERVAL", "site",   "3",    False,
            "Seconds between config-file reload checks."),
    _KeyDef("SITE_TMPL",             "site",   "",     False,
            "URL template used to build the stream URL from a username."),
    _KeyDef("PANEL_RESIZE",          "site",   "true", True,
            "Allow the dashboard panel to be resized (true/false)."),
    _KeyDef("LOGGING",               "site",   "false", False,
            "Enable per-site log files (true/false)."),
    _KeyDef("LOG_PATH",              "site",   "",     False,
            "Path for per-site log files."),
    _KeyDef("SPLIT_LOGS",            "site",   "false", False,
            "Write a separate log file per recording session (true/false)."),
    _KeyDef("POPUP_NOTIFICATIONS",   "site",   "true", True,
            "Show popup notifications for recording events (true/false)."),
    _KeyDef("POPUP_TIMEOUT",         "site",   "15",   True,
            "Seconds a notification popup stays visible."),
    _KeyDef("POPUP_COOLDOWN",        "site",   "30",   True,
            "Minimum seconds between successive popups for the same site."),
    _KeyDef("YT_DLP_PATH_WINDOWS",   "site",   "",     False,
            "Path to yt-dlp executable on Windows. Leave blank to use the bundled copy."),
    _KeyDef("YT_DLP_PATH_MAC",       "site",   "",     False,
            "Path to yt-dlp executable on macOS. Leave blank to use the bundled copy."),
    _KeyDef("YT_DLP_PATH_LINUX",     "site",   "",     False,
            "Path to yt-dlp executable on Linux. Leave blank to use the bundled copy."),
    _KeyDef("PROGRESS_BAR_MAX_HOURS","site",   "6",    True,
            "Maximum hours shown on the recording progress bar."),
    _KeyDef("PROGRESS_BAR_WIDTH",    "site",   "14",   True,
            "Width (in characters) of the recording progress bar."),
    _KeyDef("DOWNLOADER_COOKIES",    "site",   "true", False,
            "Pass browser cookies to yt-dlp for downloading (true/false)."),
    _KeyDef("CHECKER_COOKIES",       "site",   "false", False,
            "Pass browser cookies to yt-dlp for liveness checks (true/false)."),
    _KeyDef("LAST_LIVE_HIGHLIGHT",   "site",   "0",    True,
            "Seconds to highlight a streamer after they were last seen live (0 = disabled)."),
)

# ── Derived helpers (consumed by this module and importable by others) ─────────

# Keys that belong in global.conf — used to filter them out of the site editor
_GLOBAL_KEYS: set[str] = {k.name for k in CONFIG_KEYS if k.scope == "global"}

# Ordered list of global key names (preserves declaration order above)
_GLOBAL_KEYS_ORDER: list[str] = [k.name for k in CONFIG_KEYS if k.scope == "global"]

# Default values keyed by name — for both scopes
_KEY_DEFAULTS: dict[str, str] = {k.name: k.default for k in CONFIG_KEYS}

# Help comments keyed by name
_KEY_COMMENTS: dict[str, str] = {k.name: k.comment for k in CONFIG_KEYS}

# Keys that must be preserved across an update (both global and site)
PRESERVED_KEYS: list[str] = [k.name for k in CONFIG_KEYS if k.preserve]

# ── Priority panel ─────────────────────────────────────────────────────────────
# Width of the PRIORITY panel box (x2 − x1 span), matching the SYSTEM sidebar.
PRIORITY_PANEL_W: int = 27




def _compute_config_id(config_paths: "list[str]") -> str:
    """Compute a stable short ID for a combination of loaded config file paths."""
    h = hashlib.sha256()
    for p in sorted(config_paths):
        h.update(p.encode("utf-8"))
    return h.hexdigest()[:16]


def _compute_config_sha(config_path: str) -> str:
    """Compute a short SHA of a config file's raw content (for change detection)."""
    try:
        with open(config_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return ""


class PriorityEntry(NamedTuple):
    """Represents one streamer entry in the PRIORITY panel."""
    streamer:    str   # lowercase username
    site:        str   # SITE_LABEL from the config that owns this streamer
    config_path: str   # absolute path to the .conf file
    config_sha:  str   # short SHA of that .conf file at last load
    bypass:      bool  # True → always-record (displayed in green, sorted to top)


class PriorityEditor:
    """Manages the PRIORITY panel: display, reordering, bypass toggle, persistence."""

    # Key bindings (configurable here)
    KEY_MOVE_UP   = (ord('u'), ord('U'))
    KEY_MOVE_DOWN = (ord('d'), ord('D'))
    KEY_BYPASS    = (ord('b'), ord('B'))

    def __init__(self, dashboard):
        self.dashboard      = dashboard
        self._entries:  "list[PriorityEntry]" = []
        self._selected_idx:  int = 0
        self._scroll_offset: int = 0
        self._loaded:        bool = False
        self._config_id:     str  = ""

    # ── Public interface ───────────────────────────────────────────────────────

    def force_reload(self) -> None:
        """Mark data as stale so the next draw() call refreshes from disk."""
        self._loaded = False

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._refresh()
            self._loaded = True

    # ── Data management ────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Rebuild the entry list from current sites + saved global.json data."""
        sites = self.dashboard.sites
        if not sites:
            self._entries = []
            self._config_id = ""
            return

        # Collect (streamer, site_label, config_path, config_sha) from every site.
        raw: "list[tuple[str,str,str,str]]" = []
        for site in sites:
            cfg        = site.get_cached_config()
            site_label = cfg.get("site_label", os.path.basename(site.config_path))
            streamers  = cfg.get("streamers", [])
            sha        = _compute_config_sha(site.config_path)
            for s in streamers:
                raw.append((s.lower(), site_label, site.config_path, sha))

        # Compute the config_id for this exact combination of loaded files.
        config_paths   = [site.config_path for site in sites]
        self._config_id = _compute_config_id(config_paths)

        # Load saved priority data for this config_id.
        # Deferred import avoids a circular dependency (main imports config_editor
        # at module scope); by the time _refresh() is ever called both modules are
        # fully initialised.
        from .main import _global_json_lock, _load_global_json
        with _global_json_lock:
            global_data = _load_global_json()
        saved_block   = global_data.get("priorities", {}).get(self._config_id, {})
        saved_entries = saved_block.get("entries", [])

        # Build a lookup: (streamer, site) → saved dict
        saved_map: "dict[tuple,dict]" = {}
        for i, e in enumerate(saved_entries):
            key = (e.get("streamer", ""), e.get("site", ""))
            saved_map[key] = {
                "bypass":   e.get("bypass", False),
                "priority": i,
            }

        # Build enriched list with saved priority / bypass values.
        enriched = []
        for (streamer, site_label, config_path, config_sha) in raw:
            key      = (streamer, site_label)
            saved    = saved_map.get(key, {"bypass": False, "priority": 999999})
            enriched.append({
                "streamer":    streamer,
                "site":        site_label,
                "config_path": config_path,
                "config_sha":  config_sha,
                "bypass":      saved["bypass"],
                "priority":    saved["priority"],
            })

        # Sort: bypass entries first (by saved order), then normal entries (by saved order).
        bypass_part = sorted([e for e in enriched if     e["bypass"]], key=lambda x: x["priority"])
        normal_part = sorted([e for e in enriched if not e["bypass"]], key=lambda x: x["priority"])

        self._entries = [
            PriorityEntry(
                streamer    = e["streamer"],
                site        = e["site"],
                config_path = e["config_path"],
                config_sha  = e["config_sha"],
                bypass      = e["bypass"],
            )
            for e in (bypass_part + normal_part)
        ]

        # Clamp selection.
        if self._entries:
            self._selected_idx = min(self._selected_idx, len(self._entries) - 1)
        else:
            self._selected_idx = 0

    def _save(self) -> None:
        """Persist current entry ordering and bypass flags to global.json."""
        if not self._config_id:
            return
        config_paths = [site.config_path for site in self.dashboard.sites]
        entries_data = [
            {
                "streamer":   e.streamer,
                "site":       e.site,
                "config_sha": e.config_sha,
                "priority":   i,
                "bypass":     e.bypass,
            }
            for i, e in enumerate(self._entries)
        ]
        from .main import _global_json_lock, _load_global_json, _save_global_json
        with _global_json_lock:
            global_data = _load_global_json()
            if "priorities" not in global_data or not isinstance(global_data["priorities"], dict):
                global_data["priorities"] = {}
            global_data["priorities"][self._config_id] = {
                "config_files": config_paths,
                "entries":      entries_data,
            }
            _save_global_json(global_data)

    # ── Movement helpers ───────────────────────────────────────────────────────

    def _move(self, idx: int, direction: int) -> None:
        """Swap entry at *idx* with its neighbour in *direction* (+1 down / -1 up).
        Movement is constrained to within the same group (bypass / normal).
        """
        n = len(self._entries)
        if not n or not (0 <= idx < n):
            return
        new_idx = idx + direction
        if not (0 <= new_idx < n):
            return
        # Do not cross the bypass ↔ normal boundary.
        if self._entries[idx].bypass != self._entries[new_idx].bypass:
            return
        lst = list(self._entries)
        lst[idx], lst[new_idx] = lst[new_idx], lst[idx]
        self._entries    = lst
        self._selected_idx = new_idx
        self._save()

    def _toggle_bypass(self, idx: int) -> None:
        """Toggle the bypass flag on entry *idx*, relocating it within the list."""
        if not (0 <= idx < len(self._entries)):
            return
        e       = self._entries[idx]
        new_e   = PriorityEntry(e.streamer, e.site, e.config_path, e.config_sha, not e.bypass)
        lst     = list(self._entries)
        lst.pop(idx)
        # Insert at the boundary between bypass and normal sections.
        boundary = sum(1 for x in lst if x.bypass)
        if new_e.bypass:
            # Newly bypassed → place at the END of the bypass block (just before normals).
            lst.insert(boundary, new_e)
            self._selected_idx = boundary
        else:
            # Newly un-bypassed → place at the START of the normal block.
            lst.insert(boundary, new_e)
            self._selected_idx = boundary
        self._entries = lst
        self._save()

    # ── Key handling ───────────────────────────────────────────────────────────

    def handle_key(self, key) -> bool:
        """Process a keypress while this panel has focus.  Returns True if consumed."""
        self.ensure_loaded()
        if key == curses.KEY_UP:
            self._selected_idx = max(0, self._selected_idx - 1)
            return True
        elif key == curses.KEY_DOWN:
            self._selected_idx = min(len(self._entries) - 1, self._selected_idx + 1)
            return True
        elif key in self.KEY_MOVE_UP:
            self._move(self._selected_idx, -1)
            return True
        elif key in self.KEY_MOVE_DOWN:
            self._move(self._selected_idx, +1)
            return True
        elif key in self.KEY_BYPASS:
            self._toggle_bypass(self._selected_idx)
            return True
        return False

    # ── Drawing ────────────────────────────────────────────────────────────────

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int, is_active: bool) -> None:
        """Draw the PRIORITY panel inside the box (y1,x1)–(y2,x2)."""
        self.ensure_loaded()
        db = self.dashboard

        # Box border
        db.draw_box(stdscr, y1, x1, y2, x2, db.C_SYSTEM)
        db.safe_addstr(stdscr, y1, x1 + 2, " PRIORITY ",
                       curses.color_pair(db.C_LIVE) | curses.A_BOLD)
        if is_active:
            mode_str = " [ PRI ] "
            db.safe_addstr(stdscr, y1, x2 - len(mode_str) - 1, mode_str,
                           curses.color_pair(db.C_LIVE) | curses.A_BOLD)

        db.safe_addstr(stdscr, y2 - 2, x1 + 2, " bypass=always record ", 
                       curses.color_pair(db.C_DIM))

        if not self._entries:
            db.safe_addstr(stdscr, y1 + 2, x1 + 2, "No streamers.",
                           curses.color_pair(db.C_DIM))
            return

        visible_rows = (y2 - y1) - 2
        # Scroll to keep selection visible.
        if self._selected_idx < self._scroll_offset:
            self._scroll_offset = self._selected_idx
        elif self._selected_idx >= self._scroll_offset + visible_rows:
            self._scroll_offset = self._selected_idx - visible_rows + 1

        panel_inner_w = (x2 - x1) - 3   # usable character columns inside box

        row_y = y1 + 1
        for i in range(self._scroll_offset,
                       min(len(self._entries), self._scroll_offset + visible_rows)):
            entry  = self._entries[i]
            is_sel = is_active and (i == self._selected_idx)

            label = f"{entry.streamer}:{entry.site}"
            if len(label) > panel_inner_w - 2:
                label = label[:panel_inner_w - 5] + "..."

            prefix = "> " if is_sel else "  "

            if entry.bypass:
                # Always-record streamers rendered in green (C_LIVE).
                attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                        if is_sel
                        else curses.color_pair(db.C_LIVE) | curses.A_BOLD)
            else:
                attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                        if is_sel
                        else curses.color_pair(db.C_NORMAL))

            db.safe_addstr(stdscr, row_y, x1 + 1, prefix + label, attr)
            row_y += 1


def _validate_value(key: str, value: str) -> tuple[bool, str]:
    """Validate config values based on their expected types."""
    bool_keys = {"DEBUG_LOGS", "CHECK_FOR_UPDATES", "ASK_FOR_BROWSER", "ASK_FOR_CONFIG", 
                 "PANEL_RESIZE", "LOGGING", "SPLIT_LOGS", "POPUP_NOTIFICATIONS", 
                 "DOWNLOADER_COOKIES", "CHECKER_COOKIES"}
    int_keys = {"UPDATE_INTERVAL", "SITE_ORDER", "CHECK_INTERVAL", "COOLDOWN_AFTER_RECORDING", 
                "SPLIT_AFTER", "STALL_CHECK_INTERVAL", "STALL_TIMEOUT", "CONFIG_CHECK_INTERVAL", 
                "POPUP_TIMEOUT", "POPUP_COOLDOWN", "PROGRESS_BAR_MAX_HOURS", "PROGRESS_BAR_WIDTH", 
                "LAST_LIVE_HIGHLIGHT", "MAX_CONCURRENT_REC"}
    if key in bool_keys:
        if value.lower() not in ("true", "false", "yes", "no", "1", "0"):
            return False, "Must be true or false"
    if key in int_keys:
        try:
            val = int(value)
            if val < 0 and key != "SITE_ORDER":
                return False, "Must be >= 0"
        except ValueError:
            return False, "Must be an integer"
    return True, ""


def _wrap_text(text: str, width: int) -> list:
    """Word-wrap text to fit within `width` columns, returning a list of lines."""
    if not text or width <= 0:
        return []
    words = text.split()
    lines, current = [], ""
    for word in words:
        if current:
            if len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                lines.append(current)
                current = word
        else:
            current = word
    if current:
        lines.append(current)
    return lines


class GlobalConfigEditor:
    """Loads and edits global.conf — the app-wide settings."""

    # Derived from CONFIG_KEYS — no duplication needed here
    GLOBAL_KEYS_ORDER    = _GLOBAL_KEYS_ORDER
    GLOBAL_KEYS_COMMENTS = _KEY_COMMENTS

    def __init__(self, dashboard, on_save=None):
        self.dashboard = dashboard
        self._on_save = on_save          # callable(new_cfg: dict) | None
        self.conf_path = self._find_global_conf()
        self.lines: list = []
        self.items: list = []
        self.selected_idx = 0
        self.scroll_offset = 0
        self.popup_mode = False
        self.popup_buf = ""
        self.popup_error = ""
        self.editing_item = None
        self._loaded = False

    @staticmethod
    def _find_global_conf() -> str:
        """Return the path to global.conf inside the configs/ directory."""
        config_dir = os.path.abspath("configs")
        os.makedirs(config_dir, exist_ok=True)          # Ensure directory exists
        return os.path.join(config_dir, "global.conf")

    def _ensure_loaded(self):
        if not self._loaded:
            self._load()
            self._loaded = True

    def _load(self):
        """Read global.conf (creating it with defaults if absent) and build items list."""
        if not os.path.isfile(self.conf_path):
            self._create_default()
        try:
            with open(self.conf_path, "r", encoding="utf-8") as f:
                self.lines = f.readlines()
        except Exception:
            self.lines = []
        self._parse()

    def _create_default(self):
        """Write a minimal global.conf with all global keys in the configs/ folder."""
        lines = ["[General]\n", "\n"]
        for kdef in CONFIG_KEYS:
            if kdef.scope != "global":
                continue
            lines.append(f"# {kdef.comment}\n")
            lines.append(f"{kdef.name} = {kdef.default}\n")
            lines.append("\n")
        try:
            with open(self.conf_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception:
            pass

    def _parse(self):
        """Build self.items from self.lines — only [General] keys that are global."""
        self.items = []
        in_general = False
        pending_comment = ""
        for i, line in enumerate(self.lines):
            s = line.strip()
            if not s:
                pending_comment = ""
                continue
            if s.startswith("#") or s.startswith(";"):
                fragment = s.lstrip("#;").strip()
                pending_comment = (pending_comment + " " + fragment).strip() if pending_comment else fragment
                continue
            if s.startswith("[") and s.endswith("]"):
                in_general = s[1:-1] == "General"
                pending_comment = ""
                continue
            if in_general and "=" in s:
                k, v = s.split("=", 1)
                k = k.strip()
                if k.upper() in _GLOBAL_KEYS:
                    comment = pending_comment or self.GLOBAL_KEYS_COMMENTS.get(k.upper(), "")
                    self.items.append(ConfigItem(i, False, k.upper(), v.strip(), True, line, comment))
            pending_comment = ""

        # If any expected keys are missing (file was hand-edited), append them
        existing_keys = {item.key for item in self.items}
        for key in self.GLOBAL_KEYS_ORDER:
            if key not in existing_keys:
                self._append_key(key)

        # Re-sort items in canonical order
        order = {k: idx for idx, k in enumerate(self.GLOBAL_KEYS_ORDER)}
        self.items.sort(key=lambda it: order.get(it.key, 99))

    def _append_key(self, key: str):
        """Append a missing key to the [General] section of self.lines and self.items."""
        val = _KEY_DEFAULTS.get(key, "")
        new_line = f"{key} = {val}\n"
        # Find end of [General] section or end of file
        insert_at = len(self.lines)
        for i, line in enumerate(self.lines):
            s = line.strip()
            if s.startswith("[") and s.endswith("]") and s[1:-1] != "General":
                insert_at = i
                break
        self.lines.insert(insert_at, new_line)
        comment = self.GLOBAL_KEYS_COMMENTS.get(key, "")
        self.items.append(ConfigItem(insert_at, False, key, val, True, new_line, comment))

    def save(self):
        """Write self.lines back to global.conf with a backup."""
        _dbg(f"[CONFIG] GlobalConfigEditor.save() called — conf_path={self.conf_path!r}")
        backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(self.conf_path))), "backups")
        _dbg(f"[CONFIG] backup_dir resolved to {backup_dir!r}")
        try:
            os.makedirs(backup_dir, exist_ok=True)
            _dbg(f"[CONFIG] backup_dir created/confirmed OK")
        except Exception as e:
            _dbg(f"[CONFIG] ERROR creating backup_dir: {e}")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"global.conf.{timestamp}.bak")
        _dbg(f"[CONFIG] backup_path={backup_path!r}, source exists={os.path.isfile(self.conf_path)}")
        try:
            shutil.copy2(self.conf_path, backup_path)
            _dbg(f"[CONFIG] backup written OK")
        except Exception as e:
            _dbg(f"[CONFIG] ERROR writing backup: {e}")
        try:
            with open(self.conf_path, "w", encoding="utf-8") as f:
                f.writelines(self.lines)
            _dbg(f"[CONFIG] global.conf written OK ({len(self.lines)} lines)")
        except Exception as e:
            _dbg(f"[CONFIG] ERROR writing global.conf: {e}")
        # Reload so line indices stay accurate
        self._loaded = False
        try:
            self._load()
            _dbg(f"[CONFIG] GlobalConfigEditor.save() reload completed items={len(self.items)}")
        except Exception as e:
            _dbg(f"[CONFIG] GlobalConfigEditor.save() reload failed: {e}")

        # Apply changes to live globals immediately (e.g. DEBUG_LOGS)
        if self._on_save:
            new_cfg = {item.key: item.value for item in self.items}
            try:
                self._on_save(new_cfg)
                _dbg("[CONFIG] GlobalConfigEditor.save() on_save applied")
            except Exception as e:
                _dbg(f"[CONFIG] GlobalConfigEditor.save() on_save failed: {e}")

    def handle_key(self, key) -> bool:
        """Handle a keypress in the global editor section. Returns True if consumed."""
        self._ensure_loaded()
        if self.popup_mode:
            _dbg(f"[CONFIG] GlobalConfigEditor.handle_key() popup key={key} popup_buf={self.popup_buf!r} editing_item={self.editing_item.key if self.editing_item else None}")
            if key == 27:
                self.popup_mode = False
                self.popup_buf = ""
                self.popup_error = ""
                self.editing_item = None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.popup_buf = self.popup_buf[:-1]
                self.popup_error = ""
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
                if self.editing_item:
                    new_val = self.popup_buf.strip()
                    _dbg(f"[CONFIG] GlobalConfigEditor.handle_key() Enter pressed for {self.editing_item.key!r} new_val={new_val!r}")
                    is_valid, err_msg = _validate_value(self.editing_item.key, new_val)
                    if not is_valid:
                        self.popup_error = err_msg
                        _dbg(f"[CONFIG] GlobalConfigEditor.handle_key() validation failed: {err_msg}")
                        return True
                    if 0 <= self.editing_item.line_idx < len(self.lines):
                        self.lines[self.editing_item.line_idx] = f"{self.editing_item.key} = {new_val}\n"
                    else:
                        self.popup_error = "Internal error: invalid config line"
                        _dbg(f"[CONFIG] GlobalConfigEditor.handle_key() bad line_idx={self.editing_item.line_idx} len(lines)={len(self.lines)}")
                        return True
                    try:
                        self.save()
                    except Exception as e:
                        self.popup_error = f"Save failed: {e}"
                        _dbg(f"[CONFIG] GlobalConfigEditor.handle_key() save failed: {e}")
                        return True
                    _dbg(f"[CONFIG] GlobalConfigEditor.handle_key() save completed for {self.editing_item.key!r}")
                self.popup_mode = False
                self.popup_buf = ""
                self.popup_error = ""
                self.editing_item = None
                _dbg("[CONFIG] GlobalConfigEditor.handle_key() popup closed after save")
            elif 32 <= key < 127:
                self.popup_buf += chr(key)
                self.popup_error = ""
            return True

        if key == curses.KEY_UP:
            self.selected_idx = max(0, self.selected_idx - 1)
            return True
        elif key == curses.KEY_DOWN:
            self.selected_idx = min(len(self.items) - 1, self.selected_idx + 1)
            return True
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
            if self.items:
                self.editing_item = self.items[self.selected_idx]
                self.popup_buf = self.editing_item.value
                self.popup_mode = True
            return True
        return False

    def draw(self, stdscr, y1, x1, y2, x2, is_active: bool):
        """Draw the global settings panel in the given box."""
        self._ensure_loaded()
        db = self.dashboard

        self.dashboard.draw_box(stdscr, y1, x1, y2, x2, db.C_SYSTEM)
        title = " GLOBAL SETTINGS "
        self.dashboard.safe_addstr(stdscr, y1, x1 + 2, title, curses.color_pair(db.C_LIVE) | curses.A_BOLD)
        if is_active:
            mode_str = " [ GLOBAL ] "
            self.dashboard.safe_addstr(stdscr, y1, x2 - len(mode_str) - 1, mode_str,
                        curses.color_pair(db.C_LIVE) | curses.A_BOLD)

        visible_rows = (y2 - y1) - 2
        if self.selected_idx < self.scroll_offset:
            self.scroll_offset = self.selected_idx
        elif self.selected_idx >= self.scroll_offset + visible_rows:
            self.scroll_offset = self.selected_idx - visible_rows + 1

        row_y = y1 + 1
        for i in range(self.scroll_offset, min(len(self.items), self.scroll_offset + visible_rows)):
            item = self.items[i]
            is_sel = is_active and (i == self.selected_idx)
            prefix = "> " if is_sel else "  "
            key_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                        if is_sel else curses.color_pair(db.C_WARN) | curses.A_BOLD)
            val_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                        if is_sel else curses.color_pair(db.C_LIVE))
            self.dashboard.safe_addstr(stdscr, row_y, x1 + 1, prefix + f"{item.key:<22}", key_attr)
            self.dashboard.safe_addstr(stdscr, row_y, x1 + 26, "= " + str(item.value), val_attr)
            row_y += 1

        if self.popup_mode and self.editing_item:
            self.draw_popup(stdscr)

    def draw_popup(self, stdscr):
        self._draw_popup(stdscr)

    def _draw_popup(self, stdscr):
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        box_w = min(60, w - 4)
        inner_w = box_w - 4
        comment_lines = _wrap_text(self.editing_item.comment, inner_w) if self.editing_item.comment else []
        inner_rows = 4 + len(comment_lines) + (1 if comment_lines else 0)
        box_h = max(inner_rows + 1, 7)
        box_h = min(box_h, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w
        for y in range(by1, by2 + 1):
            self.dashboard.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(db.C_NORMAL))
        self.dashboard.draw_box(stdscr, by1, bx1, by2, bx2, db.C_SYSTEM)
        self.dashboard.safe_addstr(stdscr, by1, bx1 + 2, " EDIT GLOBAL VALUE ",
                    curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)
        row = by1 + 2
        self.dashboard.safe_addstr(stdscr, row, bx1 + 2, f"Key: {self.editing_item.key}",
                    curses.color_pair(db.C_CHROME))
        row += 1
        if comment_lines:
            for cl in comment_lines:
                self.dashboard.safe_addstr(stdscr, row, bx1 + 2, cl, curses.color_pair(db.C_DIM))
                row += 1
            row += 1
        else:
            row += 1
        self.dashboard.safe_addstr(stdscr, row, bx1 + 2, "New Value:",
                    curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)
        self.dashboard.safe_addstr(stdscr, row, bx1 + 13, (self.popup_buf + "_")[:box_w - 15],
                    curses.color_pair(db.C_NORMAL) | curses.A_BOLD)
        if self.popup_error:
            self.dashboard.safe_addstr(stdscr, by2, bx1 + 2, f" Error: {self.popup_error} ",
                        curses.color_pair(db.C_WARN) | curses.A_BOLD)
        else:
            self.dashboard.safe_addstr(stdscr, by2, bx1 + 2, " Enter: Save | Esc: Cancel ",
                        curses.color_pair(db.C_INVHEAD))


class ConfigEditor:
    def __init__(self, parent_dashboard):
        self.dashboard = parent_dashboard
        self.sites = parent_dashboard.sites
        self.selected_site_idx = parent_dashboard.selected_site_idx
        self.scroll_offset = 0
        self.selected_idx = 0
        self.popup_mode = False
        self.popup_buf = ""
        self.popup_error = ""
        self.lines = []
        self.items = []
        self.current_site_path = None
        self.editing_item = None

        # Which panel has keyboard focus: "global", "site", or "priority"
        self._focus = "site"

        # Sub-editor for global.conf
        self.global_editor = GlobalConfigEditor(
            parent_dashboard,
            on_save=getattr(parent_dashboard, "apply_global_cfg", None),
        )

        # Sub-editor for the PRIORITY panel
        self.priority_editor = PriorityEditor(parent_dashboard)

    def notify_site_changed(self, new_idx: int) -> None:
        """Called by the dashboard whenever selected_site_idx changes.

        This replaces the polling comparison that previously lived in
        draw_tab() — state is updated immediately on the event rather than
        discovered one frame later.
        """
        if new_idx == self.selected_site_idx and self.current_site_path is not None:
            return
        self.selected_site_idx = new_idx
        self.selected_idx = 0
        self.scroll_offset = 0
        if self.sites:
            site = self.sites[new_idx]
            self.load_config(site.config_path)
        # Streamer list may have changed — force a priority panel refresh.
        self.priority_editor.force_reload()

    def load_config(self, config_path):
        self.current_site_path = config_path
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                self.lines = f.readlines()
        except Exception:
            self.lines = []

        self.items = []
        current_section = None
        pending_comment = ""
        for i, line in enumerate(self.lines):
            s = line.strip()
            if not s:
                pending_comment = ""
                continue
            if s.startswith("#") or s.startswith(";"):
                fragment = s.lstrip("#;").strip()
                pending_comment = (pending_comment + " " + fragment).strip() if pending_comment else fragment
                continue
            if s.startswith("[") and s.endswith("]"):
                current_section = s[1:-1]
                pending_comment = ""
                if current_section == "General":
                    self.items.append(ConfigItem(i, True, current_section, "", False, line, ""))
            else:
                if current_section == "General":
                    if "=" in s:
                        k, v = s.split("=", 1)
                        k_stripped = k.strip()
                        # Skip keys that belong in global.conf
                        if k_stripped.upper() in _GLOBAL_KEYS:
                            pending_comment = ""
                            continue
                        self.items.append(ConfigItem(i, False, k_stripped, v.strip(), True, line, pending_comment))
                    else:
                        if s.upper() not in _GLOBAL_KEYS:
                            self.items.append(ConfigItem(i, False, s, "", False, line, pending_comment))
                    pending_comment = ""

        if self.items:
            self.selected_idx = min(self.selected_idx, len(self.items) - 1)
        else:
            self.selected_idx = 0

    def save_file(self):
        if not self.current_site_path or not self.lines:
            _dbg(f"[CONFIG] save_file() aborted — site_path={self.current_site_path!r}, lines={len(self.lines) if self.lines else 0}")
            return

        _dbg(f"[CONFIG] ConfigEditor.save_file() called — site_path={self.current_site_path!r}")

        # Create backup
        backup_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(self.current_site_path))), "backups")
        _dbg(f"[CONFIG] backup_dir resolved to {backup_dir!r}")
        try:
            os.makedirs(backup_dir, exist_ok=True)
            _dbg(f"[CONFIG] backup_dir created/confirmed OK")
        except Exception as e:
            _dbg(f"[CONFIG] ERROR creating backup_dir: {e}")
        base = os.path.basename(self.current_site_path)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"{base}.{timestamp}.bak")
        _dbg(f"[CONFIG] backup_path={backup_path!r}, source exists={os.path.isfile(self.current_site_path)}")
        try:
            shutil.copy2(self.current_site_path, backup_path)
            _dbg(f"[CONFIG] backup written OK")
        except Exception as e:
            _dbg(f"[CONFIG] ERROR writing backup: {e}")
            self.dashboard.sites[self.selected_site_idx].log_line(f"Failed to backup config: {e}")

        # Write new config
        try:
            with open(self.current_site_path, "w", encoding="utf-8") as f:
                f.writelines(self.lines)
            _dbg(f"[CONFIG] site config written OK ({len(self.lines)} lines)")
        except Exception as e:
            _dbg(f"[CONFIG] ERROR writing site config: {e}")
            self.dashboard.sites[self.selected_site_idx].log_line(f"Failed to save config: {e}")

        # Reload
        try:
            self.load_config(self.current_site_path)
            _dbg(f"[CONFIG] ConfigEditor.save_file() reload completed items={len(self.items)}")
        except Exception as e:
            _dbg(f"[CONFIG] ConfigEditor.save_file() reload failed: {e}")
            if self.current_site_path and self.current_site_path in {site.config_path for site in self.dashboard.sites}:
                self.dashboard.sites[self.selected_site_idx].log_line(f"Failed to reload config after save: {e}")

    def draw_tab(self, stdscr, y1, x1, y2, x2):
        # Ensure an initial load if the editor has never loaded a config yet
        # (first time the Config tab is opened). Site-change events are
        # delivered via notify_site_changed(), so no per-frame polling needed.
        if self.current_site_path is None and self.sites:
            site = self.sites[self.selected_site_idx]
            self.load_config(site.config_path)

        # ── Layout: three side-by-side columns ────────────────────────────────
        #
        #   [SITE SETTINGS (wide)]  [GLOBAL SETTINGS]  [PRIORITY (=system width)]
        #
        # PRIORITY_PANEL_W is the box span (x2−x1), matching the SYSTEM sidebar.
        total_w  = x2 - x1
        prio_w   = PRIORITY_PANEL_W                       # 27 → same as system sidebar
        # Global panel gets ~35% of remaining space (after reserving prio column + 2 gaps).
        global_w = max(28, int((total_w - prio_w - 2) * 0.35))
        site_w   = total_w - global_w - prio_w - 2        # 2 gaps between columns

        site_x1   = x1
        site_x2   = x1 + site_w
        global_x1 = site_x2 + 1
        global_x2 = global_x1 + global_w
        prio_x1   = global_x2 + 1
        prio_x2   = x2                                    # == prio_x1 + prio_w

        content_y1 = y1

        # ── Hint row (content_y1) — above boxes ──────────────────────────────
        # Tab-focus navigation hint above GLOBAL panel
        if self._focus == "site":
            focus_hint = "  Tab: Global Settings \u25ba  "
        elif self._focus == "global":
            focus_hint = "  \u25c4 Site  Tab: Priority \u25ba  "
        else:
            focus_hint = "  \u25c4 Tab: Global Settings  "
        self.dashboard.safe_addstr(stdscr, content_y1, global_x1, focus_hint,
                    curses.color_pair(self.dashboard.C_DIM))

        # Keybind legend above PRIORITY panel (always visible)
        prio_hint = "\u2191\u2193:nav  U:up D:dn  B:bypass"
        self.dashboard.safe_addstr(stdscr, content_y1, prio_x1, prio_hint,
                    curses.color_pair(self.dashboard.C_DIM))

        # ── Draw GLOBAL SETTINGS panel (middle column) ────────────────────────
        self.global_editor.draw(stdscr, content_y1 + 1, global_x1, y2, global_x2,
                                is_active=(self._focus == "global"))

        # ── Draw PRIORITY panel (right column) ───────────────────────────────
        self.priority_editor.draw(stdscr, content_y1 + 1, prio_x1, y2, prio_x2,
                                  is_active=(self._focus == "priority"))

        # ── Site selector tabs above the site box ─────────────────────────────
        tab_x = site_x1 + 1
        self.dashboard.safe_addstr(stdscr, content_y1, site_x1, "  Site: ",
                    curses.color_pair(self.dashboard.C_DIM))
        tab_x += 8
        for i, site in enumerate(self.sites):
            lbl = os.path.basename(site.config_path)
            label = f" {lbl} "
            attr = (curses.color_pair(self.dashboard.C_HILIGHT) | curses.A_BOLD
                    if i == self.selected_site_idx
                    else curses.color_pair(self.dashboard.C_CHROME))
            self.dashboard.safe_addstr(stdscr, content_y1, tab_x, label, attr)
            tab_x += len(label) + 1

        # ── Draw SITE SETTINGS box (left column) ──────────────────────────────
        site_box_y1 = content_y1 + 1
        self.dashboard.draw_box(stdscr, site_box_y1, site_x1, y2, site_x2, self.dashboard.C_CHROME)
        if self._focus == "site":
            mode_str = " [ SITE ] "
            self.dashboard.safe_addstr(stdscr, site_box_y1, site_x2 - len(mode_str) - 1, mode_str,
                        curses.color_pair(self.dashboard.C_LIVE) | curses.A_BOLD)
        self.dashboard.safe_addstr(stdscr, site_box_y1, site_x1 + 2, " SITE SETTINGS ",
                    curses.color_pair(self.dashboard.C_LIVE) | curses.A_BOLD)

        if not self.items:
            self.dashboard.safe_addstr(stdscr, site_box_y1 + 2, site_x1 + 4,
                        "No configurable items found.",
                        curses.color_pair(self.dashboard.C_DIM))
        else:
            visible_rows = (y2 - site_box_y1) - 2
            if self.selected_idx < self.scroll_offset:
                self.scroll_offset = self.selected_idx
            elif self.selected_idx >= self.scroll_offset + visible_rows:
                self.scroll_offset = self.selected_idx - visible_rows + 1

            row_y = site_box_y1 + 1
            for i in range(self.scroll_offset,
                           min(len(self.items), self.scroll_offset + visible_rows)):
                item = self.items[i]
                is_selected = self._focus == "site" and (i == self.selected_idx)

                if is_selected:
                    attr = curses.color_pair(self.dashboard.C_HILIGHT) | curses.A_BOLD
                    prefix = "> "
                else:
                    prefix = "  "
                    attr = (curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD
                            if item.is_section else curses.color_pair(self.dashboard.C_NORMAL))

                if item.is_section:
                    disp_text = f"[{item.key}]"
                    sec_attr = (curses.color_pair(self.dashboard.C_HILIGHT) | curses.A_BOLD
                                if is_selected else curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)
                    self.dashboard.safe_addstr(stdscr, row_y, site_x1 + 2, prefix + disp_text, sec_attr)
                else:
                    key_attr = (attr if is_selected
                                else curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)
                    val_attr = (attr if is_selected
                                else curses.color_pair(self.dashboard.C_LIVE))
                    self.dashboard.safe_addstr(stdscr, row_y, site_x1 + 2, prefix + f"{item.key:<25}", key_attr)
                    if item.has_equals:
                        self.dashboard.safe_addstr(stdscr, row_y, site_x1 + 29, "= " + str(item.value), val_attr)
                row_y += 1

        # Draw popup (whichever sub-editor owns it)
        if self._focus == "global" and self.global_editor.popup_mode and self.global_editor.editing_item:
            self.global_editor.draw_popup(stdscr)
        elif self._focus == "site" and self.popup_mode and self.editing_item:
            self.draw_popup(stdscr)

    def draw_popup(self, stdscr):
        h, w = stdscr.getmaxyx()
        box_w = min(60, w - 4)
        inner_w = box_w - 4

        comment_lines = []
        if self.editing_item and self.editing_item.comment:
            comment_lines = _wrap_text(self.editing_item.comment, inner_w)

        inner_rows = 4 + len(comment_lines) + (1 if comment_lines else 0)
        box_h = inner_rows + 1
        box_h = max(box_h, 7)
        box_h = min(box_h, h - 4)

        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            self.dashboard.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(self.dashboard.C_NORMAL))

        self.dashboard.draw_box(stdscr, by1, bx1, by2, bx2, self.dashboard.C_WARN)
        title = " EDIT CONFIG VALUE "
        self.dashboard.safe_addstr(stdscr, by1, bx1 + 2, title, curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)

        row = by1 + 2
        self.dashboard.safe_addstr(stdscr, row, bx1 + 2, f"Key: {self.editing_item.key}", curses.color_pair(self.dashboard.C_CHROME))
        row += 1

        if comment_lines:
            for cl in comment_lines:
                self.dashboard.safe_addstr(stdscr, row, bx1 + 2, cl, curses.color_pair(self.dashboard.C_DIM))
                row += 1
            row += 1
        else:
            row += 1

        self.dashboard.safe_addstr(stdscr, row, bx1 + 2, "New Value:", curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)
        self.dashboard.safe_addstr(stdscr, row, bx1 + 13, (self.popup_buf + "_")[:box_w - 15], curses.color_pair(self.dashboard.C_NORMAL) | curses.A_BOLD)

        if self.popup_error:
            self.dashboard.safe_addstr(stdscr, by2, bx1 + 2, f" Error: {self.popup_error} ", curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)
        else:
            self.dashboard.safe_addstr(stdscr, by2, bx1 + 2, " Enter: Save | Esc: Cancel ", curses.color_pair(self.dashboard.C_INVHEAD))

    def handle_key(self, key) -> bool:
        """Returns True if the key was consumed by the editor."""

        # Tab key cycles focus: site → global → priority → site → …
        # (only when no popup is open in any sub-editor)
        any_popup = self.global_editor.popup_mode or self.popup_mode
        if key == ord('\t') and not any_popup:
            _cycle = ["site", "global", "priority"]
            self._focus = _cycle[(_cycle.index(self._focus) + 1) % len(_cycle)]
            return True

        # ── Priority panel focus ──────────────────────────────────────────────
        if self._focus == "priority":
            if key == 27:
                self.dashboard.selected_tab = 0
                return True
            return self.priority_editor.handle_key(key)

        if self._focus == "global":
            # Escape in global panel without popup → exit Config tab
            if key == 27 and not self.global_editor.popup_mode:
                self.dashboard.selected_tab = 0
                return True
            return self.global_editor.handle_key(key)

        # ── Site panel focus ──────────────────────────────────────────────────
        if self.popup_mode:
            if key == 27:
                self.popup_mode = False
                self.popup_buf = ""
                self.popup_error = ""
                self.editing_item = None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.popup_buf = self.popup_buf[:-1]
                self.popup_error = ""
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
                if self.editing_item:
                    new_val = self.popup_buf.strip()
                    is_valid, err_msg = _validate_value(self.editing_item.key, new_val)
                    if not is_valid:
                        self.popup_error = err_msg
                        return True
                    if 0 <= self.editing_item.line_idx < len(self.lines):
                        if self.editing_item.has_equals:
                            self.lines[self.editing_item.line_idx] = f"{self.editing_item.key} = {new_val}\n"
                        else:
                            self.lines[self.editing_item.line_idx] = f"{new_val}\n"
                    else:
                        self.popup_error = "Internal error: invalid config line"
                        return True
                    try:
                        self.save_file()
                    except Exception as e:
                        self.popup_error = f"Save failed: {e}"
                        return True
                    site = self.sites[self.selected_site_idx]
                    site.trigger_event.set()
                    # Streamer list may have changed — refresh priority panel.
                    self.priority_editor.force_reload()
                self.popup_mode = False
                self.popup_buf = ""
                self.popup_error = ""
                self.editing_item = None
            elif 32 <= key < 127:
                self.popup_buf += chr(key)
                self.popup_error = ""
            return True

        if key == 27:
            self.dashboard.selected_tab = 0
            return True
        elif key == curses.KEY_UP:
            if self.items:
                self.selected_idx = max(0, self.selected_idx - 1)
            return True
        elif key == curses.KEY_DOWN:
            if self.items:
                self.selected_idx = min(len(self.items) - 1, self.selected_idx + 1)
            return True
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
            if self.items and not self.items[self.selected_idx].is_section:
                self.editing_item = self.items[self.selected_idx]
                if self.editing_item.has_equals:
                    self.popup_buf = self.editing_item.value
                else:
                    self.popup_buf = self.editing_item.key
                self.popup_mode = True
            return True

        return False
