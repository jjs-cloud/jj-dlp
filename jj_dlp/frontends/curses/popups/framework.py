"""Generic field-editing popup base (Doc 1 §16.1)."""

from __future__ import annotations

import curses
from typing import Any, Callable, List, Optional, Tuple

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]

# Result strings returned by run_popup().
ACTION_SAVE = "save"
ACTION_RESET = "reset"
ACTION_CANCEL = "cancel"

_UNSET = object()


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


class PopupField:
    """One editable row: a key, its display label, type, current override, and effective value.

    field_type is one of "bool", "tri_bool", "int", "float", "str", "list_str".
    "tri_bool" and "list_str" (with current_override=None) are the only types
    allowed to independently show "inherits: X" — other types are filled from
    effective_value until the user actually edits them.
    options, for "list_str" fields, is a fixed list of choices rendered as a
    checkbox multi-select; leave it None for a free-form comma-separated list.
    """

    def __init__(
        self,
        key: str,
        label: str,
        field_type: str,
        current_override: Any,
        effective_value: Any,
        options: Optional[List[str]] = None,
    ) -> None:
        self.key = key
        self.label = label
        self.field_type = field_type
        self.current_override = current_override
        self.effective_value = effective_value
        self.options = options


def _initial_value(field: PopupField) -> Any:
    """Return the starting working value for a field: its override, or the inherited default."""
    if field.field_type == "tri_bool":
        return field.current_override
    if field.current_override is not None:
        return field.current_override
    return field.effective_value


class FieldEditPopup:
    """A boxed popup editing a list of PopupFields, saving only what actually changed."""

    def __init__(
        self,
        title: str,
        fields: List[PopupField],
        color_fn: Optional[ColorFn] = None,
        confirm_reset_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        if not fields:
            raise ValueError("FieldEditPopup requires at least one field")
        self.title = title
        self.fields = fields
        self.color = color_fn or _default_color_fn
        # Step 11.3's confirm_reset popup is wired in here by the settings/*.py subclasses;
        # default is "no extra confirmation needed" so this module works standalone.
        self.confirm_reset_fn = confirm_reset_fn or (lambda: True)
        self._working = [_initial_value(f) for f in fields]
        self._initial = list(self._working)
        self.index = 0

    def values(self) -> dict:
        """Return the current {key: value} map of every field's working value."""
        return {f.key: v for f, v in zip(self.fields, self._working)}

    def is_modified(self) -> bool:
        """True if any field's working value differs from what it started at."""
        return self._working != self._initial

    # -- editing -----------------------------------------------------------

    def _toggle_bool(self) -> None:
        """Flip the focused bool field."""
        self._working[self.index] = not bool(self._working[self.index])

    def _cycle_tri_bool(self) -> None:
        """Cycle the focused tri-state field: inherit (None) -> True -> False -> inherit."""
        current = self._working[self.index]
        self._working[self.index] = {None: True, True: False, False: None}[current]

    def _toggle_list_option(self, option: str) -> None:
        """Toggle one choice in the focused list_str field's working value."""
        current = list(self._working[self.index] or [])
        if option in current:
            current.remove(option)
        else:
            current.append(option)
        self._working[self.index] = current

    def _edit_text(self, stdscr, win, y: int, x: int, width: int, initial: str) -> Optional[str]:
        """Read a line of text in place, starting from initial. None if canceled."""
        curses.curs_set(1)
        curses.echo()
        text = initial
        try:
            while True:
                win.addstr(y, x, " " * max(0, width), curses.A_NORMAL)
                win.addstr(y, x, text[:width], self.color("popup.button_focused", None))
                win.move(y, x + min(len(text), max(0, width - 1)))
                win.refresh()
                key = win.getch()
                if key in (curses.KEY_ENTER, 10, 13):
                    return text
                if key == 27:
                    return None
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    text = text[:-1]
                elif 0 <= key < 256 and chr(key).isprintable():
                    text += chr(key)
        finally:
            curses.noecho()
            curses.curs_set(0)

    def _edit_current_field(self, stdscr, win, row_y: int, x: int, width: int) -> None:
        """Dispatch editing for the focused field by its type."""
        field = self.fields[self.index]
        if field.field_type == "bool":
            self._toggle_bool()
        elif field.field_type == "tri_bool":
            self._cycle_tri_bool()
        elif field.field_type in ("int", "float", "str"):
            initial = "" if self._working[self.index] is None else str(self._working[self.index])
            result = self._edit_text(stdscr, win, row_y, x, width, initial)
            if result is None:
                return
            self._working[self.index] = _coerce(field.field_type, result, self._working[self.index])
        elif field.field_type == "list_str":
            if field.options:
                self._edit_checkbox_list(win, row_y)
            else:
                initial = ", ".join(self._working[self.index] or [])
                result = self._edit_text(stdscr, win, row_y, x, width, initial)
                if result is None:
                    return
                self._working[self.index] = [v.strip() for v in result.split(",") if v.strip()]

    def _edit_checkbox_list(self, win, anchor_y: int) -> None:
        """Inline checkbox multi-select over a fixed set of options."""
        field = self.fields[self.index]
        options = field.options or []
        selected = list(self._working[self.index] or [])
        cursor = 0
        while True:
            for i, option in enumerate(options):
                mark = "x" if option in selected else " "
                attr = self.color("popup.button_focused", None) if i == cursor else curses.A_NORMAL
                try:
                    win.addstr(anchor_y + 1 + i, 4, f"[{mark}] {option}", attr)
                except curses.error:
                    pass
            win.refresh()
            key = win.getch()
            if key == curses.KEY_UP:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_DOWN:
                cursor = min(len(options) - 1, cursor + 1)
            elif key == ord(" "):
                option = options[cursor]
                if option in selected:
                    selected.remove(option)
                else:
                    selected.append(option)
            elif key in (curses.KEY_ENTER, 10, 13):
                self._working[self.index] = [o for o in options if o in selected]
                return
            elif key == 27:
                return

    # -- drawing -------------------------------------------------------------

    def _display_text(self, field: PopupField, value: Any) -> Tuple[str, bool]:
        """Return (text, is_inherited) for a field's current working value."""
        if field.field_type == "tri_bool" and value is None:
            return (f"inherits: {field.effective_value}", True)
        if field.field_type == "list_str" and field.current_override is None and value == field.effective_value:
            return (f"inherits: {', '.join(value) if value else '(none)'}", True)
        if field.field_type == "list_str":
            return (", ".join(value) if value else "(none)", False)
        return (str(value), False)

    def draw(self, stdscr) -> None:
        """Render the popup box centered on the screen."""
        height, width = stdscr.getmaxyx()
        label_width = max(len(f.label) for f in self.fields)
        box_width = min(width - 2, max(50, label_width + 40))
        box_height = min(height - 2, len(self.fields) + 4)
        y1 = max(0, (height - box_height) // 2)
        x1 = max(0, (width - box_width) // 2)

        win = curses.newwin(box_height, box_width, y1, x1)
        win.keypad(True)
        win.erase()
        win.attrset(self.color("popup.border", None))
        win.border()
        win.attrset(curses.A_NORMAL)
        win.addstr(0, 2, f" {self.title} "[: box_width - 4], self.color("popup.title", None))

        for i, field in enumerate(self.fields):
            row_y = 1 + i
            if row_y >= box_height - 2:
                break
            text, inherited = self._display_text(field, self._working[i])
            row_attr = self.color("popup.button_focused", None) if i == self.index else curses.A_NORMAL
            value_attr = curses.A_DIM if inherited and i != self.index else row_attr
            label_str = f"{field.label}:"[: box_width - 4]
            try:
                win.addstr(row_y, 2, label_str, row_attr)
                win.addstr(row_y, 2 + label_width + 2, text[: max(0, box_width - label_width - 6)], value_attr)
            except curses.error:
                pass

        hint = "↑/↓ move  Enter edit  s save  r reset  Esc cancel"
        try:
            win.addstr(box_height - 2, 2, hint[: box_width - 4], curses.A_DIM)
        except curses.error:
            pass

        self._win = win
        win.noutrefresh()
        curses.doupdate()

    def handle_key(self, stdscr, key: int) -> Optional[str]:
        """Apply one keypress. Returns an ACTION_* string once the popup should close, else None."""
        if key == curses.KEY_UP:
            self.index = max(0, self.index - 1)
        elif key == curses.KEY_DOWN:
            self.index = min(len(self.fields) - 1, self.index + 1)
        elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
            field = self.fields[self.index]
            row_y = 1 + self.index
            label_width = max(len(f.label) for f in self.fields)
            x = 2 + label_width + 2
            width = self._win.getmaxyx()[1] - label_width - 6
            self._edit_current_field(stdscr, self._win, row_y, x, max(1, width))
        elif key in (ord("s"), ord("S")):
            return ACTION_SAVE
        elif key in (ord("r"), ord("R")):
            if self.confirm_reset_fn():
                return ACTION_RESET
        elif key == 27:
            return ACTION_CANCEL
        return None


def _coerce(field_type: str, text: str, fallback: Any) -> Any:
    """Parse text into an int/float, or pass str through as-is; keep fallback on bad input."""
    if field_type == "int":
        try:
            return int(text)
        except ValueError:
            return fallback
    if field_type == "float":
        try:
            return float(text)
        except ValueError:
            return fallback
    return text


def run_popup(stdscr, popup: FieldEditPopup) -> str:
    """Blocking edit loop: draw, read keys, return ACTION_SAVE/ACTION_RESET/ACTION_CANCEL."""
    popup.draw(stdscr)
    while True:
        key = popup._win.getch()
        result = popup.handle_key(stdscr, key)
        if result is not None:
            return result
        popup.draw(stdscr)
