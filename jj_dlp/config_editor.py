import os
import shutil
import curses
from datetime import datetime

def draw_box(stdscr, y1, x1, y2, x2, pair):
    h, w = stdscr.getmaxyx()
    def safe_ch(y, x, ch):
        if 0 <= y < h and 0 <= x < w - 1:
            try:
                stdscr.addch(y, x, ch, curses.color_pair(pair))
            except curses.error:
                pass
    for x in range(x1 + 1, x2):
        safe_ch(y1, x, curses.ACS_HLINE)
        safe_ch(y2, x, curses.ACS_HLINE)
    for y in range(y1 + 1, y2):
        safe_ch(y, x1, curses.ACS_VLINE)
        safe_ch(y, x2, curses.ACS_VLINE)
    safe_ch(y1, x1, curses.ACS_ULCORNER)
    safe_ch(y1, x2, curses.ACS_URCORNER)
    safe_ch(y2, x1, curses.ACS_LLCORNER)
    safe_ch(y2, x2, curses.ACS_LRCORNER)

def safe_addstr(stdscr, y, x, text, attr=0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    max_len = w - x - 1
    if max_len <= 0:
        return
    try:
        stdscr.addstr(y, x, str(text)[:max_len], attr)
    except curses.error:
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


# ── Keys that live in global.conf (never shown in per-site editor) ────────────
_GLOBAL_KEYS = {"DISK_DRIVES", "DEBUG_LOGS", "DEBUG_LOG_PATH", "CHECK_FOR_UPDATES", "UPDATE_INTERVAL", "ASK_FOR_BROWSER", "ASK_FOR_CONFIG","UPDATE_BRANCH"}


class GlobalConfigEditor:
    """Loads and edits global.conf — the six app-wide settings."""

    GLOBAL_KEYS_ORDER = [
        "DISK_DRIVES",
        "DEBUG_LOGS",
        "DEBUG_LOG_PATH",
        "CHECK_FOR_UPDATES",
        "UPDATE_INTERVAL",
        "ASK_FOR_BROWSER",
        "ASK_FOR_CONFIG",
        "UPDATE_BRANCH",
    ]
    GLOBAL_KEYS_COMMENTS = {
        "DISK_DRIVES":        "Comma-separated list of drives or paths to show disk info in the system panel. (e.g. C:\\,D:\\  or  /home,/mnt/data).",
        "DEBUG_LOGS":         "Enable debug logging to a file(true/false).",
        "DEBUG_LOG_PATH":     "Path for the debug log file. Can be a relative or absolute path (e.g. logs/debug.log)",
        "CHECK_FOR_UPDATES":  "Whether to periodically check for app updates (true/false).",
        "UPDATE_INTERVAL":    "Number of minutes between app update checks.",
        "ASK_FOR_BROWSER":    "Show the browser chooser on startup (true/false).",
        "ASK_FOR_CONFIG":     "Show the config file chooser on startup (true/false).",
        "UPDATE_BRANCH":      "Which branch of jj-dlp to update to. (main, testing, or experimental).",
    }

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
        """Write a minimal global.conf with all keys in the configs/ folder."""
        lines = [
            "[General]\n",
            "\n",
            "# Comma-separated disk drives/mount points to monitor.\n",
            "DISK_DRIVES =\n",
            "\n",
            "# Enable debug logging (true/false).\n",
            "DEBUG_LOGS = false\n",
            "\n",
            "# Path for the debug log file. Leave blank to use the default location.\n",
            "DEBUG_LOG_PATH =\n",
            "\n",
            "# Check for updates on startup (true/false).\n",
            "CHECK_FOR_UPDATES = true\n",
            "\n",
            "# Update check interval in minutes.\n",
            "UPDATE_INTERVAL = 30\n",
            "\n",
            "# Show the browser-cookie picker on startup (true/false).\n",
            "ASK_FOR_BROWSER = true\n",
            "\n",
            "# Show the config file chooser on startup (true/false).\n",
            "ASK_FOR_CONFIG = true\n",
        ]
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
        defaults = {
            "DISK_DRIVES": "",
            "DEBUG_LOGS": "false",
            "DEBUG_LOG_PATH": "",
            "CHECK_FOR_UPDATES": "true",
            "UPDATE_INTERVAL": "30",
            "ASK_FOR_BROWSER": "true",
            "ASK_FOR_CONFIG": "true",
        }
        val = defaults.get(key, "")
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
        backup_dir = "backups"
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"global.conf.{timestamp}.bak")
        try:
            shutil.copy2(self.conf_path, backup_path)
        except Exception:
            pass
        try:
            with open(self.conf_path, "w", encoding="utf-8") as f:
                f.writelines(self.lines)
        except Exception:
            pass
        # Reload so line indices stay accurate
        self._loaded = False
        self._load()

        # Apply changes to live globals immediately (e.g. DEBUG_LOGS)
        if self._on_save:
            new_cfg = {item.key: item.value for item in self.items}
            self._on_save(new_cfg)

    def handle_key(self, key) -> bool:
        """Handle a keypress in the global editor section. Returns True if consumed."""
        self._ensure_loaded()
        if self.popup_mode:
            if key == 27:
                self.popup_mode = False
                self.popup_buf = ""
                self.editing_item = None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.popup_buf = self.popup_buf[:-1]
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
                if self.editing_item:
                    new_val = self.popup_buf.strip()
                    self.lines[self.editing_item.line_idx] = f"{self.editing_item.key} = {new_val}\n"
                    self.save()
                self.popup_mode = False
                self.popup_buf = ""
                self.editing_item = None
            elif 32 <= key < 127:
                self.popup_buf += chr(key)
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

        draw_box(stdscr, y1, x1, y2, x2, db.C_SYSTEM)
        title = " GLOBAL SETTINGS "
        safe_addstr(stdscr, y1, x1 + 2, title, curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)
        if is_active:
            mode_str = " [ GLOBAL ] "
            safe_addstr(stdscr, y1, x2 - len(mode_str) - 1, mode_str,
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
            safe_addstr(stdscr, row_y, x1 + 1, prefix + f"{item.key:<22}", key_attr)
            safe_addstr(stdscr, row_y, x1 + 26, "= " + str(item.value), val_attr)
            row_y += 1

        if self.popup_mode and self.editing_item:
            self._draw_popup(stdscr)

    def _wrap_text(self, text: str, width: int) -> list:
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

    def _draw_popup(self, stdscr):
        db = self.dashboard
        h, w = stdscr.getmaxyx()
        box_w = min(60, w - 4)
        inner_w = box_w - 4
        comment_lines = self._wrap_text(self.editing_item.comment, inner_w) if self.editing_item.comment else []
        inner_rows = 4 + len(comment_lines) + (1 if comment_lines else 0)
        box_h = max(inner_rows + 1, 7)
        box_h = min(box_h, h - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w
        for y in range(by1, by2 + 1):
            safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(db.C_NORMAL))
        draw_box(stdscr, by1, bx1, by2, bx2, db.C_SYSTEM)
        safe_addstr(stdscr, by1, bx1 + 2, " EDIT GLOBAL VALUE ",
                    curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)
        row = by1 + 2
        safe_addstr(stdscr, row, bx1 + 2, f"Key: {self.editing_item.key}",
                    curses.color_pair(db.C_CHROME))
        row += 1
        if comment_lines:
            for cl in comment_lines:
                safe_addstr(stdscr, row, bx1 + 2, cl, curses.color_pair(db.C_DIM))
                row += 1
            row += 1
        else:
            row += 1
        safe_addstr(stdscr, row, bx1 + 2, "New Value:",
                    curses.color_pair(db.C_SYSTEM) | curses.A_BOLD)
        safe_addstr(stdscr, row, bx1 + 13, (self.popup_buf + "_")[:box_w - 15],
                    curses.color_pair(db.C_NORMAL) | curses.A_BOLD)
        safe_addstr(stdscr, by2, bx1 + 2, " Enter: Save | Esc: Cancel ",
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
        self.lines = []
        self.items = []
        self.current_site_path = None
        self.editing_item = None

        # Which panel has keyboard focus: "global" or "site"
        self._focus = "site"

        # Sub-editor for global.conf
        self.global_editor = GlobalConfigEditor(
            parent_dashboard,
            on_save=getattr(parent_dashboard, "apply_global_cfg", None),
        )

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
            return

        # Create backup
        backup_dir = "backups"
        os.makedirs(backup_dir, exist_ok=True)
        base = os.path.basename(self.current_site_path)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"{base}.{timestamp}.bak")
        try:
            shutil.copy2(self.current_site_path, backup_path)
        except Exception as e:
            self.dashboard.sites[self.selected_site_idx].log_line(f"Failed to backup config: {e}")

        # Write new config
        try:
            with open(self.current_site_path, "w", encoding="utf-8") as f:
                f.writelines(self.lines)
        except Exception as e:
            self.dashboard.sites[self.selected_site_idx].log_line(f"Failed to save config: {e}")

        # Reload
        self.load_config(self.current_site_path)

    def draw_tab(self, stdscr, y1, x1, y2, x2):
        # Sync selected site
        if self.selected_site_idx != self.dashboard.selected_site_idx:
            self.selected_site_idx = self.dashboard.selected_site_idx
            self.selected_idx = 0
            self.scroll_offset = 0
            site = self.sites[self.selected_site_idx]
            self.load_config(site.config_path)
        elif self.current_site_path is None and self.sites:
            site = self.sites[self.selected_site_idx]
            self.load_config(site.config_path)

        # ── Layout: side-by-side columns
        #   Site Config (left, wider) | Global Settings (right, narrower)
        total_w = x2 - x1
        # Global panel gets ~38% of width, site gets the rest
        global_w = max(30, int(total_w * 0.38))
        site_w   = total_w - global_w - 1   # -1 for the column gap

        site_x1   = x1
        site_x2   = x1 + site_w
        global_x1 = site_x2 + 1
        global_x2 = x2

        # ── Tab hint row at top ───────────────────────────────────────────────
        if self._focus == "site":
            focus_hint = "  Tab: switch to Global Settings ►  "
        else:
            focus_hint = "  ◄ Tab: switch to Site Config  "
        safe_addstr(stdscr, y1, x1 + 1, focus_hint,
                    curses.color_pair(self.dashboard.C_DIM))

        content_y1 = y1 + 1

        # ── Draw global settings panel (right, narrower) ──────────────────────
        self.global_editor.draw(stdscr, content_y1, global_x1, y2, global_x2,
                                is_active=(self._focus == "global"))

        # ── Draw Site Selector above the site box ─────────────────────────────
        tab_x = site_x1 + 1
        safe_addstr(stdscr, content_y1, site_x1, "  Site: ",
                    curses.color_pair(self.dashboard.C_DIM))
        tab_x += 8
        for i, site in enumerate(self.sites):
            lbl = os.path.basename(site.config_path)
            label = f" {lbl} "
            attr = (curses.color_pair(self.dashboard.C_HILIGHT) | curses.A_BOLD
                    if i == self.selected_site_idx
                    else curses.color_pair(self.dashboard.C_CHROME))
            safe_addstr(stdscr, content_y1, tab_x, label, attr)
            tab_x += len(label) + 1

        if self._focus == "site":
            mode_str = " [ SITE CONFIG ] "
            safe_addstr(stdscr, content_y1, site_x2 - len(mode_str) - 1, mode_str,
                        curses.color_pair(self.dashboard.C_LIVE) | curses.A_BOLD)

        # ── Draw per-site editor box (left, wider) ────────────────────────────
        site_box_y1 = content_y1 + 1
        draw_box(stdscr, site_box_y1, site_x1, y2, site_x2, self.dashboard.C_CHROME)
        safe_addstr(stdscr, site_box_y1, site_x1 + 2, " SITE CONFIGURATION ",
                    curses.color_pair(self.dashboard.C_INVHEAD) | curses.A_BOLD)

        if not self.items:
            safe_addstr(stdscr, site_box_y1 + 2, site_x1 + 4,
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
                    safe_addstr(stdscr, row_y, site_x1 + 2, prefix + disp_text, sec_attr)
                else:
                    key_attr = (attr if is_selected
                                else curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)
                    val_attr = (attr if is_selected
                                else curses.color_pair(self.dashboard.C_LIVE))
                    safe_addstr(stdscr, row_y, site_x1 + 2, prefix + f"{item.key:<25}", key_attr)
                    if item.has_equals:
                        safe_addstr(stdscr, row_y, site_x1 + 29, "= " + str(item.value), val_attr)
                row_y += 1

        # Draw popup (whichever sub-editor owns it)
        if self._focus == "global" and self.global_editor.popup_mode and self.global_editor.editing_item:
            self.global_editor._draw_popup(stdscr)
        elif self._focus == "site" and self.popup_mode and self.editing_item:
            self.draw_popup(stdscr)

    def _wrap_text(self, text: str, width: int) -> list:
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

    def draw_popup(self, stdscr):
        h, w = stdscr.getmaxyx()
        box_w = min(60, w - 4)
        inner_w = box_w - 4

        comment_lines = []
        if self.editing_item and self.editing_item.comment:
            comment_lines = self._wrap_text(self.editing_item.comment, inner_w)

        inner_rows = 4 + len(comment_lines) + (1 if comment_lines else 0)
        box_h = inner_rows + 1
        box_h = max(box_h, 7)
        box_h = min(box_h, h - 4)

        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        for y in range(by1, by2 + 1):
            safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(self.dashboard.C_NORMAL))

        draw_box(stdscr, by1, bx1, by2, bx2, self.dashboard.C_WARN)
        title = " EDIT CONFIG VALUE "
        safe_addstr(stdscr, by1, bx1 + 2, title, curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)

        row = by1 + 2
        safe_addstr(stdscr, row, bx1 + 2, f"Key: {self.editing_item.key}", curses.color_pair(self.dashboard.C_CHROME))
        row += 1

        if comment_lines:
            for cl in comment_lines:
                safe_addstr(stdscr, row, bx1 + 2, cl, curses.color_pair(self.dashboard.C_DIM))
                row += 1
            row += 1
        else:
            row += 1

        safe_addstr(stdscr, row, bx1 + 2, "New Value:", curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)
        safe_addstr(stdscr, row, bx1 + 13, (self.popup_buf + "_")[:box_w - 15], curses.color_pair(self.dashboard.C_NORMAL) | curses.A_BOLD)

        safe_addstr(stdscr, by2, bx1 + 2, " Enter: Save | Esc: Cancel ", curses.color_pair(self.dashboard.C_INVHEAD))

    def handle_key(self, key) -> bool:
        """Returns True if the key was consumed by the editor."""

        # Tab key switches focus between global and site panels
        if key == ord('\t') and not self.global_editor.popup_mode and not self.popup_mode:
            self._focus = "site" if self._focus == "global" else "global"
            return True

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
                self.editing_item = None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.popup_buf = self.popup_buf[:-1]
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER):
                if self.editing_item:
                    new_val = self.popup_buf.strip()
                    if self.editing_item.has_equals:
                        self.lines[self.editing_item.line_idx] = f"{self.editing_item.key} = {new_val}\n"
                    else:
                        self.lines[self.editing_item.line_idx] = f"{new_val}\n"
                    self.save_file()
                    site = self.sites[self.selected_site_idx]
                    site.trigger_event.set()
                self.popup_mode = False
                self.popup_buf = ""
                self.editing_item = None
            elif 32 <= key < 127:
                self.popup_buf += chr(key)
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
