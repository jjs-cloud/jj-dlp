"""Schema-driven app/site config editing tab."""

from __future__ import annotations

import copy
import curses
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from jj_dlp.core.config import app as app_config
from jj_dlp.core.config import schema
from jj_dlp.frontends.curses.tabs.framework import Tab

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]
FieldDef = Dict[str, Any]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


def get_field_value(data: dict, path: str) -> Any:
    """Return the value at a dotted path within a nested dict, or None if missing."""
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_field_value(data: dict, path: str, value: Any) -> None:
    """Set the value at a dotted path within a nested dict, creating intermediate dicts as needed."""
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _format_value(field: FieldDef, value: Any) -> str:
    """Render a field's current value as a short display string."""
    ftype = field["type"]
    if ftype == "list[str]":
        return ", ".join(str(v) for v in value) if value else "(empty)"
    if ftype == "bool":
        return "on" if value else "off"
    if value is None:
        return ""
    return str(value)


def _coerce(field: FieldDef, text: str) -> Any:
    """Convert entered text into the field's declared type, raising ValueError if invalid."""
    ftype = field["type"]
    if ftype == "int":
        return int(text.strip())
    if ftype == "float":
        return float(text.strip())
    return text


def field_editor_hints() -> List[Tuple[str, str]]:
    """Footer hints shared by any screen embedding a FieldListEditor."""
    return [("↑/↓", "move"), ("enter/space", "edit"), ("←/→", "cycle/toggle")]


def _edit_text(stdscr, initial: str, prompt: str, color_fn: ColorFn) -> Optional[str]:
    """Blocking single-line text-entry popup. Returns the new text, or None if cancelled."""
    curses.curs_set(1)
    try:
        buf = list(initial)
        pos = len(buf)
        while True:
            height, width = stdscr.getmaxyx()
            win_width = max(20, min(width - 4, 60))
            win = curses.newwin(3, win_width, max(0, height // 2 - 1), max(0, (width - win_width) // 2))
            win.erase()
            win.attrset(color_fn("popup.border", None))
            win.border()
            win.attrset(curses.A_NORMAL)
            text = "".join(buf)
            shown = (prompt + text)[: win_width - 4]
            win.addstr(1, 2, shown, color_fn("config.field_value", None))
            win.move(1, min(win_width - 2, 2 + len(prompt) + pos))
            win.noutrefresh()
            curses.doupdate()

            key = stdscr.getch()
            if key in (curses.KEY_ENTER, 10, 13):
                return "".join(buf)
            if key == 27:
                return None
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
            elif key == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif key == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif key == curses.KEY_RESIZE:
                continue
            elif 32 <= key < 256:
                buf.insert(pos, chr(key))
                pos += 1
    finally:
        curses.curs_set(0)


class _ListEditorPopup:
    """Add/remove/edit sub-editor for a single list[str] field."""

    def __init__(self, items: List[str], color_fn: ColorFn) -> None:
        self.items = list(items)
        self.index = 0
        self.color = color_fn

    def draw(self, stdscr) -> None:
        """Render the item list plus a trailing add-row, centered on the screen."""
        height, width = stdscr.getmaxyx()
        title = "enter: edit/add  a: add  d: delete  esc: close"
        rows = self.items + ["[+ add new]"]
        box_width = max(24, min(width - 2, max(len(r) for r in rows + [title]) + 4))
        box_height = max(5, min(height - 2, len(rows) + 3))
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.erase()
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        win.addstr(1, 2, title[: box_width - 4], self.color("popup.title", None))
        for i, row_text in enumerate(rows):
            row = 3 + i
            if row >= box_height - 1:
                break
            attr = self.color("popup.button_focused", None) if i == self.index else self.color("config.field_value", None)
            win.addstr(row, 2, str(row_text)[: box_width - 4], attr)
        win.noutrefresh()
        curses.doupdate()

    def handle_key(self, key: int, stdscr) -> bool:
        """Apply one keypress. Returns True once the popup should close."""
        total = len(self.items) + 1
        if key == curses.KEY_UP:
            self.index = (self.index - 1) % total
        elif key == curses.KEY_DOWN:
            self.index = (self.index + 1) % total
        elif key == ord("a"):
            new_val = _edit_text(stdscr, "", "New value: ", self.color)
            if new_val:
                self.items.append(new_val)
                self.index = len(self.items) - 1
        elif key in (curses.KEY_ENTER, 10, 13):
            if self.index == len(self.items):
                new_val = _edit_text(stdscr, "", "New value: ", self.color)
                if new_val:
                    self.items.append(new_val)
                    self.index = len(self.items) - 1
            else:
                edited = _edit_text(stdscr, self.items[self.index], "Value: ", self.color)
                if edited is not None:
                    self.items[self.index] = edited
        elif key == ord("d") and self.index < len(self.items):
            del self.items[self.index]
            self.index = min(self.index, max(0, len(self.items) - 1))
        elif key in (27, ord("q")):
            return True
        return False


def _run_list_editor(stdscr, initial: List[str], color_fn: ColorFn) -> List[str]:
    """Run the list[str] sub-editor's own input loop and return the resulting list."""
    popup = _ListEditorPopup(initial, color_fn)
    popup.draw(stdscr)
    while True:
        key = stdscr.getch()
        if popup.handle_key(key, stdscr):
            return popup.items
        popup.draw(stdscr)


class FieldListEditor:
    """Renders a list of FieldDef entries against a data dict, editing values in place."""

    def __init__(self, fields: List[FieldDef], data: dict, color_fn: Optional[ColorFn] = None) -> None:
        self.fields = fields
        self.data = data
        self.color = color_fn or _default_color_fn
        self.index = 0
        self.scroll = 0
        self.error: Optional[str] = None
        self._baseline = copy.deepcopy(data)
        self._page_size = 1

    def mark_clean(self) -> None:
        """Reset the modified-field baseline to the data's current values (e.g. after a save)."""
        self._baseline = copy.deepcopy(self.data)

    def is_modified(self, field: FieldDef) -> bool:
        """Return whether a field's value differs from the last save/load baseline."""
        return get_field_value(self.data, field["path"]) != get_field_value(self._baseline, field["path"])

    def current_field(self) -> Optional[FieldDef]:
        """Return the currently-selected field, or None if the list is empty."""
        if not self.fields:
            return None
        return self.fields[self.index]

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the visible slice of fields plus a help/error line, honoring scroll position."""
        width = x2 - x1 + 1
        height = y2 - y1 + 1
        if width <= 0 or height <= 0:
            return

        has_help_line = height > 1
        list_height = height - 1 if has_help_line else height
        self._page_size = max(1, list_height)

        if not self.fields:
            stdscr.addstr(y1, x1, "(no fields)"[:width], self.color("config.field_label", None))
            return

        if self.index < self.scroll:
            self.scroll = self.index
        elif self.index >= self.scroll + list_height:
            self.scroll = self.index - list_height + 1

        label_width = min(max(len(f["path"]) for f in self.fields) + 2, max(8, width // 2))

        row = y1
        for i in range(self.scroll, min(len(self.fields), self.scroll + list_height)):
            field = self.fields[i]
            value = get_field_value(self.data, field["path"])
            label_text = field["path"][: label_width - 1].ljust(label_width)
            value_text = _format_value(field, value)[: max(0, width - label_width)]

            if i == self.index:
                attr = self.color("popup.button_focused", None)
                try:
                    stdscr.addstr(row, x1, (label_text + value_text)[:width], attr)
                except curses.error:
                    pass
            else:
                label_attr = self.color("config.field_label", None)
                value_role = "config.field_modified" if self.is_modified(field) else "config.field_value"
                value_attr = self.color(value_role, None)
                try:
                    stdscr.addstr(row, x1, label_text, label_attr)
                    stdscr.addstr(row, x1 + label_width, value_text, value_attr)
                except curses.error:
                    pass
            row += 1

        if has_help_line:
            field = self.current_field()
            text = self.error or (field.get("help", "") if field else "")
            try:
                stdscr.addstr(y2, x1, text[:width], self.color("footer.hint_text", None))
            except curses.error:
                pass

    def _cycle_enum(self, field: FieldDef, direction: int) -> None:
        """Move an enum field's value forward/backward through its choices, wrapping around."""
        choices = field.get("choices", [])
        if not choices:
            return
        current = get_field_value(self.data, field["path"])
        try:
            idx = choices.index(current)
        except ValueError:
            idx = 0
        set_field_value(self.data, field["path"], choices[(idx + direction) % len(choices)])

    def _activate(self, stdscr) -> None:
        """Open the type-appropriate editor for the currently-selected field."""
        field = self.current_field()
        if field is None:
            return
        self.error = None
        path = field["path"]
        ftype = field["type"]
        current = get_field_value(self.data, path)

        if ftype == "bool":
            set_field_value(self.data, path, not bool(current))
        elif ftype == "enum":
            self._cycle_enum(field, 1)
        elif ftype in ("int", "float", "str"):
            if stdscr is None:
                return
            text = _edit_text(stdscr, "" if current is None else str(current), f"{path}: ", self.color)
            if text is None:
                return
            try:
                set_field_value(self.data, path, _coerce(field, text))
            except ValueError:
                self.error = f"Invalid {ftype} value: {text!r}"
        elif ftype == "list[str]":
            if stdscr is None:
                return
            new_list = _run_list_editor(stdscr, list(current) if current else [], self.color)
            set_field_value(self.data, path, new_list)

    def handle_key(self, key: int, stdscr=None) -> bool:
        """Apply one keypress: navigation, or activating the current field's editor."""
        if not self.fields:
            return False
        if key == curses.KEY_UP:
            self.index = (self.index - 1) % len(self.fields)
            return True
        if key == curses.KEY_DOWN:
            self.index = (self.index + 1) % len(self.fields)
            return True
        if key == curses.KEY_PPAGE:
            self.index = max(0, self.index - self._page_size)
            return True
        if key == curses.KEY_NPAGE:
            self.index = min(len(self.fields) - 1, self.index + self._page_size)
            return True
        if key in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self._activate(stdscr)
            return True
        field = self.fields[self.index]
        if key == curses.KEY_LEFT:
            if field["type"] == "enum":
                self._cycle_enum(field, -1)
            elif field["type"] == "bool":
                set_field_value(self.data, field["path"], not bool(get_field_value(self.data, field["path"])))
            return True
        if key == curses.KEY_RIGHT:
            if field["type"] == "enum":
                self._cycle_enum(field, 1)
            elif field["type"] == "bool":
                set_field_value(self.data, field["path"], not bool(get_field_value(self.data, field["path"])))
            return True
        return False


def _schema_path(data_dir: Path) -> Path:
    """Return the schema/fields.json path under a data directory."""
    return Path(data_dir) / "schema" / "fields.json"


class GlobalSettingsScreen:
    """Schema-driven editor for config/app.json, via core/config/app.py."""

    def __init__(self, data_dir: Path, color_fn: Optional[ColorFn] = None) -> None:
        self.data_dir = Path(data_dir)
        self.color = color_fn or _default_color_fn
        self.editor: FieldListEditor
        self.reload()

    def reload(self) -> None:
        """Reload config/app.json from disk, discarding any unsaved edits."""
        self._config = app_config.load(self.data_dir)
        fields = schema.get_app_fields(_schema_path(self.data_dir))
        self.editor = FieldListEditor(fields, self._config.to_dict(), self.color)
        self.editor.error = "Reloaded from disk."

    def save(self) -> None:
        """Rebuild an AppConfig from the editor's data and save it to config/app.json."""
        try:
            self._config = app_config.AppConfig.from_dict(self.editor.data)
        except Exception as exc:  # malformed edit slipped past field-level coercion
            self.editor.error = f"Save failed: {exc}"
            return
        app_config.save(self.data_dir, self._config)
        self.editor.mark_clean()
        self.editor.error = "Saved."

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the app_fields list."""
        self.editor.draw(stdscr, y1, x1, y2, x2)

    def handle_key(self, key: int, stdscr=None) -> bool:
        """Save on 's', reload on 'r', else delegate to the field editor."""
        if key == ord("s"):
            self.save()
            return True
        if key == ord("r"):
            self.reload()
            return True
        return self.editor.handle_key(key, stdscr)

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Field navigation hints plus save/reload."""
        return field_editor_hints() + [("s", "save"), ("r", "reload")]


class ConfigTab(Tab):
    """Config tab: global app settings for now, per-site screens join in Step 10.3."""

    title = "Config"

    def __init__(self, data_dir: Path, color_fn: Optional[ColorFn] = None) -> None:
        self.data_dir = Path(data_dir)
        self.color = color_fn or _default_color_fn
        self.global_screen = GlobalSettingsScreen(self.data_dir, self.color)
        self._stdscr = None

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the active screen (only the global settings screen exists so far)."""
        self._stdscr = stdscr
        self.global_screen.draw(stdscr, y1, x1, y2, x2)

    def handle_key(self, key: int) -> bool:
        """Delegate to the active screen, passing the stdscr captured at draw time."""
        return self.global_screen.handle_key(key, self._stdscr)

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Footer hints from the active screen."""
        return self.global_screen.footer_hints()
