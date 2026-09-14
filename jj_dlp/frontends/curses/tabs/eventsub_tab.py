"""EventSub tab: webhook status, subscriptions, and notification history."""

from __future__ import annotations

import curses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from jj_dlp.core.config import state as state_config
from jj_dlp.frontends.curses.tabs.framework import Tab

ColorTuple = Tuple[str, str, bool]
ColorFn = Callable[[str, Optional[ColorTuple]], int]


def _default_color_fn(_element_id: str, _runtime_pair: Optional[ColorTuple] = None) -> int:
    """Fallback color resolver used when no themed CursesApp is available yet."""
    return curses.A_NORMAL


@dataclass
class EventsubSiteSource:
    """One loaded site's label and config, for EventSub status rendering."""

    label: str
    config: dict


def _format_notification(entry: Dict[str, Any]) -> str:
    """Render one notification-history record as a single readable line."""
    ts = entry.get("ts") or entry.get("timestamp") or ""
    event = entry.get("event_type") or entry.get("type") or "notification"
    streamer = entry.get("streamer") or entry.get("login") or ""
    return f"[{ts}] {streamer} {event}".strip()


class EventsubTab(Tab):
    """For twitch-plugin sites: webhook/subscription/notification status; else N/A."""

    title = "EventSub"

    def __init__(
        self,
        data_dir: Path,
        sites: Optional[List[EventsubSiteSource]] = None,
        color_fn: Optional[ColorFn] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.sites = list(sites or [])
        self.color = color_fn or _default_color_fn
        self.site_index = 0
        self.frozen = False
        self.scroll_offset = 0
        self._visible_height = 0

    def _current(self) -> Optional[EventsubSiteSource]:
        """Return the selected site source, or None if no sites are loaded."""
        if not self.sites:
            return None
        self.site_index %= len(self.sites)
        return self.sites[self.site_index]

    def _header(self, width: int) -> str:
        """Build the site selector line."""
        if not self.sites:
            return "(no sites loaded)"[:width]
        site = self.sites[self.site_index]
        pos = f"{self.site_index + 1}/{len(self.sites)}"
        return f"< {site.label} ({pos}) >"[:width]

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the site selector, then either an inapplicable notice or full status."""
        width = x2 - x1 + 1
        height = y2 - y1 + 1
        if width <= 0 or height <= 0:
            return

        try:
            stdscr.addstr(y1, x1, self._header(width), self.color("tabs.bar.active", None))
        except curses.error:
            pass

        body_y1 = y1 + 1
        body_height = max(0, height - 1)
        self._visible_height = body_height
        if body_height <= 0:
            return

        site = self._current()
        if site is None:
            return
        if site.config.get("plugin") != "twitch":
            try:
                stdscr.addstr(
                    body_y1, x1, "Not applicable for this site."[:width], self.color("eventsub.status_ok", None)
                )
            except curses.error:
                pass
            return

        self._draw_twitch_status(stdscr, site, body_y1, x1, y1 + height - 1, x2, width)

    def _draw_twitch_status(
        self, stdscr, site: EventsubSiteSource, y1: int, x1: int, y2: int, x2: int, width: int
    ) -> None:
        """Draw webhook server status, per-streamer subscriptions, and notification history."""
        es_state = state_config.get_eventsub_state(self.data_dir, site.label)
        status = es_state.get("server_status", "stopped")
        sub_ids = es_state.get("subscription_ids", {})
        notifications = es_state.get("notifications", [])

        status_role = "eventsub.status_ok" if status == "running" else "eventsub.status_error"
        row = self._safe_addstr(stdscr, y1, x1, width, f"Webhook server: {status}", status_role)

        row = self._safe_addstr(stdscr, row, x1, width, "Subscriptions:", "config.field_label")
        streamers = site.config.get("streamers", [])
        if not streamers:
            row = self._safe_addstr(stdscr, row, x1, width, "  (no streamers configured)", "config.field_value")
        for streamer in streamers:
            if row > y2:
                return
            sub_id = sub_ids.get(streamer)
            state_str = f"subscribed ({sub_id})" if sub_id else "not subscribed"
            role = "eventsub.status_ok" if sub_id else "eventsub.status_error"
            row = self._safe_addstr(stdscr, row, x1, width, f"  {streamer}: {state_str}", role)

        row += 1
        if row > y2:
            return
        row = self._safe_addstr(stdscr, row, x1, width, "Recent notifications:", "config.field_label")

        list_height = max(0, y2 - row + 1)
        self._visible_height = list_height
        if list_height <= 0:
            return

        lines = [_format_notification(n) for n in notifications] or ["(none yet)"]
        max_offset = max(0, len(lines) - list_height)
        if not self.frozen:
            self.scroll_offset = 0
        self.scroll_offset = min(self.scroll_offset, max_offset)
        end = len(lines) - self.scroll_offset
        start = max(0, end - list_height)
        for text in lines[start:end]:
            row = self._safe_addstr(stdscr, row, x1, width, text, "config.field_value")

    def _safe_addstr(self, stdscr, row: int, x1: int, width: int, text: str, role: str) -> int:
        """Draw one clipped, themed line and return the next row index."""
        try:
            stdscr.addstr(row, x1, text[:width], self.color(role, None))
        except curses.error:
            pass
        return row + 1

    def handle_key(self, key: int) -> bool:
        """Switch site, freeze toggle, and scroll the notification list."""
        if key == curses.KEY_LEFT:
            if self.sites:
                self.site_index = (self.site_index - 1) % len(self.sites)
                self.scroll_offset = 0
            return True
        if key == curses.KEY_RIGHT:
            if self.sites:
                self.site_index = (self.site_index + 1) % len(self.sites)
                self.scroll_offset = 0
            return True
        if key in (ord("f"), ord("F")):
            self.frozen = not self.frozen
            if not self.frozen:
                self.scroll_offset = 0
            return True
        if key == curses.KEY_UP:
            self.frozen = True
            self.scroll_offset += 1
            return True
        if key == curses.KEY_DOWN:
            self.scroll_offset = max(0, self.scroll_offset - 1)
            return True
        if key == curses.KEY_PPAGE:
            self.frozen = True
            self.scroll_offset += max(1, self._visible_height)
            return True
        if key == curses.KEY_NPAGE:
            self.scroll_offset = max(0, self.scroll_offset - max(1, self._visible_height))
            return True
        return False

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Keybind hints, showing the scroll hint only for twitch sites while frozen."""
        hints = [("\u2190/\u2192", "site")]
        site = self._current()
        if site is not None and site.config.get("plugin") == "twitch":
            hints.append(("f", "unfreeze" if self.frozen else "freeze"))
            if self.frozen:
                hints.append(("\u2191/\u2193/pgup/pgdn", "scroll"))
        return hints
