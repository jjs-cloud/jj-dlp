"""Role definitions and custom role creation for a single theme dict."""

from __future__ import annotations

from typing import Any, Dict


def get_role(theme: dict, name: str) -> Dict[str, Any]:
    """Return a role's {"fg", "bg", "bold"} dict. Raises KeyError if unknown."""
    return theme["roles"][name]


def set_role(theme: dict, name: str, fg: str, bg: str, bold: bool) -> None:
    """Update an existing role's colors in place. Raises KeyError if unknown."""
    if name not in theme["roles"]:
        raise KeyError(f"Unknown role: {name!r}")
    theme["roles"][name] = {"fg": fg, "bg": bg, "bold": bold}


def create_role(theme: dict, name: str, fg: str, bg: str, bold: bool) -> None:
    """Add a brand-new role key. Raises ValueError if the name already exists."""
    if name in theme["roles"]:
        raise ValueError(f"Role already exists: {name!r}")
    theme["roles"][name] = {"fg": fg, "bg": bg, "bold": bold}


def delete_role(theme: dict, name: str) -> None:
    """Delete a role. Refuses if any element in this theme still points at it."""
    if name not in theme["roles"]:
        return
    for element_id, ref in theme.get("elements", {}).items():
        if ref.get("role") == name:
            raise ValueError(f"Role {name!r} is still used by element {element_id!r}")
    del theme["roles"][name]
