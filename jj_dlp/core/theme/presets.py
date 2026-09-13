"""Shipped default themes: dark, light, high_contrast."""

from __future__ import annotations

import copy
from typing import Dict

from jj_dlp.core.theme import elements

# dark: Doc 1 §3.6's example values.
_DARK_RGB = {
    "chrome": {"fg": "#d0d0d0", "bg": "#000000"},
    "highlight": {"fg": "#5fd7ff", "bg": "#000000"},
    "recording": {"fg": "#ff5f5f", "bg": "#000000"},
    "warning": {"fg": "#ffd75f", "bg": "#000000"},
    "muted": {"fg": "#6c6c6c", "bg": "#000000"},
    "accent": {"fg": "#d787ff", "bg": "#000000"},
}
_DARK_ROLES = {
    "chrome": {"fg": "white", "bg": "black", "bold": False},
    "highlight": {"fg": "cyan", "bg": "black", "bold": True},
    "recording": {"fg": "red", "bg": "black", "bold": True},
    "warning": {"fg": "yellow", "bg": "black", "bold": True},
    "muted": {"fg": "black", "bg": "black", "bold": True},
    "accent": {"fg": "magenta", "bg": "black", "bold": False},
}

# light: re-picked for a light background.
_LIGHT_RGB = {
    "chrome": {"fg": "#2b2b2b", "bg": "#f5f5f5"},
    "highlight": {"fg": "#0064c8", "bg": "#f5f5f5"},
    "recording": {"fg": "#c62828", "bg": "#f5f5f5"},
    "warning": {"fg": "#a06a00", "bg": "#f5f5f5"},
    "muted": {"fg": "#9e9e9e", "bg": "#f5f5f5"},
    "accent": {"fg": "#7b1fa2", "bg": "#f5f5f5"},
}
_LIGHT_ROLES = {
    "chrome": {"fg": "black", "bg": "white", "bold": False},
    "highlight": {"fg": "blue", "bg": "white", "bold": True},
    "recording": {"fg": "red", "bg": "white", "bold": True},
    "warning": {"fg": "yellow", "bg": "white", "bold": True},
    "muted": {"fg": "white", "bg": "white", "bold": False},
    "accent": {"fg": "magenta", "bg": "white", "bold": False},
}

# high_contrast: near-pure black/white plus a few saturated accents.
_HIGH_CONTRAST_RGB = {
    "chrome": {"fg": "#ffffff", "bg": "#000000"},
    "highlight": {"fg": "#00ffff", "bg": "#000000"},
    "recording": {"fg": "#ff0000", "bg": "#000000"},
    "warning": {"fg": "#ffff00", "bg": "#000000"},
    "muted": {"fg": "#ffffff", "bg": "#000000"},
    "accent": {"fg": "#ff00ff", "bg": "#000000"},
}
_HIGH_CONTRAST_ROLES = {
    "chrome": {"fg": "white", "bg": "black", "bold": True},
    "highlight": {"fg": "cyan", "bg": "black", "bold": True},
    "recording": {"fg": "red", "bg": "black", "bold": True},
    "warning": {"fg": "yellow", "bg": "black", "bold": True},
    "muted": {"fg": "white", "bg": "black", "bold": False},
    "accent": {"fg": "magenta", "bg": "black", "bold": True},
}


def _build(theme_id: str, name: str, rgb_mode_colors: dict, roles: dict) -> dict:
    """Assemble one fully self-contained theme dict from its role/color data."""
    return {
        "id": theme_id,
        "name": name,
        "rgb_mode_colors": copy.deepcopy(rgb_mode_colors),
        "roles": copy.deepcopy(roles),
        "elements": elements.get_default_elements_map(),
    }


def get_presets() -> Dict[str, dict]:
    """Return the three shipped presets as fresh {id: theme_dict} copies."""
    return {
        "dark": _build("dark", "Dark", _DARK_RGB, _DARK_ROLES),
        "light": _build("light", "Light", _LIGHT_RGB, _LIGHT_ROLES),
        "high_contrast": _build(
            "high_contrast", "High Contrast", _HIGH_CONTRAST_RGB, _HIGH_CONTRAST_ROLES
        ),
    }
