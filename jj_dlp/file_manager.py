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
    UP / DOWN     - move the selection (over both folder and file rows)
    ENTER         - on a file: open it with the OS default app
                    on a folder row: toggle collapse/expand
    SPACE         - on a file: open its containing folder, with the file
                    pre-selected/highlighted
                    on a folder row: open that folder
    DELETE        - remove the selected file (Trash or permanent delete,
                    see "Delete mode" below); no-op on a folder row
    S             - open the sort-order popup (persisted to global.json)
    T             - toggle delete mode between Trash and Permanent Delete
                    (persisted to global.json)
    M             - open the "File Options" popup for the selected file;
                    no-op on a folder row

When the global COLLAPSIBLE_FOLDERS key is on (default), files are grouped
by their first-level subfolder (e.g. the per-streamer folders created by
SUBFOLDERS) into collapsible/expandable folder rows. This grouping is
only active when the sort order is "Name (A-Z)" or "Name (Z-A)"; for any
other sort key, files are shown flat without collapsible folders.

Additionally, when multiple OUTPUT_DIRs exist and the same conditions
apply, the OUTPUT_DIR headers themselves become collapsible folder rows,
hiding all files under that directory when collapsed.
Collapse state is in-memory for the session only.

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

Selecting "Split" opens a popup with editable fields: Part length
(minutes), Overlap (seconds), First part number, Part number offset, and
Output directory (defaults to a "video_parts" folder beside the source
file), plus three action rows: "Start Job" (starts the split), "Stop Job"
(cancels a running split after a Yes/No confirmation), and — for files
jj-dlp is actively recording right now — "Restart the recording instead"
(forces the live recording to split immediately through the same
SPLIT_AFTER machinery, renaming the current file to _partN and
continuing into the next part). Mode is automatic: IDLE files use
Instant Mode, WRITING files use Catch-up Mode. Split runs on a background
thread via ffmpeg. A file with a job running shows a "*" to its left.
Opening Split again on that file offers "Stop Job" (kills ffmpeg
immediately, no wait, after a Yes/No confirmation); with no job running
it reports "Error: No job running".

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

from ast import Return
import os
import re
import shutil
import time
import platform
import subprocess
import threading
from collections import deque

import curses

from .deps import check_ffmpeg
from . import theme
from .logger import dbg


def _natural_sort_key(name):
    """Split a filename into text/number chunks for natural ordering.

    e.g. "file10.mp4" -> ["file", 10, ".mp4"] so it sorts after
    "file2.mp4" instead of before it (as a plain string compare would).
    """
    return [int(chunk) if chunk.isdigit() else chunk.lower()
            for chunk in re.split(r'(\d+)', name)]

try:
    from send2trash import send2trash as _send2trash
except ImportError:
    _send2trash = None

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

POLL_INTERVAL_S = 1.0      # how often we re-scan OUTPUT_DIRs
IDLE_THRESHOLD_S = 3.0     # size unchanged for this long => IDLE
STATUS_MSG_TTL_S = 4.0     # how long an inline status/error message lingers
# How long the sort‑mode transient popup stays visible after cycling
SORT_TRANSIENT_TTL_S = 2.0

DELETE_MODE_TRASH = "trash"
DELETE_MODE_PERMANENT = "permanent"
DELETE_MODE_DEFAULT = DELETE_MODE_TRASH

# ── Sort options for the File Manager tab (mirrors the Dashboard's "S" sort popup) ──
SORT_OPTIONS_FM = [
    ("name_asc",       "Name (A-Z) (grouped)"),
    ("name_desc",      "Name (Z-A) (grouped)"),
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
    ("split", "Split"),
]

# ── "Split" popup field rows (index order matters; see _split_active_buf) ──
SPLIT_FIELD_LABELS = [
    "Part length (minutes):",
    "Overlap (seconds):",
    "First part number:",
    "Part number offset:",
    "Output directory:",
]
SPLIT_ROW_START_JOB = len(SPLIT_FIELD_LABELS)  # "Start Job" action row
SPLIT_ROW_STOP = SPLIT_ROW_START_JOB + 1       # "Stop Job" action row
SPLIT_ROW_RESTART = SPLIT_ROW_STOP + 1         # "Restart the recording instead" action row

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
        dbg(f"open_file: {exc}")
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
        dbg(f"open_containing_folder: {exc}")
        return False, str(exc)


def move_to_trash(path):
    """Send *path* to the Recycle Bin / Trash."""
    abs_path = os.path.abspath(path)
    if _send2trash is not None:
        try:
            _send2trash(abs_path)
            return True, None
        except Exception as exc:
            dbg(f"move_to_trash: {exc}")
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
        dbg(f"move_to_trash: {exc}")
        return False, str(exc)


def permanent_delete(path):
    """Delete *path* immediately, with no recycle bin involved."""
    abs_path = os.path.abspath(path)
    try:
        os.remove(abs_path)
        return True, None
    except Exception as exc:
        dbg(f"permanent_delete: {exc}")
        return False, str(exc)


def permanent_delete_folder(path):
    """Delete the (empty) folder *path* immediately, with no recycle bin
    involved."""
    abs_path = os.path.abspath(path)
    try:
        os.rmdir(abs_path)
        return True, None
    except Exception as exc:
        dbg(f"permanent_delete_folder: {exc}")
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
        # path -> (size, time) — fine-grained size samples of the actively
        # recording files, used by sample_active_write_rates() to derive
        # spot rates / byte-deltas without re-walking the OUTPUT_DIRs.
        self._inst_rate_samples = {}
        # Flattened, already-sorted rows ready to draw:
        #   ("header", label_text, None)
        #   ("empty",  text,       None)
        #   ("folder", abs_path,   {"name": ..., "count": N, "collapsed": bool, "is_output_dir": bool})
        #   ("file",   path,       record_dict)
        self._rows = []

        self._selected_path = None
        # "file" or "folder" — which kind of row is currently selected.
        self._selected_kind = "file"
        # Absolute path when _selected_kind == "folder" (could be OUTPUT_DIR or subfolder).
        self._selected_folder = None
        # Absolute paths (OUTPUT_DIRs and subfolders) collapsed by the user this session
        # (gated by the COLLAPSIBLE_FOLDERS global key; never persisted).
        self._collapsed = set()
        self._scroll = 0
        self._at_top = False
        self._last_visible = 1  # rows visible in the list viewport, updated on draw
        self._last_poll = 0.0

        self._status_msg = ""
        self._status_msg_ts = 0.0

        self._sort_key, self._delete_mode = self._load_settings()
        self._sort_popup_until = 0.0          # epoch when transient sort popup expires
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

        # "Split" popup (fields + Start Job / Stop Job / Restart rows) +
        # background job state. self._split_jobs maps path -> {"proc": ...}.
        self._split_open = False
        self._split_target = None
        self._split_cursor = 0           # 0-4=fields, 5=Start Job, 6=Stop Job, 7=Restart
        self._split_field_cursor = 0
        self._split_len_buf = "30"
        self._split_overlap_buf = "5"
        self._split_first_buf = "1"
        self._split_offset_buf = "1"
        self._split_outdir_buf = ""
        self._split_jobs = {}
        self._split_jobs_lock = threading.Lock()

        # "Split" stop confirmation popup
        self._split_confirm_open = False
        self._split_confirm_target = None

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
        except Exception as e:
            dbg(f"_load_settings: {e}")
            return FM_SORT_DEFAULT, DELETE_MODE_DEFAULT

    def _save_settings(self):
        try:
            from .main import _update_global_json

            def _mutate(data):
                fm = data.setdefault("file_manager", {})
                fm["sort_key"] = self._sort_key
                fm["delete_mode"] = self._delete_mode
            _update_global_json(_mutate)
        except Exception as e:
            dbg(f"_save_settings: {e}")
            pass

    # ── OUTPUT_DIR discovery ────────────────────────────────────────────────

    def _get_output_dirs(self):
        """Ordered, de-duplicated list of (label, abs_path) - one per
        distinct OUTPUT_DIR across all configured sites."""
        seen = {}
        for site in self.dashboard.sites:
            try:
                cfg = site.get_cached_config()
            except Exception as e:
                dbg(f"_get_output_dirs: {e}")
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

    def maybe_poll(self, force=False, min_interval=None):
        """Re-scan OUTPUT_DIRs at most once every ``min_interval`` seconds
        (defaults to POLL_INTERVAL_S). Safe to call every frame from the
        draw loop — callers that only need a coarse rate figure (e.g. the
        system panel sidebar) can pass a larger ``min_interval`` so they
        don't force a full directory rescan on every redraw."""
        if min_interval is None:
            min_interval = POLL_INTERVAL_S
        now = time.time()
        if not force and (now - self._last_poll) < min_interval:
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

    def _active_recording_paths(self) -> set:
        """Absolute paths of every file currently being actively recorded by
        yt-dlp, gathered from each site's ``recording_output_paths`` registry
        (published by record_stream). Paths are normalized (abspath +
        normcase) so they line up with this tab's scan-derived ``_records``
        keys even across drive-letter case differences on Windows."""
        paths = set()
        try:
            for site in self.dashboard.sites:
                for p in site.recording_output_paths_snapshot():
                    try:
                        paths.add(os.path.normcase(os.path.abspath(p)))
                    except Exception as e:
                        dbg(f"_active_recording_paths: {e}")
                        continue
        except Exception as e:
            dbg(f"_active_recording_paths: {e}")
            pass
        return paths

    def sample_active_write_rates(self) -> tuple:
        """One combined sampling pass over every file currently being
        actively recorded by yt-dlp.

        Returns ``(inst_rate, bytes_grown)``:

          inst_rate  – sum of per-file Δsize/Δt spot rates since the
                       previous call, in bytes/sec. Bursty: near-zero
                       between yt-dlp's disk flushes, spiking right after
                       one — so a single spot reading frequently lands in a
                       quiet gap and comes back 0.
          bytes_grown – sum of positive per-file Δsize since the previous
                       call, in bytes. This is the integral the top-bar
                       graph can average over a whole GRAPH_SCALE window to
                       get a rate that never blanks while data is flowing.

        Sampling is a single getsize() per file — no os.walk, no
        _rebuild_rows — so it can be called at a fast sub-second cadence
        from the top-bar graph tick. Each path tracks its own (size, time)
        pair. Callers must call this exactly once per sub-sample (the two
        values share the same state); the graph tick does.
        """
        active = self._active_recording_paths()
        now = time.time()
        rate_total = 0.0
        bytes_total = 0.0
        for path in active:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            prev = self._inst_rate_samples.get(path)
            if prev is not None:
                dt = max(now - prev[1], 0.001)
                grown = max(0.0, size - prev[0])
                rate_total += grown / dt
                bytes_total += grown
            self._inst_rate_samples[path] = (size, now)
        if len(self._inst_rate_samples) > len(active) * 2 + 16:
            self._inst_rate_samples = {p: v for p, v in self._inst_rate_samples.items() if p in active}
        return rate_total, bytes_total

    def instantaneous_total_write_rate(self) -> float:
        """Backward-compatible wrapper: the instantaneous spot rate only."""
        return self.sample_active_write_rates()[0]

    # ── Sorting / row layout ────────────────────────────────────────────────

    def _sort_key_fn(self, path):
        rec = self._records.get(path, {})
        key = self._sort_key
        if key in ("name_asc", "name_desc"):
            return _natural_sort_key(os.path.basename(path))
        if key in ("status_writing", "status_idle"):
            return 0 if rec.get("status") == "WRITING" else 1
        if key in ("modified_new", "modified_old"):
            return int(rec.get("mtime", 0)) // 60 * 60
        if key in ("size_desc", "size_asc"):
            return rec.get("size", 0)
        if key in ("rate_desc", "rate_asc"):
            return rec.get("rate", 0.0)
        return _natural_sort_key(os.path.basename(path))

    @staticmethod
    def _collapsible_folders_enabled() -> bool:
        """True when the global COLLAPSIBLE_FOLDERS key is on."""
        try:
            from .main import load_global_config
            return bool(load_global_config().get("collapsible_folders", True))
        except Exception as e:
            dbg(f"_collapsible_folders_enabled: {e}")
            return True

    def _rebuild_rows(self, dirs):
        reverse = self._sort_key in _FM_SORT_REVERSE
        rows = []
        multi = len(dirs) > 1

        # OUTPUT_DIR rows: always collapsible but only shown when multiple dirs
        collapsible_output_dirs = True
        # Subfolder grouping: only when global setting is on AND sort is by Name
        collapsible_subfolders = self._collapsible_folders_enabled() and self._sort_key in ("name_asc", "name_desc")

        for label, folder in dirs:
            folder_abs = os.path.abspath(folder)
            files = [p for p, r in self._records.items() if r.get("group_path") == folder_abs]

            # Determine if we show a folder row for this OUTPUT_DIR (only if >1)
            show_output_dir_row = multi and collapsible_output_dirs

            if show_output_dir_row:
                collapsed = folder_abs in self._collapsed
                display_name = f"{label}  \u2014  {folder_abs}"
                rows.append(("folder", folder_abs,
                             {"name": display_name,
                              "count": len(files),
                              "collapsed": collapsed,
                              "is_output_dir": True,
                              "depth": 0}))
                if collapsed:
                    # Skip all contents under this OUTPUT_DIR
                    continue

            # --- Now display the files inside this OUTPUT_DIR ---
            if collapsible_subfolders:
                # Build a recursive folder structure
                # We'll create a map: full_abs_path -> list of files directly in that folder
                folder_map = {}
                for p in files:
                    parent = os.path.dirname(p)
                    folder_map.setdefault(parent, []).append(p)

                def process_folder(dir_path, depth):
                    # Get files directly in this folder
                    file_list = folder_map.get(dir_path, [])
                    file_list.sort(key=self._sort_key_fn, reverse=reverse)

                    # Find immediate subfolders (directories that contain files)
                    children = set()
                    for fpath in files:
                        parent = os.path.dirname(fpath)
                        if parent != dir_path and parent.startswith(dir_path + os.sep):
                            rel = os.path.relpath(parent, dir_path)
                            first_part = rel.split(os.sep)[0]
                            child_path = os.path.join(dir_path, first_part)
                            children.add(child_path)

                    # Process each child subfolder recursively
                    for child_path in sorted(children, key=lambda x: os.path.basename(x)):
                        collapsed_child = child_path in self._collapsed
                        child_name = os.path.basename(child_path)
                        # Count files inside this child subtree
                        count = sum(1 for f in files if f.startswith(child_path + os.sep))
                        rows.append(("folder", child_path,
                                     {"name": child_name,
                                      "count": count,
                                      "collapsed": collapsed_child,
                                      "is_output_dir": False,
                                      "depth": depth + 1}))
                        if not collapsed_child:
                            process_folder(child_path, depth + 1)

                    # Now add files directly in this folder (after subfolders)
                    for p in file_list:
                        rows.append(("file", p, self._records[p]))

                # Start processing from the OUTPUT_DIR root (depth=0)
                process_folder(folder_abs, 0)

            else:
                # Flat listing: just sort all files directly
                files.sort(key=self._sort_key_fn, reverse=reverse)
                for p in files:
                    rows.append(("file", p, self._records[p]))

            # Add "(no files)" row if appropriate
            if not files:
                if show_output_dir_row:
                    rows.append(("empty", "  (no files)", None))
                elif multi:
                    pass
                else:
                    # Single OUTPUT_DIR, no folder row – show "(no files)" directly
                    rows.append(("empty", "  (no files)", None))

        # Transient "Moving" section...
        if self._moving_records:
            rows.append(("header", "Moving", None))
            for p, rec in self._moving_records.items():
                rows.append(("file", p, rec))

        # --- Keep selection valid (unchanged) ---
        old_keys = [("folder", r[1]) if r[0] == "folder" else ("file", r[1])
                    for r in self._rows if r[0] in ("folder", "file")]
        cur_key = (self._selected_kind,
                   self._selected_folder if self._selected_kind == "folder" else self._selected_path)
        old_pos = old_keys.index(cur_key) if cur_key in old_keys else None

        self._rows = rows

        new_keys = [("folder", r[1]) if r[0] == "folder" else ("file", r[1])
                    for r in rows if r[0] in ("folder", "file")]
        if cur_key not in new_keys:
            if old_pos is not None and new_keys:
                new_pos = min(old_pos, len(new_keys) - 1)
                kind, ident = new_keys[new_pos]
            elif new_keys:
                kind, ident = new_keys[0]
            else:
                kind, ident = "file", None
            self._selected_kind = kind
            if kind == "file":
                self._selected_path = ident
                self._selected_folder = None
            else:
                self._selected_folder = ident
                self._selected_path = None

    def _toggle_folder_collapsed(self, abs_path: str) -> None:
        """Toggle collapse/expand for a folder row (OUTPUT_DIR or subfolder)."""
        if abs_path in self._collapsed:
            self._collapsed.discard(abs_path)
        else:
            self._collapsed.add(abs_path)
        self._rebuild_rows(self._get_output_dirs())

    # ── Selection movement ──────────────────────────────────────────────────

    def _select_row(self, row_idx) -> None:
        """Set selection state (_selected_kind/_selected_path/_selected_folder)
        from a row index into self._rows."""
        kind, ident = self._rows[row_idx][0], self._rows[row_idx][1]
        self._selected_kind = kind
        if kind == "file":
            self._selected_path = ident
            self._selected_folder = None
        else:
            self._selected_folder = ident
            self._selected_path = None

    def _cur_selectable_row_idx(self):
        """Index into self._rows of the currently selected folder/file row, or None."""
        cur_key = (self._selected_kind,
                   self._selected_folder if self._selected_kind == "folder" else self._selected_path)
        for i, r in enumerate(self._rows):
            if r[0] in ("folder", "file"):
                key = ("folder", r[1]) if r[0] == "folder" else ("file", r[1])
                if key == cur_key:
                    return i
        return None

    def move_selection(self, delta):
        sel_indices = [i for i, r in enumerate(self._rows) if r[0] in ("folder", "file")]
        if not sel_indices:
            self._selected_path = None
            self._selected_folder = None
            return
        cur_row_idx = self._cur_selectable_row_idx()
        if cur_row_idx is None:
            pos = 0 if delta >= 0 else len(sel_indices) - 1
        else:
            pos = sel_indices.index(cur_row_idx) + delta
            pos = max(0, min(len(sel_indices) - 1, pos))
        self._select_row(sel_indices[pos])

    def move_selection_page(self, direction):
        """Move the selection by a page (PageUp/PageDown), rather than jumping
        straight to the first/last row."""
        page = max(1, self._last_visible - 1)
        delta = -page if direction == "up" else page
        self.move_selection(delta)
        sel_indices = [i for i, r in enumerate(self._rows) if r[0] in ("folder", "file")]
        self._at_top = bool(sel_indices) and self._cur_selectable_row_idx() == sel_indices[0]

    def move_selection_edge(self, edge):
        """Move the selection straight to the first/last row (Home/End)."""
        sel_indices = [i for i, r in enumerate(self._rows) if r[0] in ("folder", "file")]
        if not sel_indices:
            self._selected_path = None
            self._selected_folder = None
            return
        target = sel_indices[0] if edge == "home" else sel_indices[-1]
        self._select_row(target)
        self._at_top = (edge == "home")

    # ── Key handling ─────────────────────────────────────────────────────────

    def handle_key(self, key) -> bool:
        """Returns True if the key was consumed by the File Manager tab."""
        if self._fixup_open:
            return self._handle_fixup_popup_key(key)
        if self._move_filename_open:
            return self._handle_move_filename_popup_key(key)
        if self._move_open:
            return self._handle_move_popup_key(key)
        if self._trim_open:
            return self._handle_trim_popup_key(key)
        if self._split_confirm_open:
            return self._handle_split_confirm_key(key)
        if self._split_open:
            return self._handle_split_popup_key(key)
        if self._menu_open:
            return self._handle_menu_popup_key(key)

        if key in (curses.KEY_PPAGE,):
            self.move_selection_page("up")
            return True
        if key in (curses.KEY_NPAGE,):
            self.move_selection_page("down")
            return True
        if key in (curses.KEY_UP,):
            self.move_selection(-1)
            sel_indices = [i for i, r in enumerate(self._rows) if r[0] in ("folder", "file")]
            self._at_top = bool(sel_indices) and self._cur_selectable_row_idx() == sel_indices[0]
            return True
        if key in (curses.KEY_DOWN,):
            self.move_selection(1)
            self._at_top = False
            return True
        if key in (curses.KEY_HOME,):
            self.move_selection_edge("home")
            return True
        if key in (curses.KEY_END,):
            self.move_selection_edge("end")
            return True
        if key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if self._selected_kind == "folder" and self._selected_folder:
                self._toggle_folder_collapsed(self._selected_folder)
            elif self._selected_path:
                ok, err = open_file(self._selected_path)
                if not ok:
                    self._set_status(f"Could not open file: {err}")
            return True
        if key == ord(' '):
            if self._selected_kind == "folder" and self._selected_folder:
                ok, err = open_file(self._selected_folder)
                if not ok:
                    self._set_status(f"Could not open folder: {err}")
            elif self._selected_path:
                ok, err = open_containing_folder(self._selected_path)
                if not ok:
                    self._set_status(f"Could not open folder: {err}")
            return True
        if key in (ord('s'), ord('S')) and not self.any_popup_open():
            # If the timer is already active, cycle to the next mode.
            # Otherwise, just show the current sort options without changing.
            if time.time() < self._sort_popup_until:
                self._cycle_sort(cycle=True)
            else:
                self._cycle_sort(cycle=False)
            return True
        if key in (curses.KEY_DC,):
            if self._selected_kind == "file" and self._selected_path:
                self._delete_selected()
            return True
        if key in (ord('t'), ord('T')):
            self._toggle_delete_mode()
            return True
        if key in (ord('m'), ord('M')):
            if self._selected_kind == "file" and self._selected_path:
                self.open_menu_popup()
            return True
        return False

    def _cycle_sort(self, cycle=True):
        """Show the sort popup; if cycle=True, advance to the next sort mode."""
        if cycle:
            idx = _FM_SORT_KEYS.index(self._sort_key)
            idx = (idx + 1) % len(_FM_SORT_KEYS)
            self._sort_key = _FM_SORT_KEYS[idx]
            self._save_settings()
            self._rebuild_rows(self._get_output_dirs())
        self._sort_popup_until = time.time() + SORT_TRANSIENT_TTL_S

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

        # Remember the file's parent folder and the OUTPUT_DIR root it
        # belongs to *before* deleting, so we can check afterward whether
        # the (sub)folder it lived in is now empty.
        rec = self._records.get(path, {})
        parent_dir = os.path.dirname(os.path.abspath(path))
        output_dir_root = rec.get("group_path")

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

        folder_note = self._maybe_delete_empty_folder(parent_dir, output_dir_root)

        self._rebuild_rows(self._get_output_dirs())
        mode_lbl = "Trash" if self._delete_mode == DELETE_MODE_TRASH else "Permanent"
        self._set_status(f"Deleted ({mode_lbl}): {os.path.basename(path)}{folder_note}")

    @staticmethod
    def _delete_empty_enabled() -> bool:
        """True when the global DELETE_EMPTY key is on."""
        try:
            from .main import load_global_config
            return bool(load_global_config().get("delete_empty", False))
        except Exception as e:
            dbg(f"_delete_empty_enabled: {e}")
            return False

    def _maybe_delete_empty_folder(self, folder, output_dir_root):
        """After deleting a file, remove *folder* (via the current Delete
        mode) if it is now empty of files/folders - but never the
        OUTPUT_DIR root itself, only subfolders within it.

        Returns a short human-readable suffix describing what happened
        (empty string if nothing was removed), for appending to the
        delete status message.
        """
        if not self._delete_empty_enabled():
            return ""

        if not folder or not output_dir_root:
            return ""

        folder_abs = os.path.abspath(folder)
        root_abs = os.path.abspath(output_dir_root)

        # Never touch the OUTPUT_DIR itself.
        if folder_abs == root_abs:
            return ""

        # Only ever remove folders that are actually nested inside the
        # OUTPUT_DIR - never anything outside of it.
        try:
            if os.path.commonpath([folder_abs, root_abs]) != root_abs:
                return ""
        except ValueError:
            # e.g. paths on different drives on Windows.
            return ""

        try:
            if not os.path.isdir(folder_abs) or os.listdir(folder_abs):
                return ""
        except OSError:
            return ""

        if self._delete_mode == DELETE_MODE_PERMANENT:
            ok, err = permanent_delete_folder(folder_abs)
        else:
            ok, err = move_to_trash(folder_abs)

        if not ok:
            return (f"; could not remove empty folder "
                    f"{os.path.basename(folder_abs)}: {err}")

        return f"; removed empty folder {os.path.basename(folder_abs)}"

    def _set_status(self, msg):
        self._status_msg = msg
        self._status_msg_ts = time.time()

    # ── "File Options" popup (M key) ────────────────────────────────────────

    def any_popup_open(self) -> bool:
        """True if any of this tab's popups (sort / menu / fixup / move) is open."""
        return (self._menu_open or self._fixup_open
                or self._move_open or self._move_filename_open
                or self._trim_open or self._split_open or self._split_confirm_open)

    def draw_popups(self, stdscr) -> None:
        """Draw whichever of this tab's popups is currently open."""
        if self._menu_open:
            self.draw_menu_popup(stdscr)
        elif self._fixup_open:
            self.draw_fixup_popup(stdscr)
        elif self._move_filename_open:
            self.draw_move_filename_popup(stdscr)
        elif self._move_open:
            self.draw_move_popup(stdscr)
        elif self._trim_open:
            self.draw_trim_popup(stdscr)
        elif self._split_confirm_open:
            self.draw_split_confirm_popup(stdscr)
        elif self._split_open:
            self.draw_split_popup(stdscr)
        # Transient sort popup (display only, no interaction)
        if time.time() < self._sort_popup_until:
            self._draw_sort_transient_popup(stdscr)

    def _draw_sort_transient_popup(self, stdscr):
        """Draw a styled list popup showing all sort options,
        with the current mode highlighted – exactly like the color‑schemes popup.
        """
        db = self.dashboard
        h, w = stdscr.getmaxyx()

        options = SORT_OPTIONS_FM
        n = len(options)

        # Compute box dimensions (matching the color‑schemes popup style)
        label_width = max(len(label) for _, label in options) + 4
        title = " SORT FILES "
        footer = " S: next sort "
        box_w = min(max(len(title), label_width + 4, len(footer) + 4) + 4, w - 4)
        box_h = min(n + 4, h - 4)          # title row + n rows + footer row + borders
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        # Fill background
        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           theme.attr(db, "main_jjdlpdashboard_draw_scheme_popup_normal_1"))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, title,
                       theme.attr(db, "main_jjdlpdashboard_draw_scheme_popup_hilight"))
        db.safe_addstr(stdscr, by2, bx1 + 2, footer,
                       theme.attr(db, "main_jjdlpdashboard_draw_scheme_popup_invhead"))

        # List each sort option
        for i, (sort_key, label) in enumerate(options):
            row_y = by1 + 2 + i
            if row_y >= by2:
                break
            is_current = (sort_key == self._sort_key)
            prefix = "* " if is_current else "  "
            attr = (theme.attr(db, "main_jjdlpdashboard_draw_scheme_popup_live")
                    if is_current
                    else theme.attr(db, "main_jjdlpdashboard_draw_scheme_popup_normal_2"))
            db.safe_addstr(stdscr, row_y, bx1 + 2,
                           (prefix + label)[:box_w - 4], attr)

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
            elif action_key == "split":
                self.open_split_popup()
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
                           theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_normal_1"))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " FILE OPTIONS ",
                       theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_chrome"))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Select  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_invhead"))

        for i, (_action_key, label) in enumerate(FILE_MENU_OPTIONS):
            row_y = by1 + 1 + i
            is_sel = (i == self._menu_sel)
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_hilight")) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_menu_popup_normal_2")
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

        box_w = min(65, w - 4)
        box_h = min(n + 5, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_normal_1"))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " FIXUP ",
                       theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_chrome"))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Space: Toggle  Enter: Run  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_invhead"))

        target_name = os.path.basename(self._fixup_target or "")
        db.safe_addstr(stdscr, by1 + 1, bx1 + 2,
                       target_name[:box_w - 4],
                       theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_dim"))

        for i, (check_key, label) in enumerate(FIXUP_CHECK_ITEMS):
            row_y = by1 + 3 + i
            is_sel = (i == self._fixup_cursor)
            checked = self._fixup_checks.get(check_key, False)
            box = "[x]" if checked else "[ ]"
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_hilight")) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_fixup_popup_normal_2")
            db.safe_addstr(stdscr, row_y, bx1 + 2,
                           f"{prefix}{box} {label}"[:box_w - 4], attr)

    # ── "Move" destination-picker popup ──────────────────────────────────────

    @staticmethod
    def _get_destinations():
        """Configured DESTINATIONS paths (global.conf), in order."""
        try:
            from .main import load_global_config
            return list(load_global_config().get("destinations", []))
        except Exception as e:
            dbg(f"_get_destinations: {e}")
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
        except Exception as e:
            dbg(f"_open_configure_destination: {e}")
            pass

    def draw_move_popup(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        n_dest = len(self._move_destinations)
        n_checks = len(MOVE_CHECK_ITEMS)

        box_w = min(80, w - 4)
        # 1 "Select a destination:" + n_dest rows + 1 "Configure a new
        # destination" row + 1 blank + n_checks checkbox rows
        inner_rows = 1 + n_dest + 1 + 1 + n_checks
        box_h = min(inner_rows + 4, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), theme.attr(db, "file_manager_filemanagertab_draw_move_popup_normal_1"))
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " MOVE ",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_popup_chrome"))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Select  Space: Toggle  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_popup_invhead"))

        row = by1 + 1
        db.safe_addstr(stdscr, row, bx1 + 2, "Select a destination:",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_popup_dim"))
        row += 1

        configure_idx = n_dest
        for i in range(n_dest):
            is_sel = (self._move_cursor == i)
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_move_popup_hilight_1")) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_move_popup_normal_2")
            label = self._move_destinations[i]
            db.safe_addstr(stdscr, row, bx1 + 2, (prefix + label)[:box_w - 4], attr)
            row += 1

        is_sel = (self._move_cursor == configure_idx)
        prefix = "> " if is_sel else "  "
        attr = (theme.attr(db, "file_manager_filemanagertab_draw_move_popup_hilight_2")) if is_sel \
            else theme.attr(db, "file_manager_filemanagertab_draw_move_popup_system")
        db.safe_addstr(stdscr, row, bx1 + 2,
                       (prefix + "Configure a new destination")[:box_w - 4], attr)
        row += 2

        for i, (check_key, label) in enumerate(MOVE_CHECK_ITEMS):
            idx = configure_idx + 1 + i
            is_sel = (self._move_cursor == idx)
            checked = self._move_checks.get(check_key, False)
            box = "[x]" if checked else "[ ]"
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_move_popup_hilight_3")) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_move_popup_normal_3")
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

        box_w = min(83, w - 4)
        box_h = min(9, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1), theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_normal_1"))
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " MOVE ",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_chrome"))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Start Move  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_invhead"))

        row = by1 + 1
        db.safe_addstr(stdscr, row, bx1 + 2,
                       f"Streamer: {self._move_filename_streamer}",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_dim"))
        row += 2
        db.safe_addstr(stdscr, row, bx1 + 2,
                       f"Destination: {self._move_filename_dest or ''}"[:box_w - 4],
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_dim"))
        row += 2
        db.safe_addstr(stdscr, row, bx1 + 2, "Filename:",
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_warn"))
        row += 1
        buf = self._move_filename_buf
        cur = self._move_filename_cursor
        display = buf[:cur] + "_" + buf[cur:]
        db.safe_addstr(stdscr, row, bx1 + 2,
                       display[:box_w - 4],
                       theme.attr(db, "file_manager_filemanagertab_draw_move_filename_p_normal_2"))

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
        # Captured before the move, same as the delete flow, so a
        # now-empty source subfolder can be cleaned up afterward.
        rec = self._records.get(path, {})
        parent_dir = os.path.dirname(os.path.abspath(path))
        output_dir_root = rec.get("group_path")
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
            args=(path, dest_root, filename, do_subfolder, do_fixup, final_path,
                  parent_dir, output_dir_root),
            daemon=True,
        )
        t.start()

    def _move_worker(self, path, dest_root, filename, do_subfolder, do_fixup, final_path,
                      parent_dir, output_dir_root):
        try:
            _ok, msg = self._do_move(path, dest_root, filename, do_subfolder, do_fixup, final_path,
                                      parent_dir, output_dir_root)
            self._set_status(msg)
        except Exception as exc:
            dbg(f"_move_worker: {exc}")
            self._set_status(f"Move failed: {exc}")
        finally:
            with self._move_lock:
                self._move_busy = False
                self._move_busy_path = None
                self._move_dest_path = None
                self._moving_records.pop(final_path, None)

    def _do_move(self, path, dest_root, filename, do_subfolder, do_fixup, final_path,
                 parent_dir, output_dir_root):
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
                dbg(f"_do_move: {exc}")
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

        folder_note = self._maybe_delete_empty_folder(parent_dir, output_dir_root)
        return True, f"Move complete: {final_path}{folder_note}"

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
            dbg(f"_fixup_worker: {exc}")
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
            dbg(f"_do_fixup: {exc}")
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

        box_w = min(63, w - 4)
        box_h = min(9, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_normal_1"))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " TRIM ",
                       theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_chrome"))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Trim  Space: Toggle  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_invhead"))

        target_name = os.path.basename(self._trim_target or "")
        db.safe_addstr(stdscr, by1 + 1, bx1 + 2,
                       target_name[:box_w - 4],
                       theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_dim"))

        rows = [
            ("Start:", self._trim_start_buf),
            ("End:  ", self._trim_end_buf),
        ]
        for i, (label, buf) in enumerate(rows):
            row_y = by1 + 3 + i
            is_sel = (self._trim_cursor == i)
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_hilight_1")) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_normal_2")
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
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_hilight_2")) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_trim_popup_normal_3")
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
            dbg(f"_trim_worker: {exc}")
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
            dbg(f"_do_trim: {exc}")
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

    # ── "Split" popup ────────────────────────────────────────────────────────

    def open_split_popup(self):
        if not self._selected_path:
            return
        path = self._selected_path
        self._split_target = path
        if path in self._split_jobs:
            # A job is already running for this file - the popup will only
            # offer "Stop Job" (see draw_split_popup/_handle_split_popup_key).
            self._split_open = True
            return
        self._split_len_buf = "30"
        self._split_overlap_buf = "5"
        self._split_first_buf = "1"
        self._split_offset_buf = "1"
        self._split_outdir_buf = os.path.join(os.path.dirname(path), "video_parts")
        self._split_cursor = 0
        self._split_field_cursor = len(self._split_len_buf)
        self._split_open = True

    def close_split_popup(self):
        self._split_open = False
        self._split_target = None

    def _split_active_buf(self):
        """Return (buf, attr_name) for the field row currently focused, or
        (None, None) when one of the action rows (Start Job / Stop Job /
        Restart) is focused instead."""
        mapping = {
            0: "_split_len_buf",
            1: "_split_overlap_buf",
            2: "_split_first_buf",
            3: "_split_offset_buf",
            4: "_split_outdir_buf",
        }
        attr_name = mapping.get(self._split_cursor)
        if attr_name is None:
            return None, None
        return getattr(self, attr_name), attr_name

    def _split_max_row(self):
        """Highest selectable row index in the split popup. The "Restart the
        recording now" row is always shown, but selecting it without an
        active recording will result in an error message."""
        # Always show the restart row
        return SPLIT_ROW_RESTART
        
    def _find_recording_owner(self, path):
        """Return (site, streamer) whose yt-dlp process is currently writing
        *path*, or (None, None) when no site is recording that exact file
        right now. Paths are normalized so they line up across drive-letter
        case differences on Windows."""
        if not path:
            return None, None
        key = os.path.normcase(os.path.abspath(path))
        try:
            for site in self.dashboard.sites:
                with site.lock:
                    for streamer, out_path in site.recording_output_paths.items():
                        if os.path.normcase(os.path.abspath(out_path)) == key:
                            return site, streamer
        except Exception as e:
            dbg(f"_find_recording_owner: {e}")
            pass
        return None, None

    def _restart_recording_now(self, path):
        """"Restart the recording instead": force the SPLIT_AFTER split for the
        streamer currently recording *path*, so the current file is renamed
        to _partN and recording continues into the next part. Only valid for
        files jj-dlp is actively recording. Returns True on success, False if
        no active recording is found (and sets an error status)."""        
        site, streamer = self._find_recording_owner(path)
        if site is None or not streamer:
            self._set_status("Restart failed: this file is not currently being recorded.")
            return False
        site.request_manual_split(streamer)
        self._set_status(f"Recording restart requested: {streamer}")
        return True

    def _handle_split_popup_key(self, key) -> bool:
        path = self._split_target
        running = path in self._split_jobs

        if key == 27:  # Esc -> cancel
            self.close_split_popup()
            return True

        if running:
            # Only action available: Stop Job.
            if key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459, ord(' ')):
                self.close_split_popup()
                self.open_split_confirm_popup(path)
            return True

        if key == curses.KEY_UP:
            self._split_cursor = max(0, self._split_cursor - 1)
            buf, _ = self._split_active_buf()
            self._split_field_cursor = len(buf) if buf is not None else 0
            return True
        if key == curses.KEY_DOWN:
            self._split_cursor = min(self._split_max_row(), self._split_cursor + 1)
            buf, _ = self._split_active_buf()
            self._split_field_cursor = len(buf) if buf is not None else 0
            return True
        if key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if self._split_cursor == SPLIT_ROW_START_JOB:
                self._submit_split_popup()
            elif self._split_cursor == SPLIT_ROW_STOP:
                self.close_split_popup()
                self.open_split_confirm_popup(path)
            elif self._split_cursor == SPLIT_ROW_RESTART:
                # Attempt restart; only close the popup if it succeeds
                if self._restart_recording_now(path):
                    self.close_split_popup()
                # If restart failed, an error status is already set and the popup stays open
            return True

        buf, attr_name = self._split_active_buf()
        if attr_name is not None:
            cur = self._split_field_cursor
            is_outdir = (self._split_cursor == 4)
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if cur > 0:
                    buf = buf[:cur - 1] + buf[cur:]
                    self._split_field_cursor = cur - 1
                    setattr(self, attr_name, buf)
            elif key == curses.KEY_DC:
                if cur < len(buf):
                    buf = buf[:cur] + buf[cur + 1:]
                    setattr(self, attr_name, buf)
            elif key == curses.KEY_LEFT:
                if cur > 0:
                    self._split_field_cursor = cur - 1
            elif key == curses.KEY_RIGHT:
                if cur < len(buf):
                    self._split_field_cursor = cur + 1
            elif key == curses.KEY_HOME:
                self._split_field_cursor = 0
            elif key == curses.KEY_END:
                self._split_field_cursor = len(buf)
            elif is_outdir and 32 <= key <= 126:
                # Output dir accepts any printable path character.
                buf = buf[:cur] + chr(key) + buf[cur:]
                self._split_field_cursor = cur + 1
                setattr(self, attr_name, buf)
            elif not is_outdir and 48 <= key <= 57:
                # Numeric fields: digits only.
                buf = buf[:cur] + chr(key) + buf[cur:]
                self._split_field_cursor = cur + 1
                setattr(self, attr_name, buf)
        # All other keys are consumed so nothing leaks to the dashboard.
        return True

    def _submit_split_popup(self):
        def _pos_int(buf):
            try:
                return int(buf.strip())
            except ValueError:
                return None

        length = _pos_int(self._split_len_buf)
        overlap = _pos_int(self._split_overlap_buf)
        first_part = _pos_int(self._split_first_buf)
        offset = _pos_int(self._split_offset_buf)
        outdir = self._split_outdir_buf.strip()

        if length is None or length <= 0:
            self._set_status("Split failed: Part length must be a positive number")
            return
        if overlap is None or overlap < 0:
            self._set_status("Split failed: Overlap must be zero or greater")
            return
        if overlap >= length * 60:
            self._set_status("Split failed: Overlap must be shorter than part length")
            return
        if first_part is None or first_part <= 0:
            self._set_status("Split failed: First part number must be a positive number")
            return
        if offset is None or offset < 1:
            self._set_status("Split failed: Part number offset must be 1 or greater")
            return
        if not outdir:
            self._set_status("Split failed: Output directory cannot be empty")
            return

        target = self._split_target
        self.close_split_popup()
        self._start_split(target, length, overlap, first_part, offset, outdir)

    def draw_split_popup(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        path = self._split_target
        running = path in self._split_jobs

        if running:
            box_w = min(58, w - 4)
            box_h = min(5, h - 4)
            by1 = (h - box_h) // 2
            bx1 = (w - box_w) // 2
            by2 = by1 + box_h
            bx2 = bx1 + box_w

            for y in range(by1, by2 + 1):
                db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                               theme.attr(db, "file_manager_filemanagertab_draw_split_popup_normal_1"))

            db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
            db.safe_addstr(stdscr, by1, bx1 + 2, " SPLIT ",
                           theme.attr(db, "file_manager_filemanagertab_draw_split_popup_chrome_1"))
            db.safe_addstr(stdscr, by2, bx1 + 2,
                           " Enter: Stop Job  Esc: Cancel ",
                           theme.attr(db, "file_manager_filemanagertab_draw_split_popup_invhead_1"))

            target_name = os.path.basename(path or "")
            db.safe_addstr(stdscr, by1 + 1, bx1 + 2,
                           target_name[:box_w - 4],
                           theme.attr(db, "file_manager_filemanagertab_draw_split_popup_dim_1"))
            db.safe_addstr(stdscr, by1 + 3, bx1 + 2,
                           "> Stop Job"[:box_w - 4],
                           theme.attr(db, "file_manager_filemanagertab_draw_split_popup_hilight_1"))
            return

        box_w = min(75, w - 4)
        box_h = min(15, h - 4)   # enough room for all rows (including restart)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           theme.attr(db, "file_manager_filemanagertab_draw_split_popup_normal_2"))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " SPLIT ",
                       theme.attr(db, "file_manager_filemanagertab_draw_split_popup_chrome_2"))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Select  Esc: Cancel ",
                       theme.attr(db, "file_manager_filemanagertab_draw_split_popup_invhead_2"))

        target_name = os.path.basename(path or "")
        db.safe_addstr(stdscr, by1 + 1, bx1 + 2,
                       target_name[:box_w - 4],
                       theme.attr(db, "file_manager_filemanagertab_draw_split_popup_dim_2"))

        rec = self._records.get(path, {})
        mode_lbl = "Catch-up" if rec.get("status") == "WRITING" else "Instant"
        db.safe_addstr(stdscr, by1 + 2, bx1 + 2,
                       f"Mode: {mode_lbl}"[:box_w - 4],
                       theme.attr(db, "file_manager_filemanagertab_draw_split_popup_system"))

        bufs = [self._split_len_buf, self._split_overlap_buf,
                self._split_first_buf, self._split_offset_buf, self._split_outdir_buf]
        for i, (label, buf) in enumerate(zip(SPLIT_FIELD_LABELS, bufs)):
            row_y = by1 + 4 + i
            is_sel = (self._split_cursor == i)
            prefix = "> " if is_sel else "  "
            attr = (theme.attr(db, "file_manager_filemanagertab_draw_split_popup_hilight_2")) if is_sel \
                else theme.attr(db, "file_manager_filemanagertab_draw_split_popup_normal_3")
            if is_sel:
                cur = self._split_field_cursor
                display = buf[:cur] + "_" + buf[cur:]
            else:
                display = buf
            db.safe_addstr(stdscr, row_y, bx1 + 2,
                           f"{prefix}{label} {display}"[:box_w - 4], attr)

        start_job_y = by1 + 4 + len(SPLIT_FIELD_LABELS) + 1
        is_sel = (self._split_cursor == SPLIT_ROW_START_JOB)
        prefix = "> " if is_sel else "  "
        attr = (theme.attr(db, "file_manager_filemanagertab_draw_split_popup_hilight_4")) if is_sel \
            else theme.attr(db, "file_manager_filemanagertab_draw_split_popup_normal_5")
        db.safe_addstr(stdscr, start_job_y, bx1 + 2,
                       f"{prefix}Start Job"[:box_w - 4], attr)

        stop_row_y = start_job_y + 1
        is_sel = (self._split_cursor == SPLIT_ROW_STOP)
        prefix = "> " if is_sel else "  "
        attr = (theme.attr(db, "file_manager_filemanagertab_draw_split_popup_hilight_3")) if is_sel \
            else theme.attr(db, "file_manager_filemanagertab_draw_split_popup_normal_4")
        db.safe_addstr(stdscr, stop_row_y, bx1 + 2,
                       f"{prefix}Stop Job"[:box_w - 4], attr)

        # Always draw the restart row
        restart_row_y = stop_row_y + 2
        is_sel = (self._split_cursor == SPLIT_ROW_RESTART)
        prefix = "> " if is_sel else "  "
        attr = (theme.attr(db, "file_manager_filemanagertab_draw_split_popup_hilight_5")) if is_sel \
            else theme.attr(db, "file_manager_filemanagertab_draw_split_popup_normal_6")
        db.safe_addstr(stdscr, restart_row_y, bx1 + 2,
                       f"{prefix}Restart the recording instead"[:box_w - 4], attr)

    # ── "Split" stop confirmation popup ─────────────────────────────────────

    def open_split_confirm_popup(self, path):
        if path not in self._split_jobs:
            self._set_status("Error: No job running")
            return
        self._split_confirm_target = path
        self._split_confirm_open = True

    def close_split_confirm_popup(self):
        self._split_confirm_open = False
        self._split_confirm_target = None

    def _handle_split_confirm_key(self, key) -> bool:
        if key in (27, ord('n'), ord('N')):  # Esc / No -> cancel
            self.close_split_confirm_popup()
        elif key in (ord('y'), ord('Y'), ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            path = self._split_confirm_target
            self.close_split_confirm_popup()
            self._stop_split(path)
        # All other keys are consumed so nothing leaks to the dashboard.
        return True

    def draw_split_confirm_popup(self, stdscr) -> None:
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        msg = "Are you sure you want to cancel the splitting job?"

        box_w = min(int((len(msg) + 6) * 1.25), w - 4)
        box_h = min(5, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            db.safe_addstr(stdscr, y, bx1, " " * (box_w + 1),
                           theme.attr(db, "file_manager_filemanagertab_draw_split_confirm_normal"))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " CONFIRM ",
                       theme.attr(db, "file_manager_filemanagertab_draw_split_confirm_chrome"))
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Y: Yes  N/Esc: No ",
                       theme.attr(db, "file_manager_filemanagertab_draw_split_confirm_invhead"))
        db.safe_addstr(stdscr, by1 + 2, bx1 + 3,
                       msg[:box_w - 6],
                       theme.attr(db, "file_manager_filemanagertab_draw_split_confirm_warn"))

    # ── Split job (runs ffmpeg on a background thread; killed, not waited,
    #    on Stop Job) ───────────────────────────────────────────────────────

    @staticmethod
    def _probe_duration_seconds(path):
        """Return the media duration of *path* in seconds (float), or None."""
        try:
            from .main import _resolve_ffprobe_path
            ffprobe_path = _resolve_ffprobe_path()
        except Exception as e:
            dbg(f"_probe_duration_seconds: {e}")
            ffprobe_path = None
        if not ffprobe_path:
            return None
        run_kwargs = {"capture_output": True, "text": True, "timeout": 15}
        if IS_WINDOWS:
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                **run_kwargs,
            )
            return float(result.stdout.strip())
        except Exception as e:
            dbg(f"_probe_duration_seconds: {e}")
            return None

    def _start_split(self, path, length_min, overlap_s, first_part, offset, outdir):
        if not path or not os.path.isfile(path):
            self._set_status("Split failed: file no longer exists")
            return
        with self._split_jobs_lock:
            if path in self._split_jobs:
                self._set_status("A Split job is already running for this file")
                return
        found, ffmpeg_path = check_ffmpeg()
        if not found or not ffmpeg_path:
            self._set_status("Split failed: ffmpeg not found")
            return
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as exc:
            self._set_status(f"Split failed: could not create output directory ({exc})")
            return

        rec = self._records.get(path, {})
        mode = "catchup" if rec.get("status") == "WRITING" else "instant"

        job = {"proc": None, "stop": False}
        with self._split_jobs_lock:
            self._split_jobs[path] = job
        t = threading.Thread(
            target=self._split_worker,
            args=(path, length_min * 60, overlap_s, first_part, offset,
                  outdir, mode, ffmpeg_path, job),
            daemon=True,
        )
        job["thread"] = t
        t.start()
        self._set_status("The splitting job has been started.  "
                          "Cancel the job from the File Options menu.")

    def _stop_split(self, path):
        with self._split_jobs_lock:
            job = self._split_jobs.pop(path, None)
        if not job:
            self._set_status("Error: No job running")
            return
        job["stop"] = True
        proc = job.get("proc")
        if proc is not None:
            try:
                proc.kill()  # don't wait for ffmpeg to finish
            except Exception as e:
                dbg(f"_stop_split: {e}")
                pass
        self._set_status(f"Splitting job cancelled: {os.path.basename(path)}")

    def _split_worker(self, path, part_length_s, overlap_s, first_part, offset,
                       outdir, mode, ffmpeg_path, job):
        try:
            self._run_split_job(path, part_length_s, overlap_s, first_part,
                                 offset, outdir, mode, ffmpeg_path, job)
        except Exception as exc:
            if not job.get("stop"):
                self._set_status(f"Split failed: {exc}")
        finally:
            with self._split_jobs_lock:
                # Only remove ourselves - Stop Job already pops the entry.
                if self._split_jobs.get(path) is job:
                    self._split_jobs.pop(path, None)

    def _run_split_job(self, path, part_length_s, overlap_s, first_part, offset,
                        outdir, mode, ffmpeg_path, job):
        part_index = first_part
        start_time = 0.0
        step = max(1, part_length_s - overlap_s)

        while not job.get("stop"):
            duration = self._probe_duration_seconds(path)
            if duration is None:
                if mode == "instant":
                    self._set_status(f"Split failed: could not read {os.path.basename(path)}")
                    return
                time.sleep(2.0)
                continue

            remaining = duration - start_time
            if remaining <= 0:
                self._set_status(f"Split complete: {os.path.basename(path)}")
                return

            if mode == "catchup" and remaining < part_length_s:
                rec = self._records.get(path, {})
                if rec.get("status") == "WRITING":
                    time.sleep(2.0)   # not enough buffered yet - wait for more
                    continue
                # Source stopped writing: flush the final (short) part below.

            this_len = min(part_length_s, remaining)
            display_part = part_index + offset - 1
            out_file = os.path.join(
                outdir,
                f"{os.path.basename(path).rsplit('.', 1)[0]}_part{display_part}.mp4",
            )
            cmd = [ffmpeg_path, "-y", "-ss", str(start_time), "-i", path,
                   "-c", "copy", "-t", str(this_len), out_file]
            run_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            if IS_WINDOWS:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                run_kwargs["startupinfo"] = si

            proc = subprocess.Popen(cmd, **run_kwargs)
            job["proc"] = proc
            proc.wait()
            job["proc"] = None

            if job.get("stop"):
                try:
                    if os.path.isfile(out_file):
                        os.remove(out_file)
                except OSError:
                    pass
                return

            if proc.returncode != 0 or not os.path.isfile(out_file) or os.path.getsize(out_file) == 0:
                self._set_status(f"Split failed on part {display_part} "
                                  f"(ffmpeg exited {proc.returncode})")
                return

            part_index += 1
            start_time += step

    # ── Drawing ──────────────────────────────────────────────────────────────

    def draw(self, stdscr, y1, x1, y2, x2) -> None:
        db = self.dashboard
        db.draw_box(stdscr, y1, x1, y2, x2, db.C_CHROME)
        db.safe_addstr(stdscr, y1, x1 + 2, " FILE MANAGER \u2014 (Press M for File Options) ",
                       theme.attr(db, "file_manager_filemanagertab_draw_chrome"))

        dirs = self._get_output_dirs()
        if not dirs:
            db.safe_addstr(stdscr, y1 + 2, x1 + 2,
                           "No OUTPUT_DIR configured on any site.",
                           theme.attr(db, "file_manager_filemanagertab_draw_dim_1"))
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
        header_attr = theme.attr(db, "file_manager_filemanagertab_draw_normal")
        db.safe_addstr(stdscr, header_y, x1 + 2, "File".ljust(name_w)[:name_w], header_attr)
        db.safe_addstr(stdscr, header_y, col_status_x, "Status".ljust(status_w)[:status_w], header_attr)
        db.safe_addstr(stdscr, header_y, col_mod_x, "Date Modified".ljust(mod_w)[:mod_w], header_attr)
        db.safe_addstr(stdscr, header_y, col_size_x, "Size".ljust(size_w)[:size_w], header_attr)
        db.safe_addstr(stdscr, header_y, col_rate_x, "Rate".ljust(rate_w)[:rate_w], header_attr)

        list_y1 = header_y + 2           # leave row header_y+1 for status messages
        list_y2 = y2 - 1                 # leave the last row for status/help
        visible = max(1, list_y2 - list_y1)
        self._last_visible = visible

        sel_row = self._cur_selectable_row_idx()
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
                               theme.attr(db, "file_manager_filemanagertab_draw_system_1"))
            elif kind == "empty":
                db.safe_addstr(stdscr, row_y, x1 + 3, payload[:avail_w],
                               theme.attr(db, "file_manager_filemanagertab_draw_dim_2"))
            elif kind == "folder":
                abs_path = payload
                is_sel = (self._selected_kind == "folder" and self._selected_folder == abs_path)
                arrow = "\u25ba" if rec["collapsed"] else "\u25bc"
                label = f"{arrow} {rec['name']}"
                if rec["collapsed"]:
                    label += f"  ({rec['count']})"
                row_attr = (theme.attr(db, "file_manager_filemanagertab_draw_hilight") if is_sel
                            else theme.attr(db, "file_manager_filemanagertab_draw_system_1"))
                # Indent based on depth, but only for subfolders (not OUTPUT_DIR)
                depth = rec.get("depth", 0)
                indent = 2 * depth if not rec.get("is_output_dir", False) else 0
                db.safe_addstr(stdscr, row_y, x1 + 2 + indent,
                               label.ljust(name_w - indent)[:name_w - indent], row_attr)
            else:
                path = payload
                group_path = rec.get("group_path")
                # Compute depth for file indentation
                depth = 0
                if group_path and os.path.dirname(os.path.abspath(path)) != os.path.abspath(group_path):
                    rel_parts = os.path.relpath(path, group_path).replace(os.sep, "/").split("/")
                    depth = len(rel_parts) - 1          # number of subfolder levels
                    name = "/".join(rel_parts)          # full relative path (without OUTPUT_DIR root)
                else:
                    depth = 0
                    name = os.path.basename(path)

                # Indent by two spaces per depth level
                if depth > 0:
                    name = "  " * depth + name
                if path in self._split_jobs:
                    name = "*" + name  # a Split job is running for this file
                status = rec.get("status", "IDLE")
                size_txt = human_size(rec.get("size"))
                rate_txt = human_rate(rec.get("rate")) if status == "WRITING" else "\u2014"
                mtime = rec.get("mtime")
                mod_txt = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                           if mtime else "\u2014")

                is_sel = (path == self._selected_path)
                if is_sel:
                    row_attr = theme.attr(db, "file_manager_filemanagertab_draw_hilight")
                elif status == "WRITING":
                    row_attr = theme.attr(db, "file_manager_filemanagertab_draw_live")
                else:
                    row_attr = theme.attr(db, "file_manager_filemanagertab_draw_dim_3")

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
                                   theme.attr(db, "file_manager_filemanagertab_draw_system_2"))
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
                               theme.attr(db, "file_manager_filemanagertab_draw_live_2"))
            if i == loop_end - 1 and loop_end < len(self._rows):
                db.safe_addstr(stdscr, row_y, x2 - 2, "\u25bc",
                               theme.attr(db, "file_manager_filemanagertab_draw_live_3"))
            row_y += 1

        delete_lbl = "Trash" if self._delete_mode == DELETE_MODE_TRASH else "Permanent Delete"
        info = (f" Delete mode: {delete_lbl} (T to toggle)   "
                f"Sort: {_FM_SORT_LABELS.get(self._sort_key, self._sort_key)} ")
        info_attr = theme.attr(db, "file_manager_filemanagertab_draw_delete") if self._delete_mode == DELETE_MODE_PERMANENT else theme.attr(db, "file_manager_filemanagertab_draw_dim_4")
        db.safe_addstr(stdscr, y2, x1 + 2, info[:avail_w], info_attr)

        if self._status_msg and (time.time() - self._status_msg_ts) < STATUS_MSG_TTL_S:
            db.safe_addstr(stdscr, y1 + 2, x1 + 2, self._status_msg[:avail_w],
                           theme.attr(db, "file_manager_filemanagertab_draw_warn"))