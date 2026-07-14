import os
import shutil
import curses
import hashlib
import threading
from datetime import datetime
from typing import NamedTuple, Optional

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
# scope    : "global"  → lives in global.conf, shown in GlobalConfigEditor
#            "site"    → lives in per-site .conf, shown in site ConfigEditor
# default  : value written when the key is missing / a fresh file is created.  The default value is rarely used, since we provide template config files with all the keys prepopulated.
# preserve : True  → value is carried over from the user's file during an update
#            False → value is reset to the value in the template config file during an update.
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
    _KeyDef("DISK_DRIVES",           "global", "",      True,  "Comma-separated list of drives or paths to show disk info in the system panel. (e.g. C:\\, D:\\, E:\\  or  /home,/mnt/data)."),
    _KeyDef("DEBUG_LOGS",            "global", "false", True,  "Enable debug logging to a file (true/false)."),
    _KeyDef("DEBUG_LOG_PATH",        "global", "",      True,  "Path for the debug log file. Can be a relative or absolute path (e.g. logs/debug.log)"),
    _KeyDef("CHECK_FOR_UPDATES",     "global", "true",  True,  "Whether to check for app updates at startup and periodically (true/false)."),
    _KeyDef("UPDATE_INTERVAL",       "global", "30",    True,  "Number of minutes between app update checks."),
    _KeyDef("ASK_FOR_BROWSER",       "global", "true",  False,  "Show the browser chooser on startup (true/false)."),
    _KeyDef("ASK_FOR_CONFIG",        "global", "true",  True,  "Show the config file chooser on startup (true/false)."),
    _KeyDef("UPDATE_BRANCH",         "global", "main",  True,  "Which branch of jj-dlp to update to. (main, testing, or experimental)."),
    _KeyDef("MAX_CONCURRENT_REC",    "global", "0",     True,  'The maximum number of simultaneous recordings allowed to run.  Use the "PRIORITIES" panel in the Config tab to adjust the priority of each streamer. (0=no limit)'),
    _KeyDef("LQ_DOWNLOADER",         "global", "false", True,  "When any recording reaches the ffmpeg error threshold (FF_ERR_THRESH) lower the video quality of the lowest priority streamer, freeing up bandwidth for the remaining streamers."),
    _KeyDef("FF_ERR_THRESH",         "global", "200",   True,  'Restart the download if we see this many ffmpeg errors ("timestamp discontinuity", "Packet corrupt") default: 200'),
    _KeyDef("SUBFOLDERS",            "global", "false", True,  "Save recordings into a subfolder named after the streamer inside OUTPUT_DIR (true/false)."),
    _KeyDef("NTFY_TOPIC",            "global", "",      True,  "The topic name to use for ntfy.sh notifications. (example: jj-dlp-fj48dh734fk) Refer to docs/ntfy-setup.md for a detailed setup guide. (blank = disabled)"),
    _KeyDef("NTFY_URL",              "global", "https://ntfy.sh", False, "The URL of the ntfy instance (default: https://ntfy.sh)."),
    _KeyDef("SITE_SORT",             "global", "added_first", True, "The order to display streamers on each site panel.   This can also be adjusted by pressing the S key on the Dashboard tab."),

    # ── Site keys (per-site .conf) ────────────────────────────────────────────
    _KeyDef("SITE_LABEL",            "site",   "",      True,  "The name of this site."),
    _KeyDef("SITE_ORDER",            "site",   "999",   True,  "The position on the dashboard to display this site's panel (e.g. 0 for top-left, 1 for top-right, 2 for bottom-left, 3 for bottom-right, etc.)"),
    _KeyDef("CHECK_INTERVAL",        "site",   "60",    False, "How often to check if streamers are live (in seconds).  (Default: 60)"),
    _KeyDef("OUTPUT_DIR",            "site",   "recordings", True, 'Folder where recordings will be saved.  Can be an absolute path or relative path.  example: "C:\\recordings" or "recordings"'),
    _KeyDef("OUTPUT_TMPL",           "site",   "%(title)s [%(id)s].%(ext)s", False, "Template for naming the video files. (Reference: https://github.com/yt-dlp/yt-dlp#output-templates)"),
    _KeyDef("COOLDOWN_AFTER_RECORDING", "site", "60",   False, "Seconds to wait after a recording ends before checking again."),
    _KeyDef("SPLIT_AFTER",           "site",   "0",    True,  "When recording a stream, split the video file(s) every X minutes. (0 = no split)"),
    _KeyDef("STALL_CHECK_INTERVAL",  "site",   "30",   True, "How often to check if the recording has stalled (in seconds).  Disable by setting this to a large number. (Default: 30)"),
    _KeyDef("STALL_TIMEOUT",         "site",   "120",  True, "Time to wait before considering a recording stalled (in seconds). (Default: 120)"),
    _KeyDef("CONFIG_CHECK_INTERVAL", "site",   "3",    False, "How often to check for changes to the configuration file (in seconds). (Default: 3)"),
    _KeyDef("SITE_TMPL",             "site",   "",     False, "URL where the live stream can be accessed. {username} will be replaced with the streamer's username."),
    _KeyDef("PANEL_RESIZE",          "site",   "true", True,  "When true, site panels will expand vertically as needed to display all streamers."),
    _KeyDef("LOGGING",               "site",   "false", True, "Log yt-dlp (stdout) and ffmpeg (stderr) to a file."),
    _KeyDef("LOG_PATH",              "site",   "",     True,  "Path to save the log file.  Can be an absolute or relative path."),
    _KeyDef("SPLIT_LOGS",            "site",   "false", True, "When LOGGING = true, create 2 separate log files.  One for stdout (yt-dlp) and one for stderr (ffmpeg)."),
    _KeyDef("POPUP_NOTIFICATIONS",   "site",   "true", True,  "Show a popup notification when a streamer goes live."),
    _KeyDef("NTFY_NOTIFICATIONS",    "site",   "true", True,  "Push a notification to your phone via ntfy.sh when a recording starts. This requires NTFY_TOPIC to be set in the GLOBAL SETTINGS panel. (true/false)"),
    _KeyDef("AD_ALERTS",             "site",   "True", True,  "Show an alert in the system panel when ads are detected in a recording (true/false)."),
    _KeyDef("POPUP_TIMEOUT",         "site",   "15",   True,  "Seconds to show the popup notification when a streamer goes live."),
    _KeyDef("POPUP_COOLDOWN",        "site",   "30",   True,  "Minutes to wait before showing another popup notification for the same streamer."),
    _KeyDef("YT_DLP_PATH_WINDOWS",   "site",   "",     True, 'Path to the yt-dlp executable.  "YT_DLP_PATH = bin/yt-dlp.exe" to use the bundled windows executable.  "YT_DLP_PATH = bin/yt-dlp" to use the bundled linux executable.  "YT_DLP_PATH = yt-dlp" to use PATH'),
    _KeyDef("YT_DLP_PATH_MAC",       "site",   "",     True, 'Path to the yt-dlp executable.  "YT_DLP_PATH = bin/yt-dlp.exe" to use the bundled windows executable.  "YT_DLP_PATH = bin/yt-dlp" to use the bundled linux executable.  "YT_DLP_PATH = yt-dlp" to use PATH'),
    _KeyDef("YT_DLP_PATH_LINUX",     "site",   "",     True, 'Path to the yt-dlp executable.  "YT_DLP_PATH = bin/yt-dlp.exe" to use the bundled windows executable.  "YT_DLP_PATH = bin/yt-dlp" to use the bundled linux executable.  "YT_DLP_PATH = yt-dlp" to use PATH'),
    _KeyDef("PROGRESS_BAR_MAX_HOURS","site",   "10",    True,  "Duration of the progress bar in the site panel of the dashboard. (in hours)"),
    _KeyDef("PROGRESS_BAR_WIDTH",    "site",   "58",   True,  "Width of the progress bar in the site panel of the dashboard. (in characters)"),
    _KeyDef("DOWNLOADER_COOKIES",    "site",   "true", False, "Whether to write the --cookies-from-browser flag to this config file's [Downloader] section when a browser is selected at startup."),
    _KeyDef("CHECKER_COOKIES",       "site",   "false", False, "Whether to write the --cookies-from-browser flag to this config file's [Checker] section when a browser is selected at startup."),
    _KeyDef("LAST_LIVE_HIGHLIGHT",   "site",   "0",    True,  'Highlight the "Last Live" timestamp when the streamer was last live within X days.'),
    _KeyDef("UPGRADE_QUALITY",       "site",   "true", True, "Restart the recording when a higher quality is available. (true/false)."),
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

# Lookup: key name -> preserve flag (used to flag "managed" keys in the edit popup)
_KEY_PRESERVE: dict[str, bool] = {k.name: k.preserve for k in CONFIG_KEYS}


def _managed_key_note(key: str) -> str:
    """Returns the ' (note: this is a managed key)' suffix for keys with preserve = False.

    Returns an empty string for preserved keys (preserve = True) or unknown keys.
    """
    if _KEY_PRESERVE.get(key, True):
        return ""
    return " (note: this is a managed key)"

# ── Priority panel ─────────────────────────────────────────────────────────────
# Width of the PRIORITY panel box (x2 − x1 span), matching the SYSTEM sidebar.
PRIORITY_PANEL_W: int = 40

# ── Sort options for site panels (Dashboard tab) ───────────────────────────────
SORT_OPTIONS: "list[tuple[str, str]]" = [
    ("alpha_asc",      "Alphabetical (Asc)"),
    ("alpha_desc",     "Alphabetical (Desc)"),
    ("added_first",    "Added (Asc)"),
    ("added_last",     "Added (Desc)"),
    ("last_live_asc",  "Last live (Asc)"),
    ("last_live_desc", "Last live (Desc)"),
    ("priority_asc",   "Priority (Asc)"),
    ("priority_desc",  "Priority (Desc)"),
    ("live_first",     "Currently Live (Asc)"),
    ("live_last",      "Currently Live (Desc)"),
]
_SORT_KEYS:   list = [k       for k, _   in SORT_OPTIONS]
_SORT_LABELS: dict = {k: lbl  for k, lbl in SORT_OPTIONS}
SORT_DEFAULT: str  = "added_first"




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
    streamer:         str   # lowercase username
    site:             str   # SITE_LABEL from the config that owns this streamer
    config_path:      str   # absolute path to the .conf file
    config_sha:       str   # short SHA of that .conf file at last load
    bypass:           bool  # True → always-record (displayed in green, sorted to top)
    has_override:     bool = False  # True → streamer has ANY streamer-level override
                                     # active (Schedule, Split, Notifications, or
                                     # Quality/LQ) that takes precedence over the
                                     # site-level equivalent. Drives the "*" marker
                                     # in the PRIORITY list.


def _get_site_default_cfg(dashboard, entry: "PriorityEntry") -> dict:
    """Return the cached site config dict that owns *entry*.

    Used by the per-streamer settings popups to show the site-level value a
    setting would inherit if there were no streamer-level override, so the
    popup can display an accurate "Effective: X" value instead of leaving
    the precedence between the two levels ambiguous. Returns {} if the
    owning site can't be found (e.g. sites were reloaded).
    """
    try:
        for site in dashboard.sites:
            if site.config_path == entry.config_path:
                return site.get_cached_config()
    except Exception:
        pass
    return {}


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
        self._settings_popup: "Optional[StreamerSettingsPopup]" = None

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
        priorities_block = global_data.get("priorities", {})
        # First time we've ever seen this config_id (e.g. fresh clone, no
        # priority/bypass action has been taken yet) → there is no saved
        # block at all.  If we don't persist something now, downstream
        # consumers that only *update* existing entries (e.g.
        # StreamerSettingsPopup._save(), _process_streamer_schedules()) will
        # silently find nothing to work with until the user happens to
        # reorder or bypass a streamer.  Seed it immediately so scheduling
        # works out of the box.
        needs_seed    = self._config_id not in priorities_block
        saved_block   = priorities_block.get(self._config_id, {})
        saved_entries = saved_block.get("entries", [])

        if needs_seed:
            _dbg(
                f"[CONFIG][DIAG] needs_seed=True for config_id={self._config_id!r} "
                f"(known priorities keys: {list(priorities_block.keys())}) — "
                f"about to write DEFAULTS for {len(raw)} streamer(s), which will "
                f"OVERWRITE any existing priority/bypass ordering for this config_id "
                f"if it existed under a different key. config_paths={config_paths!r}"
            )

        # Build a lookup: (streamer, site) → saved dict
        saved_map: "dict[tuple,dict]" = {}
        for i, e in enumerate(saved_entries):
            key = (e.get("streamer", ""), e.get("site", ""))

            schedule_enabled = bool(e.get("schedule", {}).get("enabled", False))

            # Split: prefer the new tri-state "split_mode" ("on" | "off");
            # an "inherit" mode is represented by the key being absent.
            # Fall back to interpreting legacy pre-tri-state data, where the
            # only overridable state was "enabled with a positive minute
            # value" — anything else meant inherit (there was no "force off").
            split_mode = e.get("split_mode")
            if split_mode is None:
                legacy_split_enabled = bool(e.get("split_enabled", False))
                legacy_split_after   = int(e.get("split_after", 0) or 0)
                has_split_override = legacy_split_enabled and legacy_split_after > 0
            else:
                has_split_override = split_mode in ("on", "off")

            # Notifications: presence of the key at all (regardless of
            # True/False) means an explicit streamer-level choice was made.
            has_notif_override = e.get("notifications_enabled") is not None

            has_lq_override = bool(e.get("lq_enabled", False))

            saved_map[key] = {
                "bypass":       e.get("bypass", False),
                "priority":     i,
                "has_override": (schedule_enabled or has_split_override
                                  or has_notif_override or has_lq_override),
            }

        # Build enriched list with saved priority / bypass values.
        enriched = []
        for (streamer, site_label, config_path, config_sha) in raw:
            key      = (streamer, site_label)
            saved    = saved_map.get(key, {"bypass": False, "priority": 999999})
            enriched.append({
                "streamer":     streamer,
                "site":         site_label,
                "config_path":  config_path,
                "config_sha":   config_sha,
                "bypass":       saved["bypass"],
                "has_override": saved.get("has_override", False),
                "priority":     saved["priority"],
            })

        # Sort: bypass entries first (by saved order), then normal entries (by saved order).
        bypass_part = sorted([e for e in enriched if     e["bypass"]], key=lambda x: x["priority"])
        normal_part = sorted([e for e in enriched if not e["bypass"]], key=lambda x: x["priority"])

        self._entries = [
            PriorityEntry(
                streamer     = e["streamer"],
                site         = e["site"],
                config_path  = e["config_path"],
                config_sha   = e["config_sha"],
                bypass       = e["bypass"],
                has_override = e["has_override"],
            )
            for e in (bypass_part + normal_part)
        ]

        # Clamp selection.
        if self._entries:
            self._selected_idx = min(self._selected_idx, len(self._entries) - 1)
        else:
            self._selected_idx = 0

        # Seed global.json for a config_id we've never saved before, so that
        # everything downstream (schedule popup, scheduler loop) has a real
        # entries list to work with immediately, rather than only after the
        # user manually reorders/bypasses a streamer.
        if needs_seed and self._entries:
            self._save()

    def _save(self) -> None:
        """Persist current entry ordering and bypass flags to global.json.
        
        Existing per-entry data (e.g. schedule settings) is preserved so that
        reordering or toggling bypass never wipes schedule configuration.
        """
        if not self._config_id:
            return
        config_paths = [site.config_path for site in self.dashboard.sites]
        from .main import _global_json_lock, _load_global_json, _save_global_json
        with _global_json_lock:
            global_data = _load_global_json()
            if "priorities" not in global_data or not isinstance(global_data["priorities"], dict):
                global_data["priorities"] = {}
            # Build a lookup of any extra fields already stored (e.g. schedule)
            # so we can carry them forward rather than losing them on every save.
            existing_entries = (global_data["priorities"]
                                .get(self._config_id, {})
                                .get("entries", []))
            existing_map: dict = {}
            for ex in existing_entries:
                key = (ex.get("streamer", ""), ex.get("site", ""))
                existing_map[key] = ex

            entries_data = []
            for i, e in enumerate(self._entries):
                ex = existing_map.get((e.streamer, e.site), {})
                entry_dict: dict = {
                    "streamer":   e.streamer,
                    "site":       e.site,
                    "config_sha": e.config_sha,
                    "priority":   i,
                    "bypass":     e.bypass,
                }
                # Preserve schedule / split / notifications / LQ overrides
                # (and any future extra fields) so reordering or toggling
                # bypass never wipes a streamer-level override. This was
                # previously only done for "schedule" and "lq_enabled",
                # which silently dropped Split and Notifications overrides
                # on the next reorder or bypass toggle.
                for extra_key in ("schedule", "lq_enabled",
                                  "split_mode", "split_after", "split_enabled",
                                  "notifications_enabled"):
                    if extra_key in ex:
                        entry_dict[extra_key] = ex[extra_key]
                entries_data.append(entry_dict)

            global_data["priorities"][self._config_id] = {
                "config_files": config_paths,
                "entries":      entries_data,
            }
            _save_global_json(global_data)

        # Invalidate the sort manager's priority cache so the panel re-sorts immediately.
        try:
            sort_mgr = getattr(self.dashboard, "sort_manager", None)
            if sort_mgr is not None:
                sort_mgr._prio_cache_ts = 0.0
        except Exception:
            pass

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
        new_e   = PriorityEntry(e.streamer, e.site, e.config_path, e.config_sha, not e.bypass, e.has_override)
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

        # If the settings popup is open, route all keys into it.
        if self._settings_popup is not None:
            should_close = self._settings_popup.handle_key(key)
            if should_close:
                self._settings_popup = None
                self.force_reload()  # Refresh entries so the override "*" marker updates.
            return True

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
        elif key in (10, 13, curses.KEY_ENTER, 459):  # Enter / Return
            if self._entries and 0 <= self._selected_idx < len(self._entries):
                self._settings_popup = StreamerSettingsPopup(
                    self.dashboard,
                    self._entries[self._selected_idx],
                    self._config_id,
                )
            return True
        return False

    # ── Drawing ────────────────────────────────────────────────────────────────

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int, is_active: bool) -> None:
        """Draw the PRIORITY panel inside the box (y1,x1)–(y2,x2)."""
        self.ensure_loaded()
        db = self.dashboard

        # Scroll calculation moved up for title arrows
        visible_rows = max(0, y2 - (y1 + 8))
        if self._entries:
            if self._selected_idx < self._scroll_offset:
                self._scroll_offset = self._selected_idx
            elif self._selected_idx >= self._scroll_offset + visible_rows:
                self._scroll_offset = max(0, self._selected_idx - visible_rows + 1)

        # Box border
        db.draw_box(stdscr, y1, x1, y2, x2, db.C_SYSTEM)
        title = " STREAMER SETTINGS "
        
        db.safe_addstr(stdscr, y1, x1 + 2, title,
                       curses.color_pair(db.C_LIVE) | curses.A_BOLD)
        if is_active:
            mode_str = " [  ] "
            db.safe_addstr(stdscr, y1, x2 - len(mode_str) - 1, mode_str,
                           curses.color_pair(db.C_LIVE) | curses.A_BOLD)

        row_y = y1 + 1
        hints = [
            "↑↓:Navigation",
            "U:Increase Priority",
            "D:Decrease Priority",
            "B:Enable Bypass",
            "Enter:More Settings"
        ]
        for hint in hints:
            db.safe_addstr(stdscr, row_y, x1 + 2, hint, curses.color_pair(db.C_DIM))
            row_y += 1
        
        row_y += 2

        if not self._entries:
            db.safe_addstr(stdscr, row_y, x1 + 2, "No streamers.",
                           curses.color_pair(db.C_DIM))
            return

        # usable character columns inside box (reduced by 1 to guarantee space for the arrow)
        panel_inner_w = (x2 - x1) - 4   

        loop_end = min(len(self._entries), self._scroll_offset + visible_rows)
        for i in range(self._scroll_offset, loop_end):
            entry  = self._entries[i]
            is_sel = is_active and (i == self._selected_idx)

            # "*" marks a streamer with ANY streamer-level override active
            # (Schedule, Split, Notifications, or Quality/LQ) — i.e. one or
            # more settings for this streamer take precedence over the
            # site-level equivalent.
            streamer_display = f"*{entry.streamer}" if entry.has_override else entry.streamer
            label = f"{streamer_display}:{entry.site}"
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

            # --- Add Scroll Arrows ---
            if i == self._scroll_offset and self._scroll_offset > 0:
                db.safe_addstr(stdscr, row_y, x2 - 2, "\u25b2", curses.color_pair(db.C_LIVE) | curses.A_BOLD)
            if i == loop_end - 1 and loop_end < len(self._entries):
                db.safe_addstr(stdscr, row_y, x2 - 2, "\u25bc", curses.color_pair(db.C_LIVE) | curses.A_BOLD)

            row_y += 1



# ══════════════════════════════════════════════════════════════════════════════
# Streamer Settings Popup
# ══════════════════════════════════════════════════════════════════════════════

class StreamerSettingsPopup:
    """Main settings menu for a streamer.

    Opened by PriorityEditor when the user presses Enter on a streamer.
    """
    def __init__(self, dashboard, entry: "PriorityEntry", config_id: str):
        self.dashboard = dashboard
        self.entry     = entry
        self.config_id = config_id

        self.options = ["Schedule", "Quality", "Split", "Notifications"]
        self._sel: int = 0
        self._schedule_popup: "Optional[ScheduleSettingsPopup]" = None
        self._quality_popup: "Optional[QualitySettingsPopup]" = None
        self._split_popup: "Optional[SplitSettingsPopup]" = None
        self._notifications_popup: "Optional[NotificationSettingsPopup]" = None

    def handle_key(self, key) -> bool:
        if self._schedule_popup is not None:
            should_close = self._schedule_popup.handle_key(key)
            if should_close:
                self._schedule_popup = None
                return True
            return False

        if self._quality_popup is not None:
            should_close = self._quality_popup.handle_key(key)
            if should_close:
                self._quality_popup = None
                return True
            return False

        if self._split_popup is not None:
            should_close = self._split_popup.handle_key(key)
            if should_close:
                self._split_popup = None
                return True
            return False

        if self._notifications_popup is not None:
            should_close = self._notifications_popup.handle_key(key)
            if should_close:
                self._notifications_popup = None
                return True
            return False

        if key == 27:  # Esc
            return True
        elif key == curses.KEY_UP:
            self._sel = max(0, self._sel - 1)
        elif key == curses.KEY_DOWN:
            self._sel = min(len(self.options) - 1, self._sel + 1)
        elif key in (10, 13, curses.KEY_ENTER, 459, ord(' ')):
            if self.options[self._sel] == "Schedule":
                self._schedule_popup = ScheduleSettingsPopup(
                    self.dashboard,
                    self.entry,
                    self.config_id,
                )
            elif self.options[self._sel] == "Quality":
                self._quality_popup = QualitySettingsPopup(
                    self.dashboard,
                    self.entry,
                    self.config_id,
                )
            elif self.options[self._sel] == "Split":
                self._split_popup = SplitSettingsPopup(
                    self.dashboard,
                    self.entry,
                    self.config_id,
                )
            elif self.options[self._sel] == "Notifications":
                self._notifications_popup = NotificationSettingsPopup(
                    self.dashboard,
                    self.entry,
                    self.config_id,
                )
        return False

    def draw(self, stdscr) -> None:
        if self._schedule_popup is not None:
            self._schedule_popup.draw(stdscr)
            return

        if self._quality_popup is not None:
            self._quality_popup.draw(stdscr)
            return

        if self._split_popup is not None:
            self._split_popup.draw(stdscr)
            return

        if self._notifications_popup is not None:
            self._notifications_popup.draw(stdscr)
            return

        db = self.dashboard
        h, w = stdscr.getmaxyx()
        
        box_w = min(40, w - 6)
        box_h = max(len(self.options) * 2 + 4, 7)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w
        
        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(db.C_NORMAL))
            
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_SYSTEM)
        title = f" {self.entry.streamer.upper()} SETTINGS "
        db.safe_addstr(stdscr, by1, bx1 + 2, title, curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)
        
        row = by1 + 2
        for i, opt in enumerate(self.options):
            is_sel = (i == self._sel)
            prefix = "> " if is_sel else "  "
            attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD) if is_sel else (curses.color_pair(db.C_WARN) | curses.A_BOLD)
            db.safe_addstr(stdscr, row, bx1 + 2, prefix + opt, attr)
            row += 2
            
        db.safe_addstr(stdscr, by2, bx1 + 2, " Enter:Select  Esc:Cancel "[:box_w-4], curses.color_pair(db.C_INVHEAD))


class QualitySettingsPopup:
    def __init__(self, dashboard, entry: "PriorityEntry", config_id: str):
        self.dashboard = dashboard
        self.entry     = entry
        self.config_id = config_id
        self.lq_enabled = False
        self._load()

    def _load(self) -> None:
        try:
            from .main import _global_json_lock, _load_global_json
            with _global_json_lock:
                gdata = _load_global_json()
            entries = (gdata.get("priorities", {})
                           .get(self.config_id, {})
                           .get("entries", []))
            for e in entries:
                if (e.get("streamer") == self.entry.streamer
                        and e.get("site") == self.entry.site):
                    self.lq_enabled = bool(e.get("lq_enabled", False))
                    break
        except Exception:
            pass

    def _save(self) -> None:
        try:
            from .main import _global_json_lock, _load_global_json, _save_global_json
            with _global_json_lock:
                gdata   = _load_global_json()
                entries = (gdata.get("priorities", {})
                               .get(self.config_id, {})
                               .get("entries", []))
                target = None
                for e in entries:
                    if (e.get("streamer") == self.entry.streamer
                            and e.get("site") == self.entry.site):
                        target = e
                        break
                if target is None:
                    target = {
                        "streamer":   self.entry.streamer,
                        "site":       self.entry.site,
                        "config_sha": self.entry.config_sha,
                        "priority":   len(entries),
                        "bypass":     self.entry.bypass,
                    }
                    entries.append(target)
                target["lq_enabled"] = self.lq_enabled

                gdata.setdefault("priorities", {}).setdefault(
                    self.config_id, {"config_files": [], "entries": []}
                )["entries"] = entries
                _save_global_json(gdata)
        except Exception:
            pass

    def handle_key(self, key) -> bool:
        if key == 27:
            return True
        elif key == ord(' '):
            self.lq_enabled = not self.lq_enabled
        elif key in (10, 13, curses.KEY_ENTER, 459):
            self._save()
            return True
        return False

    def draw(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        
        box_w = min(40, w - 6)
        box_h = 7
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w
        
        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(db.C_NORMAL))
            
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_SYSTEM)
        title = f" {self.entry.streamer.upper()} SETTINGS "
        db.safe_addstr(stdscr, by1, bx1 + 2, title, curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)
        
        val_str = "[x]" if self.lq_enabled else "[ ]"
        db.safe_addstr(stdscr, by1 + 2, bx1 + 2, "> Low Quality Enabled: ", curses.color_pair(db.C_HILIGHT) | curses.A_BOLD)
        db.safe_addstr(stdscr, by1 + 2, bx1 + 25, val_str, curses.color_pair(db.C_HILIGHT) | curses.A_BOLD)
            
        db.safe_addstr(stdscr, by2, bx1 + 2, " Enter:Save  Space:Toggle  Esc:Cancel "[:box_w-4], curses.color_pair(db.C_INVHEAD))


class NotificationSettingsPopup:
    """Per-streamer override for ntfy.sh push notifications.

    Tri-state: "inherit" (default — use the site's NTFY_NOTIFICATIONS
    value, same as if no streamer-level entry existed at all), "on", or
    "off". Only "on"/"off" are ever written to global.json; "inherit" is
    represented by the *absence* of the "notifications_enabled" key, which
    is exactly what main.py's _resolve_ntfy_enabled() (called from
    _maybe_show_live_popup()) already checks for (streamer_notif is None →
    fall back to site config). This popup used to
    default to True on every load, which meant simply opening it and
    pressing Enter would silently write an explicit "true" override for a
    streamer that never had one — that's fixed by defaulting to inherit.
    """

    _STATES = ("inherit", "on", "off")

    def __init__(self, dashboard, entry: "PriorityEntry", config_id: str):
        self.dashboard = dashboard
        self.entry     = entry
        self.config_id = config_id
        self.state: str = "inherit"   # "inherit" | "on" | "off"
        self._load()

    def _load(self) -> None:
        try:
            from .main import _global_json_lock, _load_global_json
            with _global_json_lock:
                gdata = _load_global_json()
            entries = (gdata.get("priorities", {})
                           .get(self.config_id, {})
                           .get("entries", []))
            for e in entries:
                if (e.get("streamer") == self.entry.streamer
                        and e.get("site") == self.entry.site):
                    raw = e.get("notifications_enabled", None)
                    if raw is None:
                        self.state = "inherit"
                    else:
                        self.state = "on" if bool(raw) else "off"
                    break
        except Exception:
            pass

    def _save(self) -> None:
        try:
            from .main import _global_json_lock, _load_global_json, _save_global_json
            with _global_json_lock:
                gdata   = _load_global_json()
                entries = (gdata.get("priorities", {})
                               .get(self.config_id, {})
                               .get("entries", []))
                target = None
                for e in entries:
                    if (e.get("streamer") == self.entry.streamer
                            and e.get("site") == self.entry.site):
                        target = e
                        break
                if self.state == "inherit":
                    # Nothing to override — remove any prior explicit value
                    # rather than writing one, so this streamer stops
                    # showing up as having a Notifications override.
                    if target is not None:
                        target.pop("notifications_enabled", None)
                else:
                    if target is None:
                        target = {
                            "streamer":   self.entry.streamer,
                            "site":       self.entry.site,
                            "config_sha": self.entry.config_sha,
                            "priority":   len(entries),
                            "bypass":     self.entry.bypass,
                        }
                        entries.append(target)
                    target["notifications_enabled"] = (self.state == "on")

                gdata.setdefault("priorities", {}).setdefault(
                    self.config_id, {"config_files": [], "entries": []}
                )["entries"] = entries
                _save_global_json(gdata)
        except Exception:
            pass

    def _site_default(self) -> bool:
        cfg = _get_site_default_cfg(self.dashboard, self.entry)
        return bool(cfg.get("ntfy_notifications", True))

    def handle_key(self, key) -> bool:
        if key == 27:
            return True
        elif key == ord(' '):
            idx = self._STATES.index(self.state)
            self.state = self._STATES[(idx + 1) % len(self._STATES)]
        elif key == curses.KEY_LEFT:
            idx = self._STATES.index(self.state)
            self.state = self._STATES[(idx - 1) % len(self._STATES)]
        elif key == curses.KEY_RIGHT:
            idx = self._STATES.index(self.state)
            self.state = self._STATES[(idx + 1) % len(self._STATES)]
        elif key in (10, 13, curses.KEY_ENTER, 459):
            self._save()
            return True
        return False

    def draw(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        
        box_w = min(46, w - 6)
        box_h = 7
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w
        
        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(db.C_NORMAL))
            
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_SYSTEM)
        title = f" {self.entry.streamer.upper()} SETTINGS "
        db.safe_addstr(stdscr, by1, bx1 + 2, title, curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)

        state_label = {"inherit": "< Inherit >", "on": "< On >", "off": "< Off >"}[self.state]
        db.safe_addstr(stdscr, by1 + 2, bx1 + 2, "> ntfy Notifications: ", curses.color_pair(db.C_HILIGHT) | curses.A_BOLD)
        db.safe_addstr(stdscr, by1 + 2, bx1 + 24, state_label, curses.color_pair(db.C_HILIGHT) | curses.A_BOLD)

        site_default = self._site_default()
        if self.state == "inherit":
            effective = site_default
        else:
            effective = (self.state == "on")
        eff_str = "ON" if effective else "OFF"
        db.safe_addstr(stdscr, by1 + 4, bx1 + 2, "Effective: ", curses.color_pair(db.C_NORMAL))
        db.safe_addstr(stdscr, by1 + 4, bx1 + 13, eff_str, curses.color_pair(db.C_WARN) | curses.A_BOLD)

        db.safe_addstr(stdscr, by2, bx1 + 2, " Enter:Save  Space/\u2190\u2192:Cycle  Esc:Cancel "[:box_w-4], curses.color_pair(db.C_INVHEAD))


class SplitSettingsPopup:
    """Modal popup for per-streamer split-after-X-minutes settings.

    Opened by StreamerSettingsPopup when the user presses Enter on Split.
    Data is stored inside the existing priorities[config_id][entries]
    structure in global.json, alongside lq_enabled/schedule — no new
    top-level key is created.

    Tri-state "Split" field:
      - "inherit" (default) — no override; this streamer uses the site's
        SPLIT_AFTER value, same as if it had no entry at all.
      - "on"  — override with a custom per-streamer minute value (requires
        Minutes > 0).
      - "off" — force splitting OFF for this streamer even if the site has
        SPLIT_AFTER set to a positive value.
    "inherit" is represented in global.json by the absence of "split_mode"
    (and, going forward, "split_enabled"). See _resolve_split_after() in
    main.py for the resolution logic applied at record start, which also
    still understands the old pre-tri-state "split_enabled"/"split_after"
    fields written by earlier versions of this popup.
    """

    _FIELD_MODE    = "split_mode"
    _FIELD_MINUTES = "split_after"
    _STATES = ("inherit", "on", "off")

    def __init__(self, dashboard, entry: "PriorityEntry", config_id: str):
        self.dashboard = dashboard
        self.entry     = entry
        self.config_id = config_id

        self.mode:        str  = "inherit"   # "inherit" | "on" | "off"
        self.split_after: int  = 0

        self._sel:      int  = 0      # 0 = mode row, 1 = minutes row (when shown)
        self._editing:  bool = False  # text-field edit sub-mode
        self._edit_buf: str  = ""
        self._error:    str  = ""

        self._load()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            from .main import _global_json_lock, _load_global_json
            with _global_json_lock:
                gdata = _load_global_json()
            entries = (gdata.get("priorities", {})
                           .get(self.config_id, {})
                           .get("entries", []))
            for e in entries:
                if (e.get("streamer") == self.entry.streamer
                        and e.get("site") == self.entry.site):
                    try:
                        self.split_after = max(0, int(e.get("split_after", 0) or 0))
                    except (TypeError, ValueError):
                        self.split_after = 0

                    raw_mode = e.get("split_mode")
                    if raw_mode in ("on", "off"):
                        self.mode = raw_mode
                    elif raw_mode is None:
                        # Legacy data written by the old two-state popup:
                        # enabled + minutes > 0 meant an override; anything
                        # else meant inherit (there was no "force off").
                        legacy_enabled = bool(e.get("split_enabled", False))
                        self.mode = "on" if (legacy_enabled and self.split_after > 0) else "inherit"
                    else:
                        self.mode = "inherit"
                    break
        except Exception:
            pass

    def _save(self) -> None:
        try:
            from .main import _global_json_lock, _load_global_json, _save_global_json
            with _global_json_lock:
                gdata   = _load_global_json()
                entries = (gdata.get("priorities", {})
                               .get(self.config_id, {})
                               .get("entries", []))
                target = None
                for e in entries:
                    if (e.get("streamer") == self.entry.streamer
                            and e.get("site") == self.entry.site):
                        target = e
                        break
                if self.mode == "inherit":
                    # Nothing to override — clear any prior explicit value
                    # (new or legacy) rather than writing one.
                    if target is not None:
                        target.pop("split_mode", None)
                        target.pop("split_enabled", None)
                        target.pop("split_after", None)
                else:
                    if target is None:
                        target = {
                            "streamer":   self.entry.streamer,
                            "site":       self.entry.site,
                            "config_sha": self.entry.config_sha,
                            "priority":   len(entries),
                            "bypass":     self.entry.bypass,
                        }
                        entries.append(target)
                    target["split_mode"] = self.mode
                    target["split_after"] = self.split_after if self.mode == "on" else 0
                    target.pop("split_enabled", None)  # fully migrated to split_mode

                gdata.setdefault("priorities", {}).setdefault(
                    self.config_id, {"config_files": [], "entries": []}
                )["entries"] = entries
                _save_global_json(gdata)
        except Exception:
            pass

    def _site_default_minutes(self) -> int:
        cfg = _get_site_default_cfg(self.dashboard, self.entry)
        try:
            return max(0, int(cfg.get("split_after", 0) or 0))
        except (TypeError, ValueError):
            return 0

    # ── Validation ─────────────────────────────────────────────────────────────

    def _validate(self) -> "tuple[bool, str]":
        if self.mode == "on" and self.split_after <= 0:
            return False, "Enter a split time > 0 minutes"
        return True, ""

    # ── Field list ─────────────────────────────────────────────────────────────

    def _get_fields(self) -> "list[tuple[str,str,str]]":
        mode_label = {"inherit": "< Inherit >", "on": "< On >", "off": "< Off >"}[self.mode]
        fields = [("Split", mode_label, self._FIELD_MODE)]
        if self.mode == "on":
            fields.append((
                "Split after X minutes",
                str(self.split_after) if self.split_after else "",
                self._FIELD_MINUTES,
            ))
        return fields

    # ── Key handling ───────────────────────────────────────────────────────────

    def handle_key(self, key) -> bool:
        """Handle one keypress. Returns True when the popup should close."""
        fields = self._get_fields()
        _, _, field_key = fields[self._sel]

        # ── Text-editing sub-mode (minutes field) ───────────────────────────────
        if self._editing:
            if key == 27:                               # Esc → cancel edit
                self._editing  = False
                self._edit_buf = ""
                self._error    = ""
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self._edit_buf = self._edit_buf[:-1]
                self._error    = ""
            elif key in (ord("\n"), ord("\r"), curses.KEY_ENTER, 459):
                val = self._edit_buf.strip()
                if val == "":
                    self.split_after = 0
                    self._editing  = False
                    self._edit_buf = ""
                    self._error    = ""
                elif val.isdigit() and int(val) > 0:
                    self.split_after = int(val)
                    self._editing  = False
                    self._edit_buf = ""
                    self._error    = ""
                else:
                    self._error = "Enter a whole number of minutes"
            elif 48 <= key <= 57:                        # digits only
                self._edit_buf += chr(key)
                self._error     = ""
            return False

        # ── Normal navigation ─────────────────────────────────────────────────
        if key == 27:                                   # Esc → close without saving
            return True

        elif key == curses.KEY_UP:
            self._sel   = max(0, self._sel - 1)
            self._error = ""

        elif key == curses.KEY_DOWN:
            self._sel   = min(len(fields) - 1, self._sel + 1)
            self._error = ""

        elif key in (ord(" "), curses.KEY_LEFT, curses.KEY_RIGHT):
            if field_key == self._FIELD_MODE:
                idx = self._STATES.index(self.mode)
                step = -1 if key == curses.KEY_LEFT else 1
                self.mode = self._STATES[(idx + step) % len(self._STATES)]
                self._sel = min(self._sel, len(self._get_fields()) - 1)
                self._error = ""
            elif key == ord(" "):
                self._edit_buf = str(self.split_after) if self.split_after else ""
                self._editing  = True
                self._error    = ""

        elif key in (ord("\n"), ord("\r"), curses.KEY_ENTER, 459):
            valid, err = self._validate()
            if valid:
                self._save()
                return True
            self._error = err

        return False

    # ── Drawing ────────────────────────────────────────────────────────────────

    def draw(self, stdscr) -> None:
        db     = self.dashboard
        h, w   = stdscr.getmaxyx()
        fields = self._get_fields()

        box_w = min(50, w - 6)
        box_h = len(fields) * 2 + 5   # + effective line + footer
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(db.C_NORMAL))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_SYSTEM)
        title = f" {self.entry.streamer.upper()} SETTINGS "
        db.safe_addstr(stdscr, by1, bx1 + 2, title, curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)

        row = by1 + 2
        for i, (label, val_str, field_key) in enumerate(fields):
            is_sel     = (i == self._sel)
            prefix     = "> " if is_sel else "  "
            label_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                          if is_sel else curses.color_pair(db.C_WARN) | curses.A_BOLD)
            val_attr   = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                          if is_sel else curses.color_pair(db.C_NORMAL))

            full_label = f"{prefix}{label}: "
            db.safe_addstr(stdscr, row, bx1 + 2, full_label, label_attr)

            if field_key == self._FIELD_MINUTES and self._editing and is_sel:
                shown = self._edit_buf + "_"
            else:
                shown = val_str
            val_x   = bx1 + 2 + len(full_label)
            max_len = max(1, bx2 - val_x - 1)
            db.safe_addstr(stdscr, row, val_x, shown[:max_len], val_attr)

            row += 2

        # ── Effective value ───────────────────────────────────────────────────
        site_minutes = self._site_default_minutes()
        if self.mode == "inherit":
            effective_str = f"{site_minutes}m" if site_minutes > 0 else "No split"
        elif self.mode == "off":
            effective_str = "No split"
        else:
            effective_str = f"{self.split_after}m" if self.split_after > 0 else "No split"
        db.safe_addstr(stdscr, row, bx1 + 2, "Effective: ", curses.color_pair(db.C_NORMAL))
        db.safe_addstr(stdscr, row, bx1 + 13, effective_str, curses.color_pair(db.C_WARN) | curses.A_BOLD)
        row += 1

        if self._error:
            db.safe_addstr(stdscr, by2 - 1, bx1 + 2, self._error[:box_w - 4],
                           curses.color_pair(db.C_WARN) | curses.A_BOLD)

        footer = " Enter:Save  Space/\u2190\u2192:Cycle  Esc:Cancel "
        db.safe_addstr(stdscr, by2, bx1 + 2, footer[:box_w - 4], curses.color_pair(db.C_INVHEAD))


class ScheduleSettingsPopup:
    """Modal popup for per-streamer schedule settings.

    Opened by StreamerSettingsPopup when the user presses Enter on Schedule.
    All data is stored inside the existing priorities[config_id][entries]
    structure in global.json — no new top-level key is created.
    """

    _DATETIME_FMT = "%Y-%m-%d %H:%M"
    _TIME_FMT     = "%H:%M"
    _DAY_LABELS   = ["M", "T", "W", "T", "F", "S", "S"]

    # Field keys used internally
    _FIELD_ENABLED   = "schedule_enabled"
    _FIELD_MODE      = "mode"
    _FIELD_OO_START  = "one_off_start"
    _FIELD_OO_END    = "one_off_end"
    _FIELD_REC_DAYS  = "recurring_days"
    _FIELD_REC_START = "recurring_start"
    _FIELD_REC_END   = "recurring_end"

    def __init__(self, dashboard, entry: "PriorityEntry", config_id: str):
        self.dashboard = dashboard
        self.entry     = entry
        self.config_id = config_id

        # Working copies of schedule settings
        self.schedule_enabled: bool      = False
        self.mode:             str       = "one_off"    # "one_off" | "recurring"
        self.one_off_start:    str       = ""
        self.one_off_end:      str       = ""
        self.recurring_days:   list      = [False] * 7  # Mon–Sun
        self.recurring_start:  str       = ""
        self.recurring_end:    str       = ""

        # UI state
        self._sel:        int  = 0      # selected field index
        self._editing:    bool = False  # text-field edit sub-mode
        self._edit_buf:   str  = ""
        self._day_cursor: int  = 0      # sub-cursor within the Days row
        self._error:      str  = ""

        self._load()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load saved schedule settings from global.json into working state."""
        try:
            from .main import _global_json_lock, _load_global_json
            with _global_json_lock:
                gdata = _load_global_json()
            entries = (gdata.get("priorities", {})
                           .get(self.config_id, {})
                           .get("entries", []))
            for e in entries:
                if (e.get("streamer") == self.entry.streamer
                        and e.get("site") == self.entry.site):
                    sched = e.get("schedule", {})
                    self.schedule_enabled = bool(sched.get("enabled", False))
                    self.mode             = sched.get("mode", "one_off")
                    oo  = sched.get("one_off", {})
                    self.one_off_start    = oo.get("start", "")
                    self.one_off_end      = oo.get("end",   "")
                    rec = sched.get("recurring", {})
                    days_list             = rec.get("days", [])
                    self.recurring_days   = [(i in days_list) for i in range(7)]
                    self.recurring_start  = rec.get("start_time", "")
                    self.recurring_end    = rec.get("end_time",   "")
                    break
        except Exception:
            pass

    def _save(self) -> None:
        """Write current working state back to global.json under priorities[…][entries]."""
        try:
            from .main import _global_json_lock, _load_global_json, _save_global_json
            with _global_json_lock:
                gdata   = _load_global_json()
                entries = (gdata.get("priorities", {})
                               .get(self.config_id, {})
                               .get("entries", []))
                target = None
                for e in entries:
                    if (e.get("streamer") == self.entry.streamer
                            and e.get("site") == self.entry.site):
                        target = e
                        break
                if target is None:
                    # No pre-existing entry (e.g. a fresh clone where the
                    # PRIORITY panel's seed hasn't run yet, or this streamer
                    # was added after the last seed/save). Create one rather
                    # than silently dropping the schedule the user just set.
                    target = {
                        "streamer":   self.entry.streamer,
                        "site":       self.entry.site,
                        "config_sha": self.entry.config_sha,
                        "priority":   len(entries),
                        "bypass":     self.entry.bypass,
                    }
                    entries.append(target)
                sched = target.setdefault("schedule", {})
                sched["enabled"] = self.schedule_enabled
                sched["mode"]    = self.mode
                sched.setdefault("one_off", {}).update({
                    "start": self.one_off_start,
                    "end":   self.one_off_end,
                })
                sched.setdefault("recurring", {}).update({
                    "days":       [i for i, v in enumerate(self.recurring_days) if v],
                    "start_time": self.recurring_start,
                    "end_time":   self.recurring_end,
                })
                # last_enable_attempt / last_disable_attempt are managed by
                # the scheduling engine; never overwrite them here.

                gdata.setdefault("priorities", {}).setdefault(
                    self.config_id, {"config_files": [], "entries": []}
                )["entries"] = entries
                _save_global_json(gdata)
        except Exception:
            pass

    # ── Field list (dynamic based on mode) ────────────────────────────────────

    def _get_fields(self) -> "list[tuple[str,str,str]]":
        """Return list of (label, display_value, field_key) for the current mode."""
        fields = [
            ("Schedule Enabled",
             "[x]" if self.schedule_enabled else "[ ]",
             self._FIELD_ENABLED),
            ("Mode",
             "< One-Off >" if self.mode == "one_off" else "< Recurring >",
             self._FIELD_MODE),
        ]
        if self.mode == "one_off":
            fields += [
                ("Start Datetime",
                 self.one_off_start or "YYYY-MM-DD HH:MM",
                 self._FIELD_OO_START),
                ("End Datetime",
                 self.one_off_end or "YYYY-MM-DD HH:MM",
                 self._FIELD_OO_END),
            ]
        else:
            days_disp = " ".join(
                f"[{lbl}]" if self.recurring_days[i] else f" {lbl} "
                for i, lbl in enumerate(self._DAY_LABELS)
            )
            fields += [
                ("Days",
                 days_disp,
                 self._FIELD_REC_DAYS),
                ("Start Time",
                 self.recurring_start or "HH:MM",
                 self._FIELD_REC_START),
                ("End Time",
                 self.recurring_end or "HH:MM",
                 self._FIELD_REC_END),
            ]
        return fields

    # ── Validation ─────────────────────────────────────────────────────────────

    def _validate(self) -> "tuple[bool, str]":
        if not self.schedule_enabled:
            return True, ""
        if self.mode == "one_off":
            for val, label in ((self.one_off_start, "Start"),
                               (self.one_off_end,   "End")):
                try:
                    datetime.strptime(val, self._DATETIME_FMT)
                except Exception:
                    return False, f"{label} must be YYYY-MM-DD HH:MM"
        else:
            if not any(self.recurring_days):
                return False, "Select at least one day"
            for val, label in ((self.recurring_start, "Start"),
                               (self.recurring_end,   "End")):
                try:
                    datetime.strptime(val, self._TIME_FMT)
                except Exception:
                    return False, f"{label} time must be HH:MM"
        return True, ""

    # ── Key handling ───────────────────────────────────────────────────────────

    def handle_key(self, key) -> bool:
        """Handle one keypress.  Returns True when the popup should close."""
        fields = self._get_fields()
        n      = len(fields)
        _, _, field_key = fields[self._sel] if fields else ("", "", "")

        # ── Text-editing sub-mode ─────────────────────────────────────────────
        if self._editing:
            if key == 27:                               # Esc → cancel edit
                self._editing  = False
                self._edit_buf = ""
                self._error    = ""
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self._edit_buf = self._edit_buf[:-1]
                self._error    = ""
            elif key in (ord("\n"), ord("\r"), curses.KEY_ENTER, 459):
                val = self._edit_buf.strip()
                fmt = (self._DATETIME_FMT
                       if field_key in (self._FIELD_OO_START, self._FIELD_OO_END)
                       else self._TIME_FMT)
                try:
                    datetime.strptime(val, fmt)
                    setattr(self, field_key, val)
                    self._editing  = False
                    self._edit_buf = ""
                    self._error    = ""
                except Exception:
                    expected = ("YYYY-MM-DD HH:MM"
                                if fmt == self._DATETIME_FMT else "HH:MM")
                    self._error = f"Use format: {expected}"
            elif 32 <= key < 127:
                self._edit_buf += chr(key)
                self._error     = ""
            return False

        # ── Normal navigation ─────────────────────────────────────────────────
        if key == 27:                                   # Esc → close without saving
            return True

        if key == curses.KEY_UP:
            self._sel   = max(0, self._sel - 1)
            self._error = ""

        elif key == curses.KEY_DOWN:
            self._sel   = min(n - 1, self._sel + 1)
            self._error = ""

        elif key == curses.KEY_LEFT:
            if field_key == self._FIELD_MODE:
                self.mode = "one_off"
                self._sel = min(self._sel, len(self._get_fields()) - 1)
            elif field_key == self._FIELD_REC_DAYS:
                self._day_cursor = max(0, self._day_cursor - 1)

        elif key == curses.KEY_RIGHT:
            if field_key == self._FIELD_MODE:
                self.mode = "recurring"
                self._sel = min(self._sel, len(self._get_fields()) - 1)
            elif field_key == self._FIELD_REC_DAYS:
                self._day_cursor = min(6, self._day_cursor + 1)

        elif key == ord(" "):
            if field_key in (self._FIELD_OO_START, self._FIELD_OO_END,
                             self._FIELD_REC_START, self._FIELD_REC_END):
                self._edit_buf = getattr(self, field_key, "")
                self._editing  = True
                self._error    = ""
            else:
                self._toggle_current(field_key, fields)

        elif key in (ord("\n"), ord("\r"), curses.KEY_ENTER, 459):
            valid, err = self._validate()
            if valid:
                self._save()
                return True
            self._error = err

        return False

    def _toggle_current(self, field_key: str, fields: list) -> None:
        """Toggle/cycle the currently selected field."""
        if field_key == self._FIELD_ENABLED:
            self.schedule_enabled = not self.schedule_enabled
            self._error = ""
        elif field_key == self._FIELD_MODE:
            self.mode = "recurring" if self.mode == "one_off" else "one_off"
            self._sel = min(self._sel, len(self._get_fields()) - 1)
            self._error = ""
        elif field_key == self._FIELD_REC_DAYS:
            self.recurring_days[self._day_cursor] = not self.recurring_days[self._day_cursor]
            self._error = ""

    # ── Drawing ────────────────────────────────────────────────────────────────

    def draw(self, stdscr) -> None:
        """Draw the popup centred on screen, on top of everything else."""
        db    = self.dashboard
        h, w  = stdscr.getmaxyx()
        fields = self._get_fields()

        box_w   = min(56, w - 6)
        # Two screen-rows per field (field line + blank gap), plus borders / header / footer.
        box_h   = len(fields) * 2 + 4
        box_h   = max(box_h, 8)
        box_h   = min(box_h, h - 4)

        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        # Clear background area
        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           curses.color_pair(db.C_NORMAL))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_SYSTEM)
        title = f" {self.entry.streamer.upper()} SETTINGS "
        db.safe_addstr(stdscr, by1, bx1 + 2, title,
                       curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)

        # Draw each field
        row = by1 + 2
        for i, (label, val_str, field_key) in enumerate(fields):
            if row >= by2 - 1:
                break
            is_sel     = (i == self._sel)
            prefix     = "> " if is_sel else "  "
            label_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                          if is_sel else curses.color_pair(db.C_WARN) | curses.A_BOLD)
            val_attr   = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                          if is_sel else curses.color_pair(db.C_NORMAL))

            full_label = f"{prefix}{label}: "
            db.safe_addstr(stdscr, row, bx1 + 2, full_label, label_attr)
            val_x   = bx1 + 2 + len(full_label)
            max_len = max(1, bx2 - val_x - 1)

            if field_key == self._FIELD_REC_DAYS and is_sel:
                # Render each day token individually so the sub-cursor can be highlighted.
                dx = val_x
                for di, day_lbl in enumerate(self._DAY_LABELS):
                    is_active = self.recurring_days[di]
                    is_dc     = (di == self._day_cursor)
                    day_str   = f"[{day_lbl}]" if is_active else f" {day_lbl} "
                    if is_dc:
                        day_attr = curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                    elif is_active:
                        day_attr = curses.color_pair(db.C_LIVE) | curses.A_BOLD
                    else:
                        day_attr = curses.color_pair(db.C_DIM)
                    if dx + len(day_str) < bx2:
                        db.safe_addstr(stdscr, row, dx, day_str, day_attr)
                    dx += len(day_str) + 1
            elif (is_sel and self._editing
                  and field_key in (self._FIELD_OO_START, self._FIELD_OO_END,
                                    self._FIELD_REC_START, self._FIELD_REC_END)):
                db.safe_addstr(stdscr, row, val_x,
                               (self._edit_buf + "_")[:max_len],
                               curses.color_pair(db.C_NORMAL) | curses.A_BOLD)
            else:
                db.safe_addstr(stdscr, row, val_x, val_str[:max_len], val_attr)

            row += 2  # blank line between fields for readability

        # Footer: error message or keybind hint
        if self._error:
            db.safe_addstr(stdscr, by2, bx1 + 2,
                           f" {self._error} "[:box_w - 4],
                           curses.color_pair(db.C_WARN) | curses.A_BOLD)
        else:
            if self._editing:
                hint = " Enter:Commit  Esc:Cancel edit "
            else:
                hint = " Enter:Save  Esc:Cancel  Space:Toggle/Edit  \u2190\u2192:Mode/Days "
            db.safe_addstr(stdscr, by2, bx1 + 2, hint[:box_w - 4],
                           curses.color_pair(db.C_INVHEAD))


def apply_sort_to_streamers(
    streamers:    "list[str]",
    sort_key:     str,
    live_since:   "dict[str, float]",
    last_live:    "dict[str, float]",
    priority_map: "dict[tuple, dict]",
    site_label:   str,
) -> "list[str]":
    """Return *streamers* reordered according to *sort_key*.

    ``live_since``   – streamer → epoch when they went live (absent if offline)
    ``last_live``    – streamer → epoch when last recording ended
    ``priority_map`` – (streamer, site_label) → {"priority": int, "bypass": bool}
    """
    if not streamers:
        return list(streamers)

    if sort_key == "added_first":
        return list(streamers)

    if sort_key == "added_last":
        return list(reversed(streamers))

    if sort_key == "alpha_asc":
        return sorted(streamers)

    if sort_key == "alpha_desc":
        return sorted(streamers, reverse=True)

    if sort_key == "last_live_asc":
        # Streamers never seen live sort to the end.
        def _key_ll_asc(s: str):
            ts = last_live.get(s)
            return (0, ts) if ts is not None else (1, 0.0)
        return sorted(streamers, key=_key_ll_asc)

    if sort_key == "last_live_desc":
        # Most recently live first; never-seen go last.
        def _key_ll_desc(s: str):
            ts = last_live.get(s)
            return (0, -(ts or 0.0)) if ts is not None else (1, 0.0)
        return sorted(streamers, key=_key_ll_desc)

    if sort_key == "priority_asc":
        def _key_pri_asc(s: str):
            return priority_map.get((s, site_label), {}).get("priority", 999999)
        return sorted(streamers, key=_key_pri_asc)

    if sort_key == "priority_desc":
        def _key_pri_desc(s: str):
            return priority_map.get((s, site_label), {}).get("priority", 999999)
        return sorted(streamers, key=_key_pri_desc, reverse=True)

    if sort_key == "live_first":
        live_set = set(live_since.keys())
        return [s for s in streamers if s in live_set] + \
               [s for s in streamers if s not in live_set]

    if sort_key == "live_last":
        live_set = set(live_since.keys())
        return [s for s in streamers if s not in live_set] + \
               [s for s in streamers if s in live_set]

    return list(streamers)


class SiteSortManager:
    """Manages the sort order for site panels in the Dashboard tab.

    Owns the sort-option popup, persists the chosen sort to global.conf,
    and exposes ``get_sorted_streamers()`` for use in ``draw_site_panel``.
    """

    _POPUP_TITLE = " SORT STREAMERS "

    def __init__(self, dashboard):
        self.dashboard       = dashboard
        self._current_sort:  str   = self._load_sort()
        self.popup_open:     bool  = False
        self._popup_sel:     int   = self._sort_idx(self._current_sort)
        self._popup_scroll:  int   = 0
        # Priority map cache (refreshed at most every 2 s)
        self._prio_cache:    dict  = {}
        self._prio_cache_ts: float = 0.0

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def current_sort(self) -> str:
        return self._current_sort

    @property
    def current_sort_label(self) -> str:
        return _SORT_LABELS.get(self._current_sort, self._current_sort)

    def open_popup(self) -> None:
        self._popup_sel    = self._sort_idx(self._current_sort)
        self._popup_scroll = 0
        self.popup_open    = True

    def close_popup(self) -> None:
        self.popup_open = False

    def get_sorted_streamers(
        self,
        site,
        streamers:  "list[str]",
        live_since: "dict[str, float]",
        last_live:  "dict[str, float]",
    ) -> "list[str]":
        """Return *streamers* ordered by the active sort option."""
        need_prio = self._current_sort in ("priority_asc", "priority_desc")
        priority_map = self._get_priority_map() if need_prio else {}
        cfg        = site.get_cached_config()
        site_label = cfg.get("site_label", os.path.basename(site.config_path))
        return apply_sort_to_streamers(
            streamers, self._current_sort, live_since, last_live,
            priority_map, site_label,
        )

    # ── Key handling ────────────────────────────────────────────────────────────

    def handle_key(self, key) -> bool:
        """Handle keys while the sort popup is open. Always returns True."""
        if not self.popup_open:
            return False
        n = len(SORT_OPTIONS)
        if key == 27:                                   # Esc → cancel
            self.close_popup()
        elif key == curses.KEY_UP:
            self._popup_sel = max(0, self._popup_sel - 1)
        elif key == curses.KEY_DOWN:
            self._popup_sel = min(n - 1, self._popup_sel + 1)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459, ord(' ')):
            new_key = _SORT_KEYS[self._popup_sel]
            if new_key != self._current_sort:
                self._current_sort = new_key
                self._save_sort(new_key)
            self.close_popup()
        # All other keys are consumed so nothing leaks to the dashboard.
        return True

    # ── Drawing ─────────────────────────────────────────────────────────────────

    def draw_popup(self, stdscr) -> None:
        """Draw the sort-option popup centred on the screen."""
        db   = self.dashboard
        h, w = stdscr.getmaxyx()
        n    = len(SORT_OPTIONS)

        box_w = min(36, w - 4)
        box_h = min(n + 4, h - 4)
        by1   = (h - box_h) // 2
        bx1   = (w - box_w) // 2
        by2   = by1 + box_h
        bx2   = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           curses.color_pair(db.C_NORMAL))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, self._POPUP_TITLE,
                       curses.color_pair(db.C_CHROME) | curses.A_BOLD)
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Select  Esc: Cancel ",
                       curses.color_pair(db.C_INVHEAD))

        visible = box_h - 3   # rows between border+title and legend row

        # Scroll to keep selection visible.
        if self._popup_sel < self._popup_scroll:
            self._popup_scroll = self._popup_sel
        elif self._popup_sel >= self._popup_scroll + visible:
            self._popup_scroll = self._popup_sel - visible + 1

        for i in range(self._popup_scroll, min(n, self._popup_scroll + visible)):
            sort_key, label = SORT_OPTIONS[i]
            row_y  = by1 + 1 + (i - self._popup_scroll)
            is_sel = (i == self._popup_sel)
            is_cur = (sort_key == self._current_sort)
            prefix = "> " if is_sel else ("* " if is_cur else "  ")
            if is_sel:
                attr = curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
            elif is_cur:
                attr = curses.color_pair(db.C_LIVE) | curses.A_BOLD
            else:
                attr = curses.color_pair(db.C_NORMAL)
            db.safe_addstr(stdscr, row_y, bx1 + 2,
                           (prefix + label)[:box_w - 4], attr)

    # ── Persistence ─────────────────────────────────────────────────────────────

    @staticmethod
    def _load_sort() -> str:
        """Read SITE_SORT from global.conf; returns SORT_DEFAULT on any error."""
        try:
            import configparser as _cp
            from .main import get_global_conf_path
            path   = get_global_conf_path()
            parser = _cp.ConfigParser(allow_no_value=True, interpolation=None, delimiters=('=',))
            parser.read(path, encoding="utf-8")
            general = parser["General"] if parser.has_section("General") else {}
            val     = general.get("SITE_SORT", SORT_DEFAULT).strip().lower()
            return val if val in _SORT_KEYS else SORT_DEFAULT
        except Exception:
            return SORT_DEFAULT

    def _save_sort(self, key: str) -> None:
        """Persist SITE_SORT to global.conf."""
        try:
            from .main import _write_global_conf_key
            _write_global_conf_key("SITE_SORT", key)
        except Exception:
            pass

    # ── Helpers ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _sort_idx(sort_key: str) -> int:
        try:
            return _SORT_KEYS.index(sort_key)
        except ValueError:
            return 0

    def _get_priority_map(self) -> dict:
        """Return the priority map, refreshing from global.json at most every 2 s."""
        import time as _time
        now = _time.time()
        if now - self._prio_cache_ts < 2.0:
            return self._prio_cache
        try:
            from .main import _global_json_lock, _load_global_json
            sites     = self.dashboard.sites
            config_id = _compute_config_id([s.config_path for s in sites])
            with _global_json_lock:
                global_data = _load_global_json()
            entries = (global_data.get("priorities", {})
                                  .get(config_id, {})
                                  .get("entries", []))
            pmap = {}
            for e in entries:
                k = (e.get("streamer", ""), e.get("site", ""))
                pmap[k] = {
                    "priority": e.get("priority", 999999),
                    "bypass":   e.get("bypass", False),
                }
            self._prio_cache    = pmap
            self._prio_cache_ts = now
        except Exception:
            pass
        return self._prio_cache


def _validate_value(key: str, value: str) -> tuple[bool, str]:
    """Validate config values based on their expected types."""
    bool_keys = {"DEBUG_LOGS", "CHECK_FOR_UPDATES", "ASK_FOR_BROWSER", "ASK_FOR_CONFIG",
                 "PANEL_RESIZE", "LOGGING", "SPLIT_LOGS", "POPUP_NOTIFICATIONS",
                 "DOWNLOADER_COOKIES", "CHECKER_COOKIES", "LQ_DOWNLOADER", "SUBFOLDERS",
                 "UPGRADE_QUALITY"}
    int_keys = {"UPDATE_INTERVAL", "SITE_ORDER", "CHECK_INTERVAL", "COOLDOWN_AFTER_RECORDING",
                "SPLIT_AFTER", "STALL_CHECK_INTERVAL", "STALL_TIMEOUT", "CONFIG_CHECK_INTERVAL",
                "POPUP_TIMEOUT", "POPUP_COOLDOWN", "PROGRESS_BAR_MAX_HOURS", "PROGRESS_BAR_WIDTH",
                "LAST_LIVE_HIGHLIGHT", "MAX_CONCURRENT_REC", "FF_ERR_THRESH"}
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
    if key == "SITE_SORT":
        if value.lower() not in _SORT_KEYS:
            return False, f"Must be one of: {', '.join(_SORT_KEYS)}"
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
        # ── Debug-tags popup state ─────────────────────────────────────────────
        # Activated instead of the plain text popup when DEBUG_LOGS is selected.
        self.debug_tags_mode:    bool            = False
        self.debug_tags_sel:     int             = 0       # 0=bool row, 1+=tag rows
        self._debug_tags_scroll: int             = 0
        self._debug_tags_bool:   str             = "false" # working copy of the bool
        self._debug_tags_keys:   list            = []      # ordered tag names
        self._debug_tags_state:  dict[str, bool] = {}      # working copy of tag states

        # ── Per-message filter popup state ──────────────────────────────────────
        # A third layer, opened with Space on a tag row inside the debug-tags
        # popup: lists every individual dbg() call site for that one tag, each
        # toggleable independently, plus a top row mirroring the tag's overall
        # on/off switch.
        self.msg_filters_mode:     bool            = False
        self._msg_filters_tag:     str             = ""
        self._msg_filters_sel:     int             = 0       # 0=tag switch row, 1+=message rows
        self._msg_filters_scroll:  int             = 0
        self._msg_filters_keys:    list            = []      # ordered callsite ids
        self._msg_filters_labels:  dict[str, str]  = {}      # callsite_id -> label
        self._msg_filters_state:   dict[str, bool] = {}      # working copy: callsite_id -> enabled

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

    # ── Debug-tags popup ──────────────────────────────────────────────────────

    def _open_debug_tags_popup(self) -> None:
        """Switch to the debug-tags editor for the DEBUG_LOGS key."""
        try:
            from . import logger as _logger
        except ImportError:
            import logger as _logger  # type: ignore[no-redef]

        # get_dbg_filters() reads the live state directly from global.json.
        state = _logger.get_dbg_filters()

        self._debug_tags_bool   = self.editing_item.value.strip().lower()
        self._debug_tags_state  = state
        self._debug_tags_keys   = list(state.keys())
        self.debug_tags_sel     = 0
        self._debug_tags_scroll = 0
        self.debug_tags_mode    = True

    def _handle_debug_tags_key(self, key) -> bool:
        """Handle keypresses while the debug-tags popup is open."""
        n_rows = 1 + len(self._debug_tags_keys)   # row 0 = bool, 1+ = tags

        if key == 27:                               # Esc → discard
            self.debug_tags_mode = False
            self.editing_item    = None
            return True

        elif key == curses.KEY_UP:
            self.debug_tags_sel = max(0, self.debug_tags_sel - 1)
            return True

        elif key == curses.KEY_DOWN:
            self.debug_tags_sel = min(n_rows - 1, self.debug_tags_sel + 1)
            return True

        elif key == ord(' '):                       # Space → toggle / drill in
            if self.debug_tags_sel == 0:
                cur = self._debug_tags_bool.lower()
                self._debug_tags_bool = "false" if cur == "true" else "true"
            else:
                # Drill into per-message control for this tag rather than
                # toggling it directly — the tag's own on/off switch now
                # lives as the top row of that popup.
                self._open_msg_filters_popup()
            return True

        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):   # Enter → save + close
            self._save_debug_tags()
            self.debug_tags_mode = False
            self.editing_item    = None
            return True

        return True   # consume all other keys so they don't leak to the list

    def _save_debug_tags(self) -> None:
        """Persist the debug-log bool and tag states, and apply them live."""
        # 1. Write the bool back to global.conf through the standard save path.
        if self.editing_item and 0 <= self.editing_item.line_idx < len(self.lines):
            self.lines[self.editing_item.line_idx] = (
                f"{self.editing_item.key} = {self._debug_tags_bool}\n"
            )
        self.save()   # writes global.conf and fires on_save

        # 2. Persist tag overrides to global.json.
        try:
            from .main import _global_json_lock, _load_global_json, _save_global_json
            with _global_json_lock:
                gdata = _load_global_json()
                gdata["debug_log_tags"] = self._debug_tags_state
                _save_global_json(gdata)
        except Exception as e:
            _dbg(f"[CONFIG] _save_debug_tags: failed to write global.json: {e}")

    # ── Per-message filter popup (drilled into from a tag row) ────────────────

    def _open_msg_filters_popup(self) -> None:
        """Switch to the per-message editor for the currently selected tag."""
        tag = self._debug_tags_keys[self.debug_tags_sel - 1]
        try:
            from . import logger as _logger
        except ImportError:
            import logger as _logger  # type: ignore[no-redef]

        _logger.rescan_dbg_call_sites()
        call_sites = _logger.get_dbg_call_sites(tag)          # [(id, label), ...]
        overrides  = _logger.get_dbg_message_overrides(tag)   # id -> False (explicit only)

        self._msg_filters_tag    = tag
        self._msg_filters_keys   = [cs_id for cs_id, _ in call_sites]
        self._msg_filters_labels = {cs_id: label for cs_id, label in call_sites}
        self._msg_filters_state  = {cs_id: overrides.get(cs_id, True) for cs_id, _ in call_sites}
        self._msg_filters_sel    = 0
        self._msg_filters_scroll = 0
        self.msg_filters_mode    = True

    def _handle_msg_filters_key(self, key) -> bool:
        """Handle keypresses while the per-message filter popup is open."""
        n_rows = 1 + len(self._msg_filters_keys)   # row 0 = tag switch, 1+ = messages

        if key == 27:                               # Esc → discard, back to tag list
            self.msg_filters_mode = False
            return True

        elif key == curses.KEY_UP:
            self._msg_filters_sel = max(0, self._msg_filters_sel - 1)
            return True

        elif key == curses.KEY_DOWN:
            self._msg_filters_sel = min(n_rows - 1, self._msg_filters_sel + 1)
            return True

        elif key == ord(' '):                       # Space → toggle selected row
            if self._msg_filters_sel == 0:
                tag = self._msg_filters_tag
                self._debug_tags_state[tag] = not self._debug_tags_state.get(tag, False)
            elif self._msg_filters_keys:
                cs_id = self._msg_filters_keys[self._msg_filters_sel - 1]
                self._msg_filters_state[cs_id] = not self._msg_filters_state.get(cs_id, True)
            return True

        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):   # Enter → save + back
            self._save_msg_filters()
            self.msg_filters_mode = False
            return True

        return True   # consume all other keys so they don't leak to the outer popup

    def _save_msg_filters(self) -> None:
        """Persist this tag's on/off switch and its per-message overrides."""
        try:
            from .main import _global_json_lock, _load_global_json, _save_global_json
        except ImportError:
            from main import _global_json_lock, _load_global_json, _save_global_json  # type: ignore[no-redef]

        with _global_json_lock:
            gdata = _load_global_json()
            gdata["debug_log_tags"] = self._debug_tags_state

            all_filters = gdata.get("debug_log_message_filters", {})
            # Only persist explicit disables — a callsite absent from the dict
            # is enabled by default, keeping global.json small over time.
            tag_overrides = {
                cs_id: False for cs_id, enabled in self._msg_filters_state.items() if not enabled
            }
            if tag_overrides:
                all_filters[self._msg_filters_tag] = tag_overrides
            else:
                all_filters.pop(self._msg_filters_tag, None)
            gdata["debug_log_message_filters"] = all_filters

            _save_global_json(gdata)
        _dbg(f"[CONFIG] _save_msg_filters: tag={self._msg_filters_tag!r} "
             f"disabled={sorted(k for k, v in self._msg_filters_state.items() if not v)}")

    def _draw_msg_filters_popup(self, stdscr) -> None:
        """Draw the per-message toggle popup for a single tag."""
        db   = self.dashboard
        h, w = stdscr.getmaxyx()

        box_w  = min(78, w - 4)
        n_msgs = len(self._msg_filters_keys)

        # 2 borders + 1 title gap + 1 tag-switch row + 1 blank +
        # 1 "Messages:" header + n_msgs rows (or 1 "none found" line) + 1 blank + 1 legend
        min_h = max(n_msgs, 1) + 8
        box_h = min(min_h, h - 4)
        by1   = (h - box_h) // 2
        bx1   = (w - box_w) // 2
        by2   = by1 + box_h
        bx2   = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(db.C_NORMAL))
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_SYSTEM)
        title = f" [{self._msg_filters_tag}] MESSAGES "
        db.safe_addstr(stdscr, by1, bx1 + 2, title, curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)

        row = by1 + 2

        # ── Tag on/off row (selection index 0) ────────────────────────────────
        is_sel   = (self._msg_filters_sel == 0)
        prefix   = "> " if is_sel else "  "
        enabled  = self._debug_tags_state.get(self._msg_filters_tag, False)
        val_disp = "[ ON]" if enabled else "[OFF]"
        row_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                    if is_sel else curses.color_pair(db.C_NORMAL))
        val_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                    if is_sel
                    else (curses.color_pair(db.C_LIVE) if enabled else curses.color_pair(db.C_WARN)))
        db.safe_addstr(stdscr, row, bx1 + 2, prefix + f"{'Tag Enabled:':<18}", row_attr)
        db.safe_addstr(stdscr, row, bx1 + 22, val_disp, val_attr | curses.A_BOLD)
        row += 2

        db.safe_addstr(stdscr, row, bx1 + 2, "Messages:", curses.color_pair(db.C_DIM))
        row += 1

        if not self._msg_filters_keys:
            db.safe_addstr(stdscr, row, bx1 + 2, "(no dbg() calls found for this tag)",
                           curses.color_pair(db.C_DIM))
        else:
            avail_rows = (by2 - row) - 2   # reserve 2 lines for legend at bottom
            msg_sel = self._msg_filters_sel - 1
            if msg_sel >= 0:
                if msg_sel < self._msg_filters_scroll:
                    self._msg_filters_scroll = msg_sel
                elif msg_sel >= self._msg_filters_scroll + avail_rows:
                    self._msg_filters_scroll = msg_sel - avail_rows + 1

            scroll = self._msg_filters_scroll
            label_w = box_w - 14
            for i in range(scroll, min(n_msgs, scroll + avail_rows)):
                cs_id    = self._msg_filters_keys[i]
                label    = self._msg_filters_labels.get(cs_id, cs_id)
                msg_on   = self._msg_filters_state.get(cs_id, True)
                is_sel   = (self._msg_filters_sel == i + 1)
                prefix   = "> " if is_sel else "  "
                val_str  = "[ ON]" if msg_on else "[OFF]"
                row_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                            if is_sel else curses.color_pair(db.C_NORMAL))
                val_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                            if is_sel
                            else (curses.color_pair(db.C_LIVE) if msg_on else curses.color_pair(db.C_DIM)))
                disp = label if len(label) <= label_w else label[:label_w - 1] + "\u25ba"
                db.safe_addstr(stdscr, row, bx1 + 2, prefix + disp, row_attr)
                db.safe_addstr(stdscr, row, bx2 - 7, val_str, val_attr | curses.A_BOLD)
                row += 1

        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Space:Toggle  Enter:Save  Esc:Back ",
                       curses.color_pair(db.C_INVHEAD))

    def _draw_debug_tags_popup(self, stdscr) -> None:
        """Draw the combined bool-toggle + per-tag-toggle popup for DEBUG_LOGS."""
        db   = self.dashboard
        h, w = stdscr.getmaxyx()

        box_w   = min(44, w - 4)
        n_tags  = len(self._debug_tags_keys)

        # Allocate rows: 2 borders + 1 title gap + 1 bool row + 1 blank +
        # 1 "Tag Filters:" header + n_tags tag rows + 1 blank + 1 legend
        min_h   = n_tags + 8
        box_h   = min(min_h, h - 4)
        by1     = (h - box_h) // 2
        bx1     = (w - box_w) // 2
        by2     = by1 + box_h
        bx2     = bx1 + box_w

        # Clear background
        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           curses.color_pair(db.C_NORMAL))
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_SYSTEM)
        db.safe_addstr(stdscr, by1, bx1 + 2, " DEBUG LOGGING ",
                       curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)

        row = by1 + 2

        # ── Bool row (selection index 0) ──────────────────────────────────────
        is_sel    = (self.debug_tags_sel == 0)
        prefix    = "> " if is_sel else "  "
        bool_val  = self._debug_tags_bool.lower()
        bool_disp = "[ ON]" if bool_val == "true" else "[OFF]"
        row_attr  = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                     if is_sel else curses.color_pair(db.C_NORMAL))
        val_attr  = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                     if is_sel
                     else (curses.color_pair(db.C_LIVE)
                           if bool_val == "true"
                           else curses.color_pair(db.C_WARN)))
        db.safe_addstr(stdscr, row, bx1 + 2,
                       prefix + f"{'Enable Logging:':<18}", row_attr)
        db.safe_addstr(stdscr, row, bx1 + 22, bool_disp,
                       val_attr | curses.A_BOLD)
        row += 2

        # ── "Tag Filters:" section header ─────────────────────────────────────
        db.safe_addstr(stdscr, row, bx1 + 2, "Tag Filters:",
                       curses.color_pair(db.C_DIM))
        row += 1

        # ── Scrollable tag rows (selection indices 1 … n_tags) ───────────────
        avail_rows = (by2 - row) - 2   # reserve 2 lines for legend at bottom

        # Adjust scroll so the selected tag stays visible.
        tag_sel = self.debug_tags_sel - 1   # relative index into _debug_tags_keys
        if tag_sel >= 0:
            if tag_sel < self._debug_tags_scroll:
                self._debug_tags_scroll = tag_sel
            elif tag_sel >= self._debug_tags_scroll + avail_rows:
                self._debug_tags_scroll = tag_sel - avail_rows + 1

        scroll = self._debug_tags_scroll
        for i in range(scroll, min(n_tags, scroll + avail_rows)):
            tag     = self._debug_tags_keys[i]
            enabled = self._debug_tags_state.get(tag, False)
            is_sel  = (self.debug_tags_sel == i + 1)
            prefix  = "> " if is_sel else "  "
            val_str = "[ ON]" if enabled else "[OFF]"
            row_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                        if is_sel else curses.color_pair(db.C_NORMAL))
            val_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                        if is_sel
                        else (curses.color_pair(db.C_LIVE)
                              if enabled
                              else curses.color_pair(db.C_DIM)))
            db.safe_addstr(stdscr, row, bx1 + 2,
                           prefix + f"{tag:<18}", row_attr)
            db.safe_addstr(stdscr, row, bx1 + 22, val_str,
                           val_attr | curses.A_BOLD)
            row += 1

        # ── Legend ────────────────────────────────────────────────────────────
        legend = (" Space:Toggle  Enter:Save  Esc:Cancel "
                  if self.debug_tags_sel == 0 else
                  " Space:Messages  Enter:Save  Esc:Cancel ")
        db.safe_addstr(stdscr, by2, bx1 + 2, legend, curses.color_pair(db.C_INVHEAD))

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

        # Per-message popup is nested inside the debug-tags popup and takes
        # priority over it while open; the debug-tags popup itself has
        # priority over everything else — both consume all keys.
        if self.msg_filters_mode:
            return self._handle_msg_filters_key(key)
        if self.debug_tags_mode:
            return self._handle_debug_tags_key(key)

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
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
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
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if self.items:
                self.editing_item = self.items[self.selected_idx]
                if self.editing_item.key == "DEBUG_LOGS":
                    self._open_debug_tags_popup()
                else:
                    self.popup_buf = self.editing_item.value
                    self.popup_mode = True
            return True
        return False

    def draw(self, stdscr, y1, x1, y2, x2, is_active: bool):
        """Draw the global settings panel in the given box."""
        self._ensure_loaded()
        db = self.dashboard

        visible_rows = (y2 - y1) - 2
        if self.selected_idx < self.scroll_offset:
            self.scroll_offset = self.selected_idx
        elif self.selected_idx >= self.scroll_offset + visible_rows:
            self.scroll_offset = self.selected_idx - visible_rows + 1

        self.dashboard.draw_box(stdscr, y1, x1, y2, x2, db.C_SYSTEM)
        title = " GLOBAL SETTINGS "
            
        self.dashboard.safe_addstr(stdscr, y1, x1 + 2, title, curses.color_pair(db.C_LIVE) | curses.A_BOLD)
        if is_active:
            mode_str = " [  ] "
            self.dashboard.safe_addstr(stdscr, y1, x2 - len(mode_str) - 1, mode_str,
                        curses.color_pair(db.C_LIVE) | curses.A_BOLD)

        row_y = y1 + 1
        loop_end = min(len(self.items), self.scroll_offset + visible_rows)
        for i in range(self.scroll_offset, loop_end):
            item = self.items[i]
            is_sel = is_active and (i == self.selected_idx)
            prefix = "> " if is_sel else "  "
            key_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                        if is_sel else curses.color_pair(db.C_WARN) | curses.A_BOLD)
            val_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                        if is_sel else curses.color_pair(db.C_LIVE))
            self.dashboard.safe_addstr(stdscr, row_y, x1 + 1, prefix + f"{item.key:<22}", key_attr)
            val_str = "= " + str(item.value)
            
            # columns between value start and right border (reduced by 2 to leave space for arrows)
            max_val_w = (x2 - x1) - 26 - 3   
            
            if len(val_str) > max_val_w:
                val_str = val_str[:max_val_w - 1] + "\u25ba"
            self.dashboard.safe_addstr(stdscr, row_y, x1 + 26, val_str, val_attr)
            
            # --- Add Scroll Arrows ---
            if i == self.scroll_offset and self.scroll_offset > 0:
                self.dashboard.safe_addstr(stdscr, row_y, x2 - 2, "\u25b2", curses.color_pair(db.C_LIVE) | curses.A_BOLD)
            if i == loop_end - 1 and loop_end < len(self.items):
                self.dashboard.safe_addstr(stdscr, row_y, x2 - 2, "\u25bc", curses.color_pair(db.C_LIVE) | curses.A_BOLD)
                
            row_y += 1

        if self.popup_mode and self.editing_item:
            self.draw_popup(stdscr)
        elif self.debug_tags_mode or self.msg_filters_mode:
            self.draw_popup(stdscr)

    def draw_popup(self, stdscr):
        if self.msg_filters_mode:
            self._draw_msg_filters_popup(stdscr)
        elif self.debug_tags_mode:
            self._draw_debug_tags_popup(stdscr)
        else:
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
        self.dashboard.safe_addstr(stdscr, row, bx1 + 2,
                    f"Key: {self.editing_item.key}{_managed_key_note(self.editing_item.key)}",
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
        prio_w   = PRIORITY_PANEL_W                       # same as system sidebar
        # Split remaining space evenly so SITE SETTINGS and GLOBAL SETTINGS are identical widths.
        remaining_w = total_w - prio_w - 2               # 2 gaps between the three columns
        col_w    = max(28, remaining_w // 2)
        global_w = col_w
        site_w   = remaining_w - col_w                   # absorbs odd pixel when terminal is odd-width

        site_x1   = x1
        site_x2   = x1 + site_w
        global_x1 = site_x2 + 1
        global_x2 = global_x1 + global_w
        prio_x1   = global_x2 + 1
        prio_x2   = x2                                    # == prio_x1 + prio_w

        content_y1 = y1

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

        self.dashboard.safe_addstr(stdscr, content_y1, tab_x + 2, "[: prev site  ]: next site  Tab: Next Panel", curses.color_pair(self.dashboard.C_DIM))

        # ── Draw SITE SETTINGS box (left column) ──────────────────────────────
        site_box_y1 = content_y1 + 1
        self.dashboard.draw_box(stdscr, site_box_y1, site_x1, y2, site_x2, self.dashboard.C_CHROME)
        if self._focus == "site":
            mode_str = " [  ] "
            self.dashboard.safe_addstr(stdscr, site_box_y1, site_x2 - len(mode_str) - 1, mode_str,
                        curses.color_pair(self.dashboard.C_LIVE) | curses.A_BOLD)
                        
        title = " SITE SETTINGS "
        if self.items:
            visible_rows = (y2 - site_box_y1) - 2
            if self.selected_idx < self.scroll_offset:
                self.scroll_offset = self.selected_idx
            elif self.selected_idx >= self.scroll_offset + visible_rows:
                self.scroll_offset = self.selected_idx - visible_rows + 1

        self.dashboard.safe_addstr(stdscr, site_box_y1, site_x1 + 2, title,
                    curses.color_pair(self.dashboard.C_LIVE) | curses.A_BOLD)

        if not self.items:
            self.dashboard.safe_addstr(stdscr, site_box_y1 + 2, site_x1 + 4,
                        "No configurable items found.",
                        curses.color_pair(self.dashboard.C_DIM))
        else:
            row_y = site_box_y1 + 1
            loop_end = min(len(self.items), self.scroll_offset + visible_rows)
            for i in range(self.scroll_offset, loop_end):
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
                        val_str = "= " + str(item.value)
                        
                        # columns between value start and right border (reduced by 2 to leave space for arrows)
                        max_val_w = (site_x2 - site_x1) - 29 - 3   
                        
                        if len(val_str) > max_val_w:
                            val_str = val_str[:max_val_w - 1] + "\u25ba"
                        self.dashboard.safe_addstr(stdscr, row_y, site_x1 + 29, val_str, val_attr)
                
                # --- Add Scroll Arrows ---
                if i == self.scroll_offset and self.scroll_offset > 0:
                    self.dashboard.safe_addstr(stdscr, row_y, site_x2 - 2, "\u25b2", curses.color_pair(self.dashboard.C_LIVE) | curses.A_BOLD)
                if i == loop_end - 1 and loop_end < len(self.items):
                    self.dashboard.safe_addstr(stdscr, row_y, site_x2 - 2, "\u25bc", curses.color_pair(self.dashboard.C_LIVE) | curses.A_BOLD)
                    
                row_y += 1

        # Draw popup (whichever sub-editor owns it)
        if self._focus == "global" and (
            (self.global_editor.popup_mode and self.global_editor.editing_item)
            or self.global_editor.debug_tags_mode
            or self.global_editor.msg_filters_mode
        ):
            self.global_editor.draw_popup(stdscr)
        elif self._focus == "site" and self.popup_mode and self.editing_item:
            self.draw_popup(stdscr)
        elif self.priority_editor._settings_popup is not None:
            self.priority_editor._settings_popup.draw(stdscr)

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
        self.dashboard.safe_addstr(stdscr, row, bx1 + 2,
                    f"Key: {self.editing_item.key}{_managed_key_note(self.editing_item.key)}",
                    curses.color_pair(self.dashboard.C_CHROME))
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
        any_popup = (self.global_editor.popup_mode or self.global_editor.debug_tags_mode
                     or self.global_editor.msg_filters_mode or self.popup_mode)
        if key == ord('\t') and not any_popup:
            _cycle = ["site", "global", "priority"]
            self._focus = _cycle[(_cycle.index(self._focus) + 1) % len(_cycle)]
            return True

        # ── Priority panel focus ──────────────────────────────────────────────
        if self._focus == "priority":
            # Only exit the Config tab on Esc when no streamer settings popup is open.
            if key == 27 and self.priority_editor._settings_popup is None:
                self.dashboard.selected_tab = 0
                return True
            return self.priority_editor.handle_key(key)

        if self._focus == "global":
            # Escape in global panel without any popup → exit Config tab.
            # Must also check debug_tags_mode / msg_filters_mode: when the
            # DEBUG LOGGING popup (or its nested per-message popup) is open,
            # ESC should close it (handled inside global_editor.handle_key)
            # rather than switching away from the Config tab.
            if (key == 27 and not self.global_editor.popup_mode
                    and not self.global_editor.debug_tags_mode
                    and not self.global_editor.msg_filters_mode):
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
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
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
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if self.items and not self.items[self.selected_idx].is_section:
                self.editing_item = self.items[self.selected_idx]
                if self.editing_item.has_equals:
                    self.popup_buf = self.editing_item.value
                else:
                    self.popup_buf = self.editing_item.key
                self.popup_mode = True
            return True

        return False
