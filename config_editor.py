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
    def __init__(self, line_idx: int, is_section: bool, key: str, value: str, has_equals: bool, raw_line: str):
        self.line_idx = line_idx
        self.is_section = is_section
        self.key = key
        self.value = value
        self.has_equals = has_equals
        self.raw_line = raw_line

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

    def load_config(self, config_path):
        self.current_site_path = config_path
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                self.lines = f.readlines()
        except Exception:
            self.lines = []

        self.items = []
        current_section = None
        for i, line in enumerate(self.lines):
            s = line.strip()
            if not s or s.startswith("#") or s.startswith(";"):
                continue
            if s.startswith("[") and s.endswith("]"):
                current_section = s[1:-1]
                if current_section == "General":
                    self.items.append(ConfigItem(i, True, current_section, "", False, line))
            else:
                if current_section == "General":
                    if "=" in s:
                        k, v = s.split("=", 1)
                        self.items.append(ConfigItem(i, False, k.strip(), v.strip(), True, line))
                    else:
                        self.items.append(ConfigItem(i, False, s, "", False, line))
        
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

        # Draw Site Selector at the top
        tab_x = x1 + 1

        safe_addstr(stdscr, y1, x1, "  Site: ", curses.color_pair(self.dashboard.C_DIM))
        tab_x += 8
        for i, site in enumerate(self.sites):
            lbl = os.path.basename(site.config_path)
            label = f" {lbl} "
            attr = (curses.color_pair(self.dashboard.C_HILIGHT) | curses.A_BOLD
                    if i == self.selected_site_idx
                    else curses.color_pair(self.dashboard.C_CHROME))
            safe_addstr(stdscr, y1, tab_x, label, attr)
            tab_x += len(label) + 1

        # Mode Indicator
        mode_str = " [ CONFIG EDITOR MODE ] "
        safe_addstr(stdscr, y1, x2 - len(mode_str) - 2, mode_str, 
                                    curses.color_pair(self.dashboard.C_LIVE) | curses.A_BOLD)

        # Draw Editor Box
        draw_box(stdscr, y1 + 1, x1, y2, x2, self.dashboard.C_CHROME)
        safe_addstr(stdscr, y1 + 1, x1 + 2, " CONFIGURATION ", 
                                    curses.color_pair(self.dashboard.C_INVHEAD) | curses.A_BOLD)

        if not self.items:
            safe_addstr(stdscr, y1 + 3, x1 + 4, "No configurable items found.", curses.color_pair(self.dashboard.C_DIM))
            return

        # Scrolling logic
        visible_rows = (y2 - y1) - 4
        if self.selected_idx < self.scroll_offset:
            self.scroll_offset = self.selected_idx
        elif self.selected_idx >= self.scroll_offset + visible_rows:
            self.scroll_offset = self.selected_idx - visible_rows + 1

        row_y = y1 + 2
        for i in range(self.scroll_offset, min(len(self.items), self.scroll_offset + visible_rows)):
            item = self.items[i]
            is_selected = (i == self.selected_idx)
            
            if is_selected:
                attr = curses.color_pair(self.dashboard.C_HILIGHT) | curses.A_BOLD
                prefix = "> "
            else:
                prefix = "  "
                attr = curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD if item.is_section else curses.color_pair(self.dashboard.C_NORMAL)
            
            if item.is_section:
                disp_text = f"[{item.key}]"
                if is_selected:
                    attr = curses.color_pair(self.dashboard.C_HILIGHT) | curses.A_BOLD
                else:
                    attr = curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD
                safe_addstr(stdscr, row_y, x1 + 2, prefix + disp_text, attr)
            else:
                key_attr = attr if is_selected else curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD
                val_attr = attr if is_selected else curses.color_pair(self.dashboard.C_LIVE)
                
                safe_addstr(stdscr, row_y, x1 + 2, prefix + f"{item.key:<25}", key_attr)
                if item.has_equals:
                    safe_addstr(stdscr, row_y, x1 + 29, "= " + str(item.value), val_attr)

            row_y += 1

        # Draw Popup
        if self.popup_mode and self.editing_item:
            self.draw_popup(stdscr)

    def draw_popup(self, stdscr):
        h, w = stdscr.getmaxyx()
        box_h, box_w = 8, min(60, w - 4)
        by1 = (h - box_h) // 2
        bx1 = (w - box_w) // 2
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        # Fill background
        for y in range(by1, by2 + 1):
            safe_addstr(stdscr, y, bx1, " " * (box_w + 1), curses.color_pair(self.dashboard.C_NORMAL))

        draw_box(stdscr, by1, bx1, by2, bx2, self.dashboard.C_WARN)
        title = " EDIT CONFIG VALUE "
        safe_addstr(stdscr, by1, bx1 + 2, title, curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)

        safe_addstr(stdscr, by1 + 2, bx1 + 2, f"Key: {self.editing_item.key}", curses.color_pair(self.dashboard.C_CHROME))
        
        safe_addstr(stdscr, by1 + 4, bx1 + 2, "New Value:", curses.color_pair(self.dashboard.C_WARN) | curses.A_BOLD)
        safe_addstr(stdscr, by1 + 4, bx1 + 13, (self.popup_buf + "_")[:box_w - 15], curses.color_pair(self.dashboard.C_NORMAL) | curses.A_BOLD)

        safe_addstr(stdscr, by2, bx1 + 2, " Enter: Save | Esc: Cancel ", curses.color_pair(self.dashboard.C_INVHEAD))

    def handle_key(self, key) -> bool:
        """Returns True if the key was consumed by the editor."""
        if self.popup_mode:
            if key == 27:  # Escape
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
                    
                    # Notify monitor threads if needed (trigger_event)
                    site = self.sites[self.selected_site_idx]
                    site.trigger_event.set()
                self.popup_mode = False
                self.popup_buf = ""
                self.editing_item = None
            elif 32 <= key < 127:
                self.popup_buf += chr(key)
            return True

        # Navigation mode
        if key == 27: # Escape exits config editor entirely
            self.dashboard.selected_tab = 0 # Return to Dashboard
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
