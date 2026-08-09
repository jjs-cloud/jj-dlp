"""
File Manager tab
=================

TUI conversion of the standalone ``file_write_monitor.pyw`` tool, restyled
to match the rest of the jj-dlp curses dashboard and wired in as a tab of
its own ("File Manager").

What it does
------------
Watches every configured site's OUTPUT_DIR (de-duplicated, since multiple
sites can point at the same folder) and shows, per file, whether it is
currently being written to. A file is considered WRITING if its size has
changed within the last ``IDLE_THRESHOLD_S`` seconds, otherwise it is IDLE.
When there is more than one distinct OUTPUT_DIR, each folder's files are
shown under their own group header so they stay visually separated.

Each OUTPUT_DIR is scanned recursively, so files stay visible when the
SUBFOLDERS global key is on and recordings are nested one level down in a
per-streamer subfolder. Files found below the top of an OUTPUT_DIR are
shown with their subfolder-relative path (e.g. "StreamerName/file.mp4")
so it's clear which streamer's subfolder they live in.

Keybinds (active only while the "File Manager" tab is selected)
-----------------------------------------------------------------
    UP / DOWN     - move the file selection
    ENTER         - open the selected file with the OS default app
    SPACE         - open the file's containing folder, with the file
                    pre-selected/highlighted
    DELETE        - remove the selected file (Trash or permanent delete,
                    see "Delete mode" below)
    S             - open the sort-order popup (persisted to global.json)
    T             - toggle delete mode between Trash and Permanent Delete
                    (persisted to global.json)
    M             - open the "File Options" popup for the selected file

File Options
---------------------
Pressing M on a selected file opens a small "File Options" menu. Selecting
"Fixup" opens a second popup with two checkboxes:

    [ ] Delete original file after fixup is completed
    [ ] Convert to MP4 (no re-encode)

Fixup itself mirrors yt-dlp's fixup remux: it stream-copies (no re-encode)
the file through ffmpeg to repair broken/discontinuous timestamps, the
same class of problem yt-dlp's own fixup postprocessors patch up after a
download. "Convert to MP4" only changes the output container - it is still
a stream-copy remux, not a transcode. The new file's "date modified" is
always set to match the original file's, and Fixup runs in a background
thread so the UI doesn't freeze while ffmpeg works.

Selecting "Trim" opens a popup asking for a start and end time (each
entered as HH:MM:SS) plus two checkboxes:

    Start: 00:00:00
    End:   00:00:00
    [ ] Delete original file
    [ ] Convert to MP4 (no re-encode)

ffmpeg stream-copies (no re-encode) the span between Start and End into a
new "<name>_trimmed<ext>" file (or "<name>_trimmed.mp4" when "Convert to
MP4" is checked). Trim runs in a background thread so the UI doesn't
freeze while ffmpeg works, and the original is only removed (via the
current Delete mode) once the trimmed file has been written successfully.

Delete mode
-----------
Controlled by the ``file_manager.delete_mode`` key in global.json, one of:

    "trash"     (default) - send the file to the Recycle Bin / Trash.
                 Uses send2trash if it happens to be installed; otherwise
                 falls back to a native mechanism per OS (Windows Shell API,
                 macOS Finder via AppleScript, or Linux gio/trash-put/trash).
                 send2trash is NOT a hard requirement - if it isn't
                 installed and no native fallback is available either, the
                 delete is refused (with an on-screen message) rather than
                 silently deleting the file for good.
    "permanent" - delete the file immediately with no recycle bin at all.

Press T inside the File Manager tab to flip between the two at any time.
"""

import os
import re
import shutil
import time
import platform
import subprocess
import threading

import curses

from .deps import check_ffmpeg
from . import theme

try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

POLL_INTERVAL_S = 1.0      # how often we re-scan OUTPUT_DIRs
IDLE_THRESHOLD_S = 3.0     # size unchanged for this long => IDLE
STATUS_MSG_TTL_S = 4.0     # how long an inline status/error message lingers

DELETE_MODE_TRASH = "trash"
DELETE_MODE_PERMANENT = "permanent"
DELETE_MODE_DEFAULT = DELETE_MODE_TRASH

# ── Sort options for the File Manager tab (mirrors the Dashboard's "S" sort popup) ──
SORT_OPTIONS_FM = [
    ("name_asc",       "Name (A-Z)"),
    ("name_desc",      "Name (Z-A)"),
    ("status_writing", "Status (Writing first)"),
    ("status_idle",    "Status (Idle first)"),
    ("modified_new",   "Date Modified (Newest first)"),
    ("modified_old",   "Date Modified (Oldest first)"),
    ("size_desc",      "Size (Largest first)"),
    ("size_asc",       "Size (Smallest first)"),
    ("rate_desc",      "Rate (Fastest first)"),
    ("rate_asc",       "Rate (Slowest first)"),
]
_FM_SORT_KEYS = [k for k, _ in SORT_OPTIONS_FM]
_FM_SORT_LABELS = {k: lbl for k, lbl in SORT_OPTIONS_FM}
FM_SORT_DEFAULT = "name_asc"

# ── "File Options" popup (M key) ────────────────────────────────────────────
# One row per action.
FILE_MENU_OPTIONS = [
    ("fixup", "Fixup"),
    ("move",  "Move"),
    ("trim",  "Trim"),
]

# ── "Fixup" checkbox popup ──────────────────────────────────────────────────
FIXUP_CHECK_ITEMS = [
    ("delete_original", "Delete original file when finished"),
    ("convert_mp4",     "Convert to MP4 (no re-encode)"),
]

# ── "Move" checkbox popup ────────────────────────────────────────────────────
MOVE_CHECK_ITEMS = [
    ("subfolder", "Move file into a subfolder named after the streamer"),
    ("fixup",     "Fixup the file during the move"),
]

# Sort keys for which the underlying comparison needs to be reversed to
# achieve the labeled order (e.g. "Newest first" means reverse=True on mtime).
_FM_SORT_REVERSE = {
    "name_desc", "status_idle", "modified_new", "size_desc", "rate_desc",
}


# ──────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────────────────────────────────

def human_size(n):
    if n is None:
        return "\u2014"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024.0 or unit == "PB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} EB"


def human_rate(bps):
    if not bps:
        return "\u2014"
    sign = "-" if bps < 0 else ""
    return f"{sign}{human_size(abs(bps))}/s"


# ──────────────────────────────────────────────────────────────────────────
# OS integration helpers (ported from file_write_monitor.pyw; no tkinter,
# every function returns (ok: bool, error_message: str | None) instead of
# popping up a messagebox, so the TUI can show failures inline).
# ──────────────────────────────────────────────────────────────────────────

def open_file(path):
    """Launch *path* with the OS default application."""
    abs_path = os.path.abspath(path)
    try:
        if IS_WINDOWS:
            os.startfile(abs_path)  # noqa: S606 - Windows-only API
        elif IS_MAC:
            subprocess.Popen(["open", abs_path])
        else:
            subprocess.Popen(["xdg-open", abs_path])
        return True, None
    except Exception as exc:
        return False, str(exc)


def open_containing_folder(path):
    """Open the folder containing *path*, with the file pre-selected."""
    abs_path = os.path.abspath(path)
    try:
        if IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", abs_path])
        elif IS_MAC:
            subprocess.Popen(["open", "-R", abs_path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(abs_path) or "."])
        return True, None
    except Exception as exc:
        return False, str(exc)


def move_to_trash(path):
    """Send *path* to the Recycle Bin / Trash."""
    abs_path = os.path.abspath(path)
    if _send2trash is not None:
        try:
            _send2trash(abs_path)
            return True, None
        except Exception as exc:
            return False, str(exc)
    try:
        if IS_WINDOWS:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            FO_DELETE = 0x0003
            FOF_ALLOWUNDO = 0x0040
            FOF_NOCONFIRMATION = 0x0010
            FOF_SILENT = 0x0004
            # pFrom must be double-null-terminated
            pfrom = abs_path + "\0"
            op = SHFILEOPSTRUCTW(
                hwnd=None, wFunc=FO_DELETE, pFrom=pfrom, pTo=None,
                fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT,
                fAnyOperationsAborted=False, hNameMappings=None, lpszProgressTitle=None,
            )
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            if result != 0:
                return False, f"SHFileOperationW failed (code {result})"
            return True, None
        elif IS_MAC:
            script = (
                'tell application "Finder" to delete POSIX file '
                f'"{abs_path}"'
            )
            subprocess.run(["osascript", "-e", script], check=True,
                            capture_output=True)
            return True, None
        else:
            for cmd in (["gio", "trash", abs_path],
                        ["trash-put", abs_path],
                        ["trash", abs_path]):
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                    return True, None
                except (OSError, subprocess.CalledProcessError):
                    continue
            return False, ("No trash utility found (tried gio/trash-put/trash). "
                            "Install send2trash, or switch delete mode to "
                            "Permanent with T.")
    except Exception as exc:
        return False, str(exc)


def permanent_delete(path):
    """Delete *path* immediately, with no recycle bin involved."""
    abs_path = os.path.abspath(path)
    try:
        os.remove(abs_path)
        return True, None
    except Exception as exc:
        return False, str(exc)


# ──────────────────────────────────────────────────────────────────────────
# The tab itself
# ──────────────────────────────────────────────────────────────────────────

class FileManagerTab:
    """Owns all state/behavior for the "File Manager" tab.

    Constructed once by ``JJDlpDashboard.__init__`` (mirroring
    ``ConfigEditor`` / ``SiteSortManager``) and driven by the main
    dashboard's draw loop and key dispatcher.
    """

    def __init__(self, dashboard):
        self.dashboard = dashboard

        # path -> {size, last_change, rate, mtime, last_poll, status,
        #          group_path, group_label}
        self._records = {}
        # Flattened, already-sorted rows ready to draw:
        #   ("header", label_text, None)
        #   ("empty",  text,       None)
        #   ("file",   path,       record_dict)
        self._rows = []

        self._selected_path = None
        self._scroll = 0
        self._at_top = False
        self._last_poll = 0.0

        self._status_msg = ""
        self._status_msg_ts = 0.0

        self._sort_key, self._delete_mode = self._load_settings()

        self.popup_open = False
        self._popup_sel = self._sort_idx(self._sort_key)
        self._popup_scroll = 0

        # "File Options" menu popup (M key)
        self._menu_open = False
        self._menu_sel = 0

        # "Fixup" checkbox popup + background job state
        self._fixup_open = False
        self._fixup_cursor = 0
        self._fixup_target = None
        self._fixup_checks = {"delete_original": True, "convert_mp4": True}
        self._fixup_busy = False
        self._fixup_lock = threading.Lock()

        # "Move" destination-picker popup (step 1: destination + checkboxes)
        self._move_open = False
        self._move_cursor = 0
        self._move_checks = {"subfolder": True, "fixup": False}
        self._move_target = None
        self._move_destinations = []

        # "Move" filename popup (step 2)
        self._move_filename_open = False
        self._move_filename_buf = ""
        self._move_filename_cursor = 0
        self._move_filename_dest = None
        self._move_filename_streamer = ""

        self._move_busy = False
        self._move_busy_path = None
        self._move_lock = threading.Lock()

        # Transient tracking for the Move OUTPUT file: while a Move is in
        # progress, the destination file is temporarily added to the File
        # Manager (under its own "Moving" section) so the user can watch
        # its Size/Rate/Status grow in real time. Removed the moment the
        # move finishes (success or failure).
        self._move_dest_path = None
        self._moving_records = {}

        # "Trim" popup (start/end time entry + "Delete original"/"Convert to
        # MP4" checkboxes)
        self._trim_open = False
        self._trim_target = None
        self._trim_cursor = 0            # 0=start, 1=end, 2=delete cb, 3=mp4 cb
        self._trim_field_cursor = 0      # cursor position within the active field
        self._trim_start_buf = "00:00:00"
        self._trim_end_buf = "00:00:00"
        self._trim_delete_original = False
        self._trim_convert_mp4 = False
        self._trim_busy = False
        self._trim_lock = threading.Lock()

    # ── Settings persistence (global.json) ─────────────────────────────────

    @staticmethod
    def _load_settings():
        try:
            from .main import _load_global_json, _global_json_lock
            with _global_json_lock:
                data = _load_global_json()
            fm = data.get("file_manager", {}) if isinstance(data, dict) else {}
            sort_key = fm.get("sort_key", FM_SORT_DEFAULT)
            if sort_key not in _FM_SORT_KEYS:
                sort_key = FM_SORT_DEFAULT
            delete_mode = fm.get("delete_mode", DELETE_MODE_DEFAULT)
            if delete_mode not in (DELETE_MODE_TRASH, DELETE_MODE_PERMANENT):
                delete_mode = DELETE_MODE_DEFAULT
            return sort_key, delete_mode
        except Exception:
            return FM_SORT_DEFAULT, DELETE_MODE_DEFAULT

    def _save_settings(self):
        try:
            from .main import _load_global_json, _save_global_json, _global_json_lock
            with _global_json_lock:
                data = _load_global_json()
                fm = data.get("file_manager", {})
                if not isinstance(fm, dict):
                    fm = {}
                fm["sort_key"] = self._sort_key
                fm["delete_mode"] = self._delete_mode
                data["file_manager"] = fm
                _save_global_json(data)
        except Exception:
            pass

    # ── OUTPUT_DIR discovery ────────────────────────────────────────────────

    def _get_output_dirs(self):
        """Ordered, de-duplicated list of (label, abs_path) - one per
        distinct OUTPUT_DIR across all configured sites."""
        seen = {}
        for site in self.dashboard.sites:
            try:
                cfg = site.get_cached_config()
            except Exception:
                continue
            out_dir = cfg.get("output_dir")
            if not out_dir:
                continue
            abs_dir = os.path.abspath(out_dir)
            if abs_dir in seen:
                continue
            label = cfg.get("site_label") or os.path.basename(site.config_path)
            seen[abs_dir] = label
        return [(label, path) for path, label in seen.items()]

    # ── Polling ─────────────────────────────────────────────────────────────

    def maybe_poll(self, force=False):
        """Re-scan OUTPUT_DIRs at most once every POLL_INTERVAL_S. Safe to
        call every frame from the draw loop."""
        now = time.time()
        if not force and (now - self._last_poll) < POLL_INTERVAL_S:
            return
        self._last_poll = now

        dirs = self._get_output_dirs()
        dir_label_map = {os.path.abspath(folder): label for label, folder in dirs}

        with self._move_lock:
            move_dest_path = self._move_dest_path

        current_paths = set()
        # Maps each discovered file path to the OUTPUT_DIR (root) it was
        # found under, since recursive scanning means a file's immediate
        # parent directory (e.g. a per-streamer subfolder created by the
        # SUBFOLDERS global key) is no longer necessarily the OUTPUT_DIR
        # itself.
        path_roots = {}
        for _label, folder in dirs:
            try:
                for root, _dirnames, filenames in os.walk(folder, followlinks=False):
                    for fname in filenames:
                        fpath = os.path.join(root, fname)
                        try:
                            if not os.path.isfile(fpath):
                                continue
                            # The Move destination file is tracked separately
                            # (see below) so it isn't double-listed if a
                            # DESTINATIONS folder happens to overlap with a
                            # watched OUTPUT_DIR.
                            if move_dest_path and os.path.abspath(fpath) == move_dest_path:
                                continue
                        except OSError:
                            continue
                        current_paths.add(fpath)
                        path_roots[fpath] = folder
            except OSError:
                continue

        # Drop files that vanished (moved/deleted externally).
        for path in list(self._records):
            if path not in current_paths:
                del self._records[path]

        for path in current_paths:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            rec = self._records.get(path)
            if rec is None:
                rec = {"size": size, "last_change": now, "rate": 0.0, "last_poll": now}
                self._records[path] = rec
            elif size != rec["size"]:
                dt = max(now - rec.get("last_poll", now - POLL_INTERVAL_S), 0.001)
                rec["rate"] = (size - rec["size"]) / dt
                rec["size"] = size
                rec["last_change"] = now
            else:
                pass  # keep last known rate; cleared naturally via IDLE status after ~3s
            rec["last_poll"] = now
            try:
                rec["mtime"] = os.path.getmtime(path)
            except OSError:
                rec["mtime"] = rec.get("mtime", 0)

            folder_abs = path_roots.get(path, os.path.dirname(path))
            rec["group_path"] = folder_abs
            rec["group_label"] = dir_label_map.get(folder_abs, os.path.basename(folder_abs) or folder_abs)
            rec["status"] = "WRITING" if (now - rec["last_change"]) < IDLE_THRESHOLD_S else "IDLE"

        # Update the transient "Moving" record (the Move destination file),
        # if a Move is currently in progress.
        if move_dest_path:
            rec = self._moving_records.get(move_dest_path)
            if rec is not None:
                try:
                    size = os.path.getsize(move_dest_path)
                except OSError:
                    size = rec.get("size", 0)
                if size != rec["size"]:
                    dt = max(now - rec.get("last_poll", now - POLL_INTERVAL_S), 0.001)
                    rec["rate"] = (size - rec["size"]) / dt
                    rec["size"] = size
                    rec["last_change"] = now
                rec["last_poll"] = now
                try:
                    rec["mtime"] = os.path.getmtime(move_dest_path)
                except OSError:
                    pass
                rec["status"] = "WRITING"

        self._rebuild_rows(dirs)

    # ── Sorting / row layout ────────────────────────────────────────────────

    def _sort_key_fn(self, path):
        rec = self._records.get(path, {})
        key = self._sort_key
        if key in ("name_asc", "name_desc"):
            return os.path.basename(path).lower()
        if key in ("status_writing", "status_idle"):
            return 0 if rec.get("status") == "WRITING" else 1
        if key in ("modified_new", "modified_old"):
            return rec.get("mtime", 0)
        if key in ("size_desc", "size_asc"):
            return rec.get("size", 0)
        if key in ("rate_desc", "rate_asc"):
            return rec.get("rate", 0.0)
        return os.path.basename(path).lower()

    def _rebuild_rows(self, dirs):
        reverse = self._sort_key in _FM_SORT_REVERSE
        rows = []
        multi = len(dirs) > 1
        for label, folder in dirs:
            folder_abs = os.path.abspath(folder)
            files = [p for p, r in self._records.items() if r.get("group_path") == folder_abs]
            files.sort(key=self._sort_key_fn, reverse=reverse)
            if multi:
                rows.append(("header", f"{label}  \u2014  {folder_abs}", None))
            for p in files:
                rows.append(("file", p, self._records[p]))
            if multi and not files:
                rows.append(("empty", "  (no files)", None))

        # Transient "Moving" section: only present while a Move is actively
        # writing its output file. Always shown as its own section (with a
        # header), independent of how many OUTPUT_DIRs are configured, and
        # disappears the instant the move finishes.
        if self._moving_records:
            rows.append(("header", "Moving", None))
            for p, rec in self._moving_records.items():
                rows.append(("file", p, rec))

        # Remember where the current selection sat among the *old* file rows,
        # so that if it vanishes (fixup/move/trim finishing, deleted
        # externally, etc.) we can land on its neighbor instead of jumping
        # back to the top of the list.
        old_file_paths = [r[1] for r in self._rows if r[0] == "file"]
        old_pos = None
        if self._selected_path in old_file_paths:
            old_pos = old_file_paths.index(self._selected_path)

        self._rows = rows

        # Keep selection valid.
        file_paths = [r[1] for r in rows if r[0] == "file"]
        if self._selected_path not in file_paths:
            if old_pos is not None and file_paths:
                # Land on whatever now occupies the same (or nearest lower)
                # position, mirroring how the list "shifts up" underneath us.
                new_pos = min(old_pos, len(file_paths) - 1)
                self._selected_path = file_paths[new_pos]
            else:
                self._selected_path = file_paths[0] if file_paths else None

    # ── Selection movement ──────────────────────────────────────────────────

    def move_selection(self, delta):
        file_indices = [i for i, r in enumerate(self._rows) if r[0] == "file"]
        if not file_indices:
            self._selected_path = None
            return
        cur_row_idx = next(
            (i for i in file_indices if self._rows[i][1] == self._selected_path), None
        )
        if cur_row_idx is None:
            pos = 0 if delta >= 0 else len(file_indices) - 1
        else:
            pos = file_indices.index(cur_row_idx) + delta
            pos = max(0, min(len(file_indices) - 1, pos))
        self._selected_path = self._rows[file_indices[pos]][1]

    # ── Key handling ─────────────────────────────────────────────────────────

    def handle_key(self, key) -> bool:
        """Returns True if the key was consumed by the File Manager tab."""
        if self.popup_open:
            return self._handle_popup_key(key)
        if self._fixup_open:
            return self._handle_fixup_popup_key(key)
        if self._move_filename_open:
            return self._handle_move_filename_popup_key(key)
        if self._move_open:
            return self._handle_move_popup_key(key)
        if self._trim_open:
            return self._handle_trim_popup_key(key)
        if self._menu_open:
            return self._handle_menu_popup_key(key)

        if key in (curses.KEY_UP,):
            self.move_selection(-1)
            file_indices = [i for i, r in enumerate(self._rows) if r[0] == "file"]
            if file_indices and self._selected_path == self._rows[file_indices[0]][1]:
                self._at_top = True
            return True
        if key in (curses.KEY_DOWN,):
            self.move_selection(1)
            self._at_top = False
            return True
        if key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if self._selected_path:
                ok, err = open_file(self._selected_path)
                if not ok:
                    self._set_status(f"Could not open file: {err}")
            return True
        if key == ord(' '):
            if self._selected_path:
                ok, err = open_containing_folder(self._selected_path)
                if not ok:
                    self._set_status(f"Could not open folder: {err}")
            return True
        if key in (curses.KEY_DC, curses.KEY_BACKSPACE, 127):
            self._delete_selected()
            return True
        if key in (ord('s'), ord('S')):
            self.open_popup()
            return True
        if key in (ord('t'), ord('T')):
            self._toggle_delete_mode()
            return True
        if key in (ord('m'), ord('M')):
            if self._selected_path:
                self.open_menu_popup()
            return True
        return False

    def _toggle_delete_mode(self):
        self._delete_mode = (
            DELETE_MODE_PERMANENT if self._delete_mode == DELETE_MODE_TRASH
            else DELETE_MODE_TRASH
        )
        self._save_settings()
        label = "Permanent Delete" if self._delete_mode == DELETE_MODE_PERMANENT else "Trash"
        self._set_status(f"Delete mode set to: {label}")

    def _delete_selected(self):
        path = self._selected_path
        if not path:
            return

        file_indices = [i for i, r in enumerate(self._rows) if r[0] == "file"]
        cur_row_idx = next((i for i in file_indices if self._rows[i][1] == path), None)

        if self._delete_mode == DELETE_MODE_PERMANENT:
            ok, err = permanent_delete(path)
        else:
            ok, err = move_to_trash(path)

        if not ok:
            self._set_status(f"Delete failed: {err}")
            return

        self._records.pop(path, None)

        # Pick the next sensible selection: next file after the deleted one,
        # else the previous one, else nothing.
        if cur_row_idx is not None:
            remaining = [i for i in file_indices if i != cur_row_idx]
            after = [i for i in remaining if i > cur_row_idx]
            if after:
                self._selected_path = self._rows[after[0]][1]
            elif remaining:
                self._selected_path = self._rows[remaining[-1]][1]
            else:
                self._selected_path = None

        self._rebuild_rows(self._get_output_dirs())
        mode_lbl = "Trash" if self._delete_mode == DELETE_MODE_TRASH else "Permanent"
        self._set_status(f"Deleted ({mode_lbl}): {os.path.basename(path)}")

    def _set_status(self, msg):
        self._status_msg = msg
        self._status_msg_ts = time.time()

    # ── Sort popup ───────────────────────────────────────────────────────────

    def open_popup(self):
        self._popup_sel = self._sort_idx(self._sort_key)
        self._popup_scroll = 0
        self.popup_open = True

    def close_popup(self):
        self.popup_open = False

    def _handle_popup_key(self, key) -> bool:
        n = len(SORT_OPTIONS_FM)
        if key == 27:  # Esc -> cancel
            self.close_popup()
        elif key == curses.KEY_UP:
            self._popup_sel = max(0, self._popup_sel - 1)
        elif key == curses.KEY_DOWN:
            self._popup_sel = min(n - 1, self._popup_sel + 1)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459, ord(' ')):
            new_key = _FM_SORT_KEYS[self._popup_sel]
            if new_key != self._sort_key:
                self._sort_key = new_key
                self._save_settings()
                self._rebuild_rows(self._get_output_dirs())
            self.close_popup()
        # All other keys are consumed so nothing leaks to the dashboard.
        return True

    @staticmethod
    def _sort_idx(sort_key: str) -> int:
        try:
            return _FM_SORT_KEYS.index(sort_key)
        except ValueError:
            return 0

    def draw_popup(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        n = len(SORT_OPTIONS_FM)

        box_w = min(42, w - 4)
        box_h = min(n + 4, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           theme.attr(db, "file_manager_filemanagertab_draw_popup_normal_1", db.C_NORMAL, False))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " SORT FILES ",
                       theme.attr(db, "file_manager_filemanagertab_draw_popup_chrome", db.C_CHROME, True))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Select  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_popup_invhead", db.C_INVHEAD, False))

        visible = box_h - 3

        if self._popup_sel < self._popup_scroll:
            self._popup_scroll = self._popup_sel
        elif self._popup_sel >= self._popup_scroll + visible:
            self._popup_scroll = self._popup_sel - visible + 1

        for i in range(self._popup_scroll, min(n, self._popup_scroll + visible)):
            sort_key, label = SORT_OPTIONS_FM[i]
            row_y = by1 + 1 + (i - self._popup_scroll)
            is_sel = (i == self._popup_sel)
            is_cur = (sort_key == self._sort_key)
            prefix = "> " if is_sel else ("* " if is_cur else "  ")
            if is_sel:
                attr = theme.attr(db, "file_manager_filemanagertab_draw_popup_hilight", db.C_HILIGHT, True)
            elif is_cur:
                attr = theme.attr(db, "file_manager_filemanagertab_draw_popup_live", db.C_LIVE, True)
            else:
                attr = theme.attr(db, "file_manager_filemanagertab_draw_popup_normal_2", db.C_NORMAL, False)
            db.safe_addstr(stdscr, row_y, bx1 + 2,
                           (prefix + label)[:box_w - 4], attr)

    # ── "File Options" popup (M key) ────────────────────────────────────────

    def any_popup_open(self) -> bool:
        """True if any of this tab's popups (sort / menu / fixup / move) is open."""
        return (self.popup_open or self._menu_open or self._fixup_open
                or self._move_open or self._move_filename_open
                or self._trim_open)

    def draw_popups(self, stdscr) -> None:
        """Draw whichever of this tab's popups is currently open."""
        if self.popup_open:
            self.draw_popup(stdscr)
        elif self._menu_open:
            self.draw_menu_popup(stdscr)
        elif self._fixup_open:
            self.draw_fixup_popup(stdscr)
        elif self._move_filename_open:
            self.draw_move_filename_popup(stdscr)
        elif self._move_open:
            self.draw_move_popup(stdscr)
        elif self._trim_open:
            self.draw_trim_popup(stdscr)

    def open_menu_popup(self):
        self._menu_sel = 0
        self._menu_open = True

    def close_menu_popup(self):
        self._menu_open = False

    def _handle_menu_popup_key(self, key) -> bool:
        n = len(FILE_MENU_OPTIONS)
        if key == 27:  # Esc -> cancel
            self.close_menu_popup()
        elif key == curses.KEY_UP:
            self._menu_sel = max(0, self._menu_sel - 1)
        elif key == curses.KEY_DOWN:
            self._menu_sel = min(n - 1, self._menu_sel + 1)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459, ord(' ')):
            action_key, _label = FILE_MENU_OPTIONS[self._menu_sel]
            self.close_menu_popup()
            if action_key == "fixup":
                self.open_fixup_popup()
            elif action_key == "move":
                self.open_move_popup()
            elif action_key == "trim":
                self.open_trim_popup()
        # All other keys are consumed so nothing leaks to the dashboard.
        return True

    def draw_menu_popup(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        n = len(FILE_MENU_OPTIONS)

        box_w = min(36, w - 4)
        box_h = min(n + 3, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_normal_1", db.C_NORMAL, False))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " FILE OPTIONS ",
                       theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_chrome", db.C_CHROME, True))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Select  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_invhead", db.C_INVHEAD, False))

        for i, (_action_key, label) in enumerate(FILE_MENU_OPTIONS):
            row_y = by1 + 1 + i
            is_sel = (i == self._menu_sel)
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_hilight", db.C_HILIGHT, True)) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_normal_2", db.C_NORMAL, False)
            db.safe_addstr(stdscr, row_y, bx1 + 2,
                           (prefix + label)[:box_w - 4], attr)

    # ── "Fixup" checkbox popup ───────────────────────────────────────────────

    def open_fixup_popup(self):
        if not self._selected_path:
            return
        self._fixup_target = self._selected_path
        self._fixup_checks = {"delete_original": True, "convert_mp4": True}
        self._fixup_cursor = 0
        self._fixup_open = True

    def close_fixup_popup(self):
        self._fixup_open = False
        self._fixup_target = None

    def _handle_fixup_popup_key(self, key) -> bool:
        n = len(FIXUP_CHECK_ITEMS)
        if key == 27:  # Esc -> cancel, discard checkbox state
            self.close_fixup_popup()
        elif key == curses.KEY_UP:
            self._fixup_cursor = max(0, self._fixup_cursor - 1)
        elif key == curses.KEY_DOWN:
            self._fixup_cursor = min(n - 1, self._fixup_cursor + 1)
        elif key == ord(' '):
            check_key, _label = FIXUP_CHECK_ITEMS[self._fixup_cursor]
            self._fixup_checks[check_key] = not self._fixup_checks[check_key]
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            target = self._fixup_target
            delete_original = self._fixup_checks["delete_original"]
            convert_mp4 = self._fixup_checks["convert_mp4"]
            self.close_fixup_popup()
            self._start_fixup(target, delete_original, convert_mp4)
        # All other keys are consumed so nothing leaks to the dashboard.
        return True

    def draw_fixup_popup(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        n = len(FIXUP_CHECK_ITEMS)

        box_w = min(52, w - 4)
        box_h = min(n + 5, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_normal_1", db.C_NORMAL, False))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " FIXUP ",
                       theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_chrome", db.C_CHROME, True))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Space: Toggle  Enter: Run  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_invhead", db.C_INVHEAD, False))

        target_name = os.path.basename(self._fixup_target or "")
        db.safe_addstr(stdscr, by1 + 1, bx1 + 2,
                       target_name[:box_w - 4],
                       theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_dim", db.C_DIM, False))

        for i, (check_key, label) in enumerate(FIXUP_CHECK_ITEMS):
            row_y = by1 + 3 + i
            is_sel = (i == self._fixup_cursor)
            checked = self._fixup_checks.get(check_key, False)
            box = "[x]" if checked else "[ ]"
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_hilight", db.C_HILIGHT, True)) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_normal_2", db.C_NORMAL, False)
            db.safe_addstr(stdscr, row_y, bx1 + 2,
                           f"{prefix}{box} {label}"[:box_w - 4], attr)

    # ── "Move" destination-picker popup ──────────────────────────────────────

    @staticmethod
    def _get_destinations():
        """Configured DESTINATIONS paths (global.conf), in order."""
        try:
            from .main import load_global_config
            return list(load_global_config().get("destinations", []))
        except Exception:
            return []

    def open_move_popup(self):
        if not self._selected_path:
            return
        self._move_target = self._selected_path
        self._move_destinations = self._get_destinations()
        self._move_checks = {"subfolder": True, "fixup": False}
        self._move_cursor = 0
        self._move_open = True

    def close_move_popup(self):
        """Fully cancel the Move flow."""
        self._move_open = False
        self._move_target = None

    def _handle_move_popup_key(self, key) -> bool:
        n_dest = len(self._move_destinations)
        n_checks = len(MOVE_CHECK_ITEMS)
        configure_idx = n_dest
        total = n_dest + 1 + n_checks

        if key == 27:  # Esc -> cancel the whole Move
            self.close_move_popup()
        elif key == curses.KEY_UP:
            self._move_cursor = max(0, self._move_cursor - 1)
        elif key == curses.KEY_DOWN:
            self._move_cursor = min(total - 1, self._move_cursor + 1)
        elif key == ord(' ') and self._move_cursor > configure_idx:
            check_key, _label = MOVE_CHECK_ITEMS[self._move_cursor - configure_idx - 1]
            self._move_checks[check_key] = not self._move_checks[check_key]
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if self._move_cursor < n_dest:
                dest = self._move_destinations[self._move_cursor]
                self._move_open = False   # keep _move_target alive for step 2
                self.open_move_filename_popup(dest)
            elif self._move_cursor == configure_idx:
                self._open_configure_destination()
            else:
                check_key, _label = MOVE_CHECK_ITEMS[self._move_cursor - configure_idx - 1]
                self._move_checks[check_key] = not self._move_checks[check_key]
        # All other keys are consumed so nothing leaks to the dashboard.
        return True

    def _open_configure_destination(self):
        """Abandon the in-progress Move and jump to the Config tab's
        DESTINATIONS editor so the user can add a destination, then press
        M again on the file to retry the move."""
        self.close_move_popup()
        db = self.dashboard
        try:
            ce = db.config_editor
            ge = ce.global_editor
            ge._ensure_loaded()
            for i, item in enumerate(ge.items):
                if item.key == "DESTINATIONS":
                    ge.selected_idx = i
                    ge.editing_item = item
                    ge._open_destinations_popup()
                    break
            ce._focus = "global"
            db.selected_tab = db.TABS.index("Config")
        except Exception:
            pass

    def draw_move_popup(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        n_dest = len(self._move_destinations)
        n_checks = len(MOVE_CHECK_ITEMS)

        box_w = min(64, w - 4)
        # 1 "Select a destination:" + n_dest rows + 1 "Configure a new
        # destination" row + 1 blank + n_checks checkbox rows
        inner_rows = 1 + n_dest + 1 + 1 + n_checks
        box_h = min(inner_rows + 4, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), theme.attr(db, "file_manager_filemanagertab_draw_move_popup_normal_1", db.C_NORMAL, False))
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " MOVE ",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_popup_chrome", db.C_CHROME, True))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Select  Space: Toggle  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_popup_invhead", db.C_INVHEAD, False))

        row = by1 + 1
        db.safe_addstr(stdscr, row, bx1 + 2, "Select a destination:",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_popup_dim", db.C_DIM, False))
        row += 1

        configure_idx = n_dest
        for i in range(n_dest):
            is_sel = (self._move_cursor == i)
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_move_popup_hilight_1", db.C_HILIGHT, True)) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_move_popup_normal_2", db.C_NORMAL, False)
            label = self._move_destinations[i]
            db.safe_addstr(stdscr, row, bx1 + 2, (prefix + label)[:box_w - 4], attr)
            row += 1

        is_sel = (self._move_cursor == configure_idx)
        prefix = "> " if is_sel else "  "
        attr = (theme.attr(db, "file_manager_filemanagertab_draw_move_popup_hilight_2", db.C_HILIGHT, True)) if is_sel \
            else theme.attr(db, "file_manager_filemanagertab_draw_move_popup_system", db.C_SYSTEM, False)
        db.safe_addstr(stdscr, row, bx1 + 2,
                       (prefix + "Configure a new destination")[:box_w - 4], attr)
        row += 2

        for i, (check_key, label) in enumerate(MOVE_CHECK_ITEMS):
            idx = configure_idx + 1 + i
            is_sel = (self._move_cursor == idx)
            checked = self._move_checks.get(check_key, False)
            box = "[x]" if checked else "[ ]"
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_move_popup_hilight_3", db.C_HILIGHT, True)) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_move_popup_normal_3", db.C_NORMAL, False)
            db.safe_addstr(stdscr, row, bx1 + 2,
                           f"{prefix}{box} {label}"[:box_w - 4], attr)
            row += 1

    # ── "Move" filename popup (step 2, opened after picking a destination) ──

    def open_move_filename_popup(self, dest):
        self._move_filename_dest = dest
        self._move_filename_buf = os.path.basename(self._move_target or "")
        self._move_filename_cursor = len(self._move_filename_buf)
        path = self._move_target or ""
        group_path = self._records.get(path, {}).get("group_path")
        self._move_filename_streamer = self._derive_streamer_name(path, group_path)
        self._move_filename_open = True

    def close_move_filename_popup(self):
        self._move_filename_open = False
        self._move_filename_dest = None
        self._move_target = None

    def _handle_move_filename_popup_key(self, key) -> bool:
        cur = self._move_filename_cursor
        if key == 27:  # Esc -> cancel the whole Move
            self.close_move_filename_popup()
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if cur > 0:
                self._move_filename_buf = \
                    self._move_filename_buf[:cur - 1] + self._move_filename_buf[cur:]
                self._move_filename_cursor = cur - 1
        elif key in (curses.KEY_DC,):
            if cur < len(self._move_filename_buf):
                self._move_filename_buf = \
                    self._move_filename_buf[:cur] + self._move_filename_buf[cur + 1:]
        elif key in (curses.KEY_LEFT,):
            if cur > 0:
                self._move_filename_cursor = cur - 1
        elif key in (curses.KEY_RIGHT,):
            if cur < len(self._move_filename_buf):
                self._move_filename_cursor = cur + 1
        elif key in (curses.KEY_HOME,):
            self._move_filename_cursor = 0
        elif key in (curses.KEY_END,):
            self._move_filename_cursor = len(self._move_filename_buf)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            filename = self._move_filename_buf.strip()
            target = self._move_target
            dest = self._move_filename_dest
            do_subfolder = self._move_checks.get("subfolder", True)
            do_fixup = self._move_checks.get("fixup", True)
            self.close_move_filename_popup()
            if filename and target and dest:
                self._start_move(target, dest, filename, do_subfolder, do_fixup)
        elif 32 <= key < 127:
            self._move_filename_buf = \
                self._move_filename_buf[:cur] + chr(key) + self._move_filename_buf[cur:]
            self._move_filename_cursor = cur + 1
        # All other keys are consumed so nothing leaks to the dashboard.
        return True

    def draw_move_filename_popup(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()

        box_w = min(66, w - 4)
        box_h = min(6, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_normal_1", db.C_NORMAL, False))
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " MOVE ",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_chrome", db.C_CHROME, True))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Start Move  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_invhead", db.C_INVHEAD, False))

        row = by1 + 1
        db.safe_addstr(stdscr, row, bx1 + 2,
                       f"Streamer: {self._move_filename_streamer}",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_dim", db.C_DIM, False))
        row += 2
        db.safe_addstr(stdscr, row, bx1 + 2, "Filename:",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_warn", db.C_WARN, True))
        row += 1
        buf = self._move_filename_buf
        cur = self._move_filename_cursor
        display = buf[:cur] + "_" + buf[cur:]
        db.safe_addstr(stdscr, row, bx1 + 2,
                       display[:box_w - 4],
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_normal_2", db.C_NORMAL, True))

    # ── Move job (runs on a background thread; mirrors the Fixup job) ───────

    def _compute_move_destination(self, path, dest_root, filename, do_subfolder):
        """Work out the exact destination path a Move will write to, and
        make sure the destination folder exists. Done up front (on the main
        thread, before the worker starts) so the transient "Moving" record
        can be shown immediately, pointed at the real output path."""
        rec = self._records.get(path, {})
        group_path = rec.get("group_path")

        dest_dir = dest_root
        if do_subfolder:
            streamer = self._derive_streamer_name(path, group_path)
            dest_dir = os.path.join(dest_root, streamer)

        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            return None, f"Move failed: could not create destination folder ({exc})"

        return self._unique_path(os.path.join(dest_dir, filename)), None

    def _start_move(self, path, dest_root, filename, do_subfolder, do_fixup):
        if not path or not os.path.isfile(path):
            self._set_status("Move failed: file no longer exists")
            return
        with self._move_lock:
            if self._move_busy:
                self._set_status("A Move is already running - please wait")
                return
            final_path, err = self._compute_move_destination(
                path, dest_root, filename, do_subfolder)
            if final_path is None:
                self._set_status(err)
                return
            self._move_busy = True
            self._move_busy_path = path
            self._move_dest_path = final_path
            now = time.time()
            self._moving_records[final_path] = {
                "size": 0, "last_change": now, "rate": 0.0, "last_poll": now,
                "mtime": now, "status": "WRITING",
                "group_path": None, "group_label": "Moving",
            }
        self._set_status(f"Move started: {os.path.basename(path)}")
        t = threading.Thread(
            target=self._move_worker,
            args=(path, dest_root, filename, do_subfolder, do_fixup, final_path),
            daemon=True,
        )
        t.start()

    def _move_worker(self, path, dest_root, filename, do_subfolder, do_fixup, final_path):
        try:
            _ok, msg = self._do_move(path, dest_root, filename, do_subfolder, do_fixup, final_path)
            self._set_status(msg)
        except Exception as exc:
            self._set_status(f"Move failed: {exc}")
        finally:
            with self._move_lock:
                self._move_busy = False
                self._move_busy_path = None
                self._move_dest_path = None
                self._moving_records.pop(final_path, None)

    def _do_move(self, path, dest_root, filename, do_subfolder, do_fixup, final_path):
        """Runs on a background thread. Returns (ok, status_message).
        *final_path* is the exact destination path, already computed (and
        its parent folder already created) by _compute_move_destination."""
        dest_dir = os.path.dirname(final_path)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            return False, f"Move failed: could not create destination folder ({exc})"

        if do_fixup:
            found, ffmpeg_path = check_ffmpeg()
            if not found or not ffmpeg_path:
                return False, "Move failed: ffmpeg not found (Fixup was requested)"

            try:
                st = os.stat(path)
                orig_atime, orig_mtime = st.st_atime, st.st_mtime
            except OSError as exc:
                return False, f"Move failed: could not read source file ({exc})"

            cmd = self._build_fixup_cmd(ffmpeg_path, path, final_path)
            run_kwargs = {"capture_output": True, "text": True}
            if IS_WINDOWS:
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            try:
                result = subprocess.run(cmd, **run_kwargs)
            except Exception as exc:
                return False, f"Move failed: could not run ffmpeg ({exc})"

            if (result.returncode != 0 or not os.path.isfile(final_path)
                    or os.path.getsize(final_path) == 0):
                try:
                    if os.path.isfile(final_path):
                        os.remove(final_path)
                except OSError:
                    pass
                stderr_lines = (result.stderr or "").strip().splitlines()
                detail = stderr_lines[-1] if stderr_lines else f"ffmpeg exited {result.returncode}"
                return False, f"Move failed: {detail}"

            try:
                os.utime(final_path, (orig_atime, orig_mtime))
            except OSError:
                pass

            try:
                os.remove(path)
            except OSError as exc:
                return False, (f"Move completed but could not delete original "
                                f"({exc}); new file is at {final_path}")
        else:
            try:
                shutil.move(path, final_path)
            except OSError as exc:
                return False, f"Move failed: {exc}"

        return True, f"Move complete: {final_path}"

    @staticmethod
    def _derive_streamer_name(path, group_path):
        """Best-effort guess at the streamer name for *path*, for use when
        building the per-streamer subfolder (mirrors what the SUBFOLDERS
        global key does at record time). The File Manager only tracks
        files by OUTPUT_DIR, not by streamer, so: if the file already
        lives inside a subfolder beneath its tracked OUTPUT_DIR (e.g.
        because SUBFOLDERS was already on when it was recorded), that
        subfolder's name is reused. Otherwise this falls back to the
        file's own name (without extension)."""
        parent = os.path.dirname(path)
        if group_path and os.path.abspath(parent) != os.path.abspath(group_path):
            return os.path.basename(parent)
        stem = os.path.splitext(os.path.basename(path))[0]
        return stem.split()[0] if stem else ""

    # ── Fixup job (runs ffmpeg on a background thread) ──────────────────────

    def _start_fixup(self, path, delete_original, convert_mp4):
        if not path or not os.path.isfile(path):
            self._set_status("Fixup failed: file no longer exists")
            return
        with self._fixup_lock:
            if self._fixup_busy:
                self._set_status("A Fixup is already running - please wait")
                return
            self._fixup_busy = True
        self._set_status(f"Fixup started: {os.path.basename(path)}")
        t = threading.Thread(
            target=self._fixup_worker,
            args=(path, delete_original, convert_mp4),
            daemon=True,
        )
        t.start()

    def _fixup_worker(self, path, delete_original, convert_mp4):
        try:
            _ok, msg = self._do_fixup(path, delete_original, convert_mp4)
            self._set_status(msg)
        except Exception as exc:
            self._set_status(f"Fixup failed: {exc}")
        finally:
            with self._fixup_lock:
                self._fixup_busy = False

    @staticmethod
    def _unique_path(path):
        """Return *path*, or a "(2)"/"(3)"/... suffixed variant if it exists."""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        n = 2
        while True:
            candidate = f"{base} ({n}){ext}"
            if not os.path.exists(candidate):
                return candidate
            n += 1

    @staticmethod
    def _build_fixup_cmd(ffmpeg_path, input_path, output_path):
        """Stream-copy remux mirroring yt-dlp's fixup postprocessors: fix
        broken/discontinuous timestamps without re-encoding anything."""
        cmd = [
            ffmpeg_path, "-y",
            "-err_detect", "ignore_err",
            "-fflags", "+genpts",
            "-i", input_path,
            "-map", "0", "-c", "copy",
            "-avoid_negative_ts", "make_zero",
        ]
        in_ext = os.path.splitext(input_path)[1].lower()
        out_ext = os.path.splitext(output_path)[1].lower()
        if in_ext == ".ts" and out_ext in (".mp4", ".m4a", ".mov", ".m4v"):
            # Raw ADTS AAC (typical of HLS/.ts captures) needs its headers
            # converted for an mp4-family container - same fix yt-dlp's
            # m3u8 fixup applies when remuxing.
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd.append(output_path)
        return cmd

    def _do_fixup(self, path, delete_original, convert_mp4):
        """Runs on a background thread. Returns (ok, status_message)."""
        found, ffmpeg_path = check_ffmpeg()
        if not found or not ffmpeg_path:
            return False, "Fixup failed: ffmpeg not found"

        src_base, src_ext = os.path.splitext(path)
        out_ext = ".mp4" if convert_mp4 else src_ext
        same_ext = out_ext.lower() == src_ext.lower()

        try:
            st = os.stat(path)
            orig_atime, orig_mtime = st.st_atime, st.st_mtime
        except OSError as exc:
            return False, f"Fixup failed: could not read source file ({exc})"

        if delete_original and same_ext:
            # The original still occupies the final name, so remux into a
            # scratch file first and only claim the real name once the
            # original is gone.
            work_path = self._unique_path(src_base + ".fixup_tmp" + src_ext)
            final_path = src_base + src_ext
        else:
            # Either the container is changing (so the name is already
            # distinct from the original) or we're keeping the original
            # (so we need a distinct name too).
            suffix = out_ext if not same_ext else ("_fixed" + src_ext)
            work_path = self._unique_path(src_base + suffix)
            final_path = work_path

        cmd = self._build_fixup_cmd(ffmpeg_path, path, work_path)
        run_kwargs = {"capture_output": True, "text": True}
        if IS_WINDOWS:
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            result = subprocess.run(cmd, **run_kwargs)
        except Exception as exc:
            return False, f"Fixup failed: could not run ffmpeg ({exc})"

        if (result.returncode != 0 or not os.path.isfile(work_path)
                or os.path.getsize(work_path) == 0):
            try:
                if os.path.isfile(work_path):
                    os.remove(work_path)
            except OSError:
                pass
            stderr_lines = (result.stderr or "").strip().splitlines()
            detail = stderr_lines[-1] if stderr_lines else f"ffmpeg exited {result.returncode}"
            return False, f"Fixup failed: {detail}"

        # Carry the original file's date modified (and accessed) time over
        # to the new file.
        try:
            os.utime(work_path, (orig_atime, orig_mtime))
        except OSError:
            pass

        if delete_original:
            if self._delete_mode == DELETE_MODE_PERMANENT:
                ok, err = permanent_delete(path)
            else:
                ok, err = move_to_trash(path)
            if not ok:
                return False, (f"Fixup completed but could not delete original "
                                f"({err}); kept as {os.path.basename(work_path)}")
            if work_path != final_path:
                try:
                    os.rename(work_path, final_path)
                except OSError as exc:
                    return False, (f"Fixup completed and original deleted, but "
                                    f"rename failed ({exc}); output is at "
                                    f"{os.path.basename(work_path)}")

        return True, f"Fixup complete: {os.path.basename(final_path)}"

    # ── "Trim" popup ─────────────────────────────────────────────────────────

    _HMS_RE = re.compile(r"^\d{1,2}:[0-5]\d:[0-5]\d$")

    def open_trim_popup(self):
        if not self._selected_path:
            return
        self._trim_target = self._selected_path
        self._trim_start_buf = "00:00:00"
        self._trim_end_buf = "00:00:00"
        self._trim_delete_original = False
        self._trim_convert_mp4 = False
        self._trim_cursor = 0
        self._trim_field_cursor = len(self._trim_start_buf)
        self._trim_open = True

    def close_trim_popup(self):
        self._trim_open = False
        self._trim_target = None

    def _trim_active_buf(self):
        """Return (buf, setter) for whichever text field currently has focus,
        or (None, None) if a checkbox row is focused instead."""
        if self._trim_cursor == 0:
            return self._trim_start_buf, "_trim_start_buf"
        if self._trim_cursor == 1:
            return self._trim_end_buf, "_trim_end_buf"
        return None, None

    def _handle_trim_popup_key(self, key) -> bool:
        if key == 27:  # Esc -> cancel
            self.close_trim_popup()
            return True
        if key == curses.KEY_UP:
            self._trim_cursor = max(0, self._trim_cursor - 1)
            buf, _ = self._trim_active_buf()
            self._trim_field_cursor = len(buf) if buf is not None else 0
            return True
        if key == curses.KEY_DOWN:
            self._trim_cursor = min(3, self._trim_cursor + 1)
            buf, _ = self._trim_active_buf()
            self._trim_field_cursor = len(buf) if buf is not None else 0
            return True

        buf, attr_name = self._trim_active_buf()

        if key == ord(' '):
            if self._trim_cursor == 2:
                self._trim_delete_original = not self._trim_delete_original
            elif self._trim_cursor == 3:
                self._trim_convert_mp4 = not self._trim_convert_mp4
            else:
                # Typing a literal space inside a time field is meaningless;
                # ignore it there.
                pass
            return True
        if key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            self._submit_trim_popup()
            return True

        if attr_name is not None:
            cur = self._trim_field_cursor
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if cur > 0:
                    buf = buf[:cur - 1] + buf[cur:]
                    self._trim_field_cursor = cur - 1
                    setattr(self, attr_name, buf)
            elif key == curses.KEY_DC:
                if cur < len(buf):
                    buf = buf[:cur] + buf[cur + 1:]
                    setattr(self, attr_name, buf)
            elif key == curses.KEY_LEFT:
                if cur > 0:
                    self._trim_field_cursor = cur - 1
            elif key == curses.KEY_RIGHT:
                if cur < len(buf):
                    self._trim_field_cursor = cur + 1
            elif key == curses.KEY_HOME:
                self._trim_field_cursor = 0
            elif key == curses.KEY_END:
                self._trim_field_cursor = len(buf)
            elif (48 <= key <= 57) or key == ord(':'):
                buf = buf[:cur] + chr(key) + buf[cur:]
                self._trim_field_cursor = cur + 1
                setattr(self, attr_name, buf)
        # All other keys are consumed so nothing leaks to the dashboard.
        return True

    def _submit_trim_popup(self):
        start = self._trim_start_buf.strip()
        end = self._trim_end_buf.strip()
        if not self._HMS_RE.match(start) or not self._HMS_RE.match(end):
            self._set_status("Trim failed: times must be in HH:MM:SS format")
            return
        if self._to_seconds(end) <= self._to_seconds(start):
            self._set_status("Trim failed: End must be after Start")
            return
        target = self._trim_target
        delete_original = self._trim_delete_original
        convert_mp4 = self._trim_convert_mp4
        self.close_trim_popup()
        self._start_trim(target, start, end, delete_original, convert_mp4)

    @staticmethod
    def _to_seconds(hms: str) -> int:
        h, m, s = (int(p) for p in hms.split(":"))
        return h * 3600 + m * 60 + s

    def draw_trim_popup(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()

        box_w = min(50, w - 4)
        box_h = min(9, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_normal_1", db.C_NORMAL, False))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " TRIM ",
                       theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_chrome", db.C_CHROME, True))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Trim  Space: Toggle  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_invhead", db.C_INVHEAD, False))

        target_name = os.path.basename(self._trim_target or "")
        db.safe_addstr(stdscr, by1 + 1, bx1 + 2,
                       target_name[:box_w - 4],
                       theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_dim", db.C_DIM, False))

        rows = [
            ("Start:", self._trim_start_buf),
            ("End:  ", self._trim_end_buf),
        ]
        for i, (label, buf) in enumerate(rows):
            row_y = by1 + 3 + i
            is_sel = (self._trim_cursor == i)
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_hilight_1", db.C_HILIGHT, True)) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_normal_2", db.C_NORMAL, False)
            if is_sel:
                cur = self._trim_field_cursor
                display = buf[:cur] + "_" + buf[cur:]
            else:
                display = buf
            db.safe_addstr(stdscr, row_y, bx1 + 2,
                           f"{prefix}{label} {display}"[:box_w - 4], attr)

        checkboxes = [
            ("_trim_delete_original", "Delete original file"),
            ("_trim_convert_mp4",     "Convert to MP4 (no re-encode)"),
        ]
        check_row0 = by1 + 3 + len(rows) + 1
        for j, (attr_name, label) in enumerate(checkboxes):
            check_row = check_row0 + j
            cursor_idx = len(rows) + j
            is_sel = (self._trim_cursor == cursor_idx)
            checked = getattr(self, attr_name)
            box = "[x]" if checked else "[ ]"
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_hilight_2", db.C_HILIGHT, True)) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_normal_3", db.C_NORMAL, False)
            db.safe_addstr(stdscr, check_row, bx1 + 2,
                           f"{prefix}{box} {label}"[:box_w - 4], attr)

    # ── Trim job (runs ffmpeg on a background thread) ───────────────────────

    def _start_trim(self, path, start, end, delete_original, convert_mp4):
        if not path or not os.path.isfile(path):
            self._set_status("Trim failed: file no longer exists")
            return
        with self._trim_lock:
            if self._trim_busy:
                self._set_status("A Trim is already running - please wait")
                return
            self._trim_busy = True
        self._set_status(f"Trim started: {os.path.basename(path)}")
        t = threading.Thread(
            target=self._trim_worker,
            args=(path, start, end, delete_original, convert_mp4),
            daemon=True,
        )
        t.start()

    def _trim_worker(self, path, start, end, delete_original, convert_mp4):
        try:
            _ok, msg = self._do_trim(path, start, end, delete_original, convert_mp4)
            self._set_status(msg)
        except Exception as exc:
            self._set_status(f"Trim failed: {exc}")
        finally:
            with self._trim_lock:
                self._trim_busy = False

    def _do_trim(self, path, start, end, delete_original, convert_mp4):
        """Runs on a background thread. Returns (ok, status_message)."""
        found, ffmpeg_path = check_ffmpeg()
        if not found or not ffmpeg_path:
            return False, "Trim failed: ffmpeg not found"

        src_base, src_ext = os.path.splitext(path)
        out_ext = ".mp4" if convert_mp4 else src_ext
        work_path = self._unique_path(src_base + "_trimmed" + out_ext)

        cmd = [
            ffmpeg_path, "-y",
            "-i", path,
            "-ss", start, "-to", end,
            "-map", "0", "-c", "copy",
            "-avoid_negative_ts", "make_zero",
        ]
        if src_ext.lower() == ".ts" and out_ext.lower() in (".mp4", ".m4a", ".mov", ".m4v"):
            # Raw ADTS AAC (typical of HLS/.ts captures) needs its headers
            # converted for an mp4-family container - same fix Fixup's
            # "Convert to MP4" option applies.
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd.append(work_path)
        run_kwargs = {"capture_output": True, "text": True}
        if IS_WINDOWS:
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            result = subprocess.run(cmd, **run_kwargs)
        except Exception as exc:
            return False, f"Trim failed: could not run ffmpeg ({exc})"

        if (result.returncode != 0 or not os.path.isfile(work_path)
                or os.path.getsize(work_path) == 0):
            try:
                if os.path.isfile(work_path):
                    os.remove(work_path)
            except OSError:
                pass
            stderr_lines = (result.stderr or "").strip().splitlines()
            detail = stderr_lines[-1] if stderr_lines else f"ffmpeg exited {result.returncode}"
            return False, f"Trim failed: {detail}"

        if delete_original:
            if self._delete_mode == DELETE_MODE_PERMANENT:
                ok, err = permanent_delete(path)
            else:
                ok, err = move_to_trash(path)
            if not ok:
                return False, (f"Trim completed but could not delete original "
                                f"({err}); kept as {os.path.basename(work_path)}")

        return True, f"Trim complete: {os.path.basename(work_path)}"

    # ── Drawing ──────────────────────────────────────────────────────────────

    def draw(self, stdscr, y1, x1, y2, x2) -> None:
        db = self.dashboard
        db.draw_box(stdscr, y1, x1, y2, x2, db.C_CHROME)
        db.safe_addstr(stdscr, y1, x1 + 2, " FILE MANAGER ",
                       theme.attr(db, "file_manager_filemanagertab_draw_chrome", db.C_CHROME, True))

        dirs = self._get_output_dirs()
        if not dirs:
            db.safe_addstr(stdscr, y1 + 2, x1 + 2,
                           "No OUTPUT_DIR configured on any site.",
                           theme.attr(db, "file_manager_filemanagertab_draw_dim_1", db.C_DIM, False))
            return

        avail_w = (x2 - x1) - 3
        status_w = 8
        mod_w    = 20
        size_w   = 8
        rate_w   = 9
        fixed_cols = 2 + 1 + status_w + 1 + mod_w + 1 + size_w + 1 + rate_w
        name_w = max(12, avail_w - fixed_cols)

        col_status_x = x1 + 2 + name_w + 1
        col_mod_x    = col_status_x + status_w + 1
        col_size_x   = col_mod_x + mod_w + 1
        col_rate_x   = col_size_x + size_w + 1

        header_y = y1 + 1
        header_attr = theme.attr(db, "file_manager_filemanagertab_draw_normal", db.C_NORMAL, True)
        db.safe_addstr(stdscr, header_y, x1 + 2, "File".ljust(name_w)[:name_w], header_attr)
        db.safe_addstr(stdscr, header_y, col_status_x, "Status".ljust(status_w)[:status_w], header_attr)
        db.safe_addstr(stdscr, header_y, col_mod_x, "Date Modified".ljust(mod_w)[:mod_w], header_attr)
        db.safe_addstr(stdscr, header_y, col_size_x, "Size".ljust(size_w)[:size_w], header_attr)
        db.safe_addstr(stdscr, header_y, col_rate_x, "Rate".ljust(rate_w)[:rate_w], header_attr)

        list_y1 = header_y + 2           # leave row header_y+1 for status messages
        list_y2 = y2 - 1                 # leave the last row for status/help
        visible = max(1, list_y2 - list_y1)

        sel_row = None
        for i, r in enumerate(self._rows):
            if r[0] == "file" and r[1] == self._selected_path:
                sel_row = i
                break
        if sel_row is not None:
            if sel_row < self._scroll:
                self._scroll = sel_row
            elif sel_row >= self._scroll + visible:
                self._scroll = sel_row - visible + 1
        # Allow scroll to include a header row above the first file when
        # the user reached the top via the UP arrow.
        first_file = next((i for i, r in enumerate(self._rows) if r[0] == "file"), None)
        has_header_above = (first_file is not None and first_file > 0
                            and self._rows[0][0] == "header")
        if self._at_top and has_header_above:
            min_scroll = min(0, first_file - 1)
        else:
            min_scroll = 0
        self._scroll = max(min_scroll, min(self._scroll, max(0, len(self._rows) - visible)))

        row_y = list_y1
        loop_end = min(len(self._rows), self._scroll + visible)
        for i in range(self._scroll, loop_end):
            kind, payload, rec = self._rows[i]
            if kind == "header":
                db.safe_addstr(stdscr, row_y, x1 + 1, ("\u2500 " + payload)[:avail_w],
                               theme.attr(db, "file_manager_filemanagertab_draw_system_1", db.C_SYSTEM, True))
            elif kind == "empty":
                db.safe_addstr(stdscr, row_y, x1 + 3, payload[:avail_w],
                               theme.attr(db, "file_manager_filemanagertab_draw_dim_2", db.C_DIM, False))
            else:
                path = payload
                group_path = rec.get("group_path")
                if group_path and os.path.dirname(os.path.abspath(path)) != os.path.abspath(group_path):
                    # Nested inside a subfolder (e.g. a per-streamer folder
                    # created by SUBFOLDERS) - show the subfolder-relative
                    # path so it's clear where the file lives.
                    name = os.path.relpath(path, group_path).replace(os.sep, "/")
                else:
                    name = os.path.basename(path)
                status = rec.get("status", "IDLE")
                size_txt = human_size(rec.get("size"))
                rate_txt = human_rate(rec.get("rate")) if status == "WRITING" else "\u2014"
                mtime = rec.get("mtime")
                mod_txt = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                           if mtime else "\u2014")

                is_sel = (path == self._selected_path)
                if is_sel:
                    row_attr = theme.attr(db, "file_manager_filemanagertab_draw_hilight", db.C_HILIGHT, True)
                elif status == "WRITING":
                    row_attr = theme.attr(db, "file_manager_filemanagertab_draw_live", db.C_LIVE, True)
                else:
                    row_attr = theme.attr(db, "file_manager_filemanagertab_draw_dim_3", db.C_NORMAL, True)

                # Subfolder-relative paths (e.g. "StreamerName/file.mp4") get
                # their directory part drawn in a distinct color so nested
                # files stand out. Skipped on the highlighted row so the
                # subfolder text keeps the selection background.
                sub_prefix = None
                if not is_sel and "/" in name:
                    sub_prefix = name.rpartition("/")[0] + "/"

                is_moving = self._move_busy and self._move_busy_path == path
                name_lbl = name if not is_moving else name + " Moving..."
                name_col = name_lbl.ljust(name_w)[:name_w]
                if sub_prefix:
                    pf = name_col[:len(sub_prefix)]
                    rest = name_col[len(sub_prefix):]
                    db.safe_addstr(stdscr, row_y, x1 + 2, pf,
                                   theme.attr(db, "file_manager_filemanagertab_draw_system_2", db.C_WARN, False))
                    db.safe_addstr(stdscr, row_y, x1 + 2 + len(pf), rest, row_attr)
                else:
                    db.safe_addstr(stdscr, row_y, x1 + 2, name_col, row_attr)
                db.safe_addstr(stdscr, row_y, col_status_x, status.ljust(status_w)[:status_w], row_attr)
                db.safe_addstr(stdscr, row_y, col_mod_x, mod_txt.ljust(mod_w)[:mod_w], row_attr)
                db.safe_addstr(stdscr, row_y, col_size_x, size_txt.rjust(size_w)[:size_w], row_attr)
                db.safe_addstr(stdscr, row_y, col_rate_x, rate_txt.rjust(rate_w)[:rate_w], row_attr)

            # --- Add Scroll Arrows ---
            if i == self._scroll and self._scroll > 0:
                db.safe_addstr(stdscr, row_y, x2 - 2, "\u25b2",
                               theme.attr(db, "file_manager_filemanagertab_draw_live_2", db.C_LIVE, True))
            if i == loop_end - 1 and loop_end < len(self._rows):
                db.safe_addstr(stdscr, row_y, x2 - 2, "\u25bc",
                               theme.attr(db, "file_manager_filemanagertab_draw_live_3", db.C_LIVE, True))
            row_y += 1

        delete_lbl = "Trash" if self._delete_mode == DELETE_MODE_TRASH else "Permanent Delete"
        info = (f" Delete mode: {delete_lbl} (T to toggle)   "
                f"Sort: {_FM_SORT_LABELS.get(self._sort_key, self._sort_key)} ")
        info_attr = theme.attr(db, "file_manager_filemanagertab_draw_delete", db.C_DELETE, True) if self._delete_mode == DELETE_MODE_PERMANENT else theme.attr(db, "file_manager_filemanagertab_draw_dim_4", db.C_DIM, False)
        db.safe_addstr(stdscr, y2, x1 + 2, info[:avail_w], info_attr)

        if self._status_msg and (time.time() - self._status_msg_ts) < STATUS_MSG_TTL_S:
            db.safe_addstr(stdscr, y1 + 2, x1 + 2, self._status_msg[:avail_w],
                           theme.attr(db, "file_manager_filemanagertab_draw_warn", db.C_WARN, True))
