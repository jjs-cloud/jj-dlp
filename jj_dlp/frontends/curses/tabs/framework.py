"""Tab draw/handle_key/footer_hints interface and TabBar dispatcher."""

from __future__ import annotations

from typing import List, Tuple


class Tab:
    """Interface every tab implements. Subclasses override all three methods."""

    title: str = "Tab"

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw this tab's body inside the rectangle (y1, x1)-(y2, x2) inclusive."""
        raise NotImplementedError

    def handle_key(self, key: int) -> bool:
        """Handle one keypress. Return True if it was consumed by this tab."""
        raise NotImplementedError

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Return (key, label) pairs describing this tab's keybinds, for the footer."""
        raise NotImplementedError


class EmptyTab(Tab):
    """Placeholder tab with a blank body and no keybinds. Real tabs replace this."""

    def __init__(self, title: str) -> None:
        self.title = title

    def draw(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw nothing but the tab's own body area."""
        pass

    def handle_key(self, key: int) -> bool:
        """Never consumes a key."""
        return False

    def footer_hints(self) -> List[Tuple[str, str]]:
        """No tab-specific keybinds."""
        return []


class TabBar:
    """Renders the tab strip and routes drawing/input to the active tab."""

    def __init__(self, tabs: List[Tab]) -> None:
        if not tabs:
            raise ValueError("TabBar requires at least one tab")
        self.tabs = tabs
        self.active_index = 0

    @property
    def active_tab(self) -> Tab:
        """Return the currently active tab."""
        return self.tabs[self.active_index]

    def next_tab(self) -> None:
        """Switch to the next tab, wrapping around."""
        self.active_index = (self.active_index + 1) % len(self.tabs)

    def prev_tab(self) -> None:
        """Switch to the previous tab, wrapping around."""
        self.active_index = (self.active_index - 1) % len(self.tabs)

    def draw_bar(self, stdscr, y: int, x1: int, x2: int, color_fn) -> None:
        """Draw the tab strip on row y between columns x1 and x2 (inclusive)."""
        col = x1
        for index, tab in enumerate(self.tabs):
            label = f" {tab.title} "
            if col + len(label) > x2 + 1:
                break
            role = "tabs.bar.active" if index == self.active_index else "tabs.bar.inactive"
            stdscr.addstr(y, col, label, color_fn(role))
            col += len(label)

    def draw_active(self, stdscr, y1: int, x1: int, y2: int, x2: int) -> None:
        """Draw the active tab's body inside the given rectangle."""
        self.active_tab.draw(stdscr, y1, x1, y2, x2)

    def handle_key(self, key: int) -> bool:
        """Let the active tab handle a key first; caller handles global keys otherwise."""
        return self.active_tab.handle_key(key)

    def footer_hints(self) -> List[Tuple[str, str]]:
        """Return the active tab's footer hints."""
        return self.active_tab.footer_hints()
