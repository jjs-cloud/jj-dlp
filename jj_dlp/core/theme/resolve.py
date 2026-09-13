"""Color resolution order: element override, role, runtime fallback."""

from __future__ import annotations

from typing import Optional, Tuple

from jj_dlp.core.theme import elements, roles

ColorTuple = Tuple[str, str, bool]

_SAFE_DEFAULT: ColorTuple = ("white", "black", False)


def resolve_color(
    theme: dict, element_id: str, runtime_pair: Optional[ColorTuple] = None
) -> ColorTuple:
    """Resolve (fg, bg, bold) for an element: direct override, then its role, then runtime_pair."""
    entry = theme.get("elements", {}).get(element_id)

    if entry:
        color = entry.get("color")
        if color:
            return (color["fg"], color["bg"], color.get("bold", False))
        role_name = entry.get("role")
        if role_name:
            pair = _role_pair(theme, role_name)
            if pair is not None:
                return pair

    try:
        default_role = elements.get_default_role(element_id)
    except KeyError:
        default_role = None
    if default_role is not None:
        pair = _role_pair(theme, default_role)
        if pair is not None:
            return pair

    if runtime_pair is not None:
        return runtime_pair
    return _SAFE_DEFAULT


def _role_pair(theme: dict, role_name: str) -> Optional[ColorTuple]:
    """Return a role's (fg, bg, bold), or None if the role doesn't exist."""
    try:
        role = roles.get_role(theme, role_name)
    except KeyError:
        return None
    return (role["fg"], role["bg"], role["bold"])
