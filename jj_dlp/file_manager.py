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
import time
import platform
import subprocess

import curses

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

        current_paths = set()
        for _label, folder in dirs:
            try:
                with os.scandir(folder) as it:
                    for entry in it:
                        try:
                            if entry.is_file(follow_symlinks=False):
                                current_paths.add(entry.path)
                        except OSError:
                            continue
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

            folder_abs = os.path.dirname(path)
            rec["group_path"] = folder_abs
            rec["group_label"] = dir_label_map.get(folder_abs, os.path.basename(folder_abs) or folder_abs)
            rec["status"] = "WRITING" if (now - rec["last_change"]) < IDLE_THRESHOLD_S else "IDLE"

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
        self._rows = rows

        # Keep selection valid; default to the first file if none/invalid.
        file_paths = [r[1] for r in rows if r[0] == "file"]
        if self._selected_path not in file_paths:
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
                           curses.color_pair(db.C_NORMAL))

        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        db.safe_addstr(stdscr, by1, bx1 + 2, " SORT FILES ",
                       curses.color_pair(db.C_CHROME) | curses.A_BOLD)
        db.safe_addstr(stdscr, by2, bx1 + 2,
                       " Enter: Select  Esc: Cancel ",
                       curses.color_pair(db.C_INVHEAD))

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
                attr = curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
            elif is_cur:
                attr = curses.color_pair(db.C_LIVE) | curses.A_BOLD
            else:
                attr = curses.color_pair(db.C_NORMAL)
            db.safe_addstr(stdscr, row_y, bx1 + 2,
                           (prefix + label)[:box_w - 4], attr)

    # ── Drawing ──────────────────────────────────────────────────────────────

    def draw(self, stdscr, y1, x1, y2, x2) -> None:
        db = self.dashboard
        db.draw_box(stdscr, y1, x1, y2, x2, db.C_CHROME)
        db.safe_addstr(stdscr, y1, x1 + 2, " FILE MANAGER ",
                       curses.color_pair(db.C_CHROME) | curses.A_BOLD)

        dirs = self._get_output_dirs()
        if not dirs:
            db.safe_addstr(stdscr, y1 + 2, x1 + 2,
                           "No OUTPUT_DIR configured on any site.",
                           curses.color_pair(db.C_DIM))
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
        header_attr = curses.color_pair(db.C_NORMAL) | curses.A_BOLD
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
        for i in range(self._scroll, min(len(self._rows), self._scroll + visible)):
            kind, payload, rec = self._rows[i]
            if kind == "header":
                db.safe_addstr(stdscr, row_y, x1 + 1, ("\u2500 " + payload)[:avail_w],
                               curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)
            elif kind == "empty":
                db.safe_addstr(stdscr, row_y, x1 + 3, payload[:avail_w],
                               curses.color_pair(db.C_DIM))
            else:
                path = payload
                name = os.path.basename(path)
                status = rec.get("status", "IDLE")
                size_txt = human_size(rec.get("size"))
                rate_txt = human_rate(rec.get("rate")) if status == "WRITING" else "\u2014"
                mtime = rec.get("mtime")
                mod_txt = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
                           if mtime else "\u2014")

                is_sel = (path == self._selected_path)
                if is_sel:
                    row_attr = curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                elif status == "WRITING":
                    row_attr = curses.color_pair(db.C_LIVE) | curses.A_BOLD
                else:
                    row_attr = curses.color_pair(db.C_DIM)

                db.safe_addstr(stdscr, row_y, x1 + 2, name.ljust(name_w)[:name_w], row_attr)
                db.safe_addstr(stdscr, row_y, col_status_x, status.ljust(status_w)[:status_w], row_attr)
                db.safe_addstr(stdscr, row_y, col_mod_x, mod_txt.ljust(mod_w)[:mod_w], row_attr)
                db.safe_addstr(stdscr, row_y, col_size_x, size_txt.rjust(size_w)[:size_w], row_attr)
                db.safe_addstr(stdscr, row_y, col_rate_x, rate_txt.rjust(rate_w)[:rate_w], row_attr)
            row_y += 1

        delete_lbl = "Trash" if self._delete_mode == DELETE_MODE_TRASH else "Permanent Delete"
        info = (f" Delete mode: {delete_lbl} (T to toggle)   "
                f"Sort: {_FM_SORT_LABELS.get(self._sort_key, self._sort_key)} ")
        db.safe_addstr(stdscr, y2, x1 + 2, info[:avail_w], curses.color_pair(db.C_DIM))

        if self._status_msg and (time.time() - self._status_msg_ts) < STATUS_MSG_TTL_S:
            db.safe_addstr(stdscr, y1 + 2, x1 + 2, self._status_msg[:avail_w],
                           curses.color_pair(db.C_WARN) | curses.A_BOLD)
