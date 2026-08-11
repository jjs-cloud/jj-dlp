"""
theme.py — Per-call-site color/bold customization for the JJDlpDashboard TUI.

WHAT THIS DOES
───────────────
Every `curses.color_pair(...)` [`| curses.A_BOLD`] expression in main.py,
config_editor.py, and file_manager.py has been rewritten to go through
`theme.attr(owner, tag)` — or `theme.attr(owner, tag, runtime_pair)` for the
small number of sites whose pair legitimately varies at runtime (a stat value
colored by its current state, a border highlighted when focused, etc.). Every
SITE_REGISTRY entry has a fixed `default_role`, so every site behaves the same
way: an override role always wins, otherwise a passed runtime_pair wins (for
the dynamic sites), otherwise the default_role applies. The splash/picker
screens pass owner=None (they run before any dashboard exists); their roles
resolve through the module-level ROLE_PAIR_NUM table instead of a dashboard's
C_* constants, so they are editable exactly like every other site. SITE_REGISTRY
in this file is the single source of truth for each site's default role and
bold — call sites no longer carry their own default pair/bold literals, so
there's only ever one place to edit a site's default. `attr()` looks up `tag`
in the user's saved overrides (theme.json) and, if found, uses the overridden
pair/bold instead of the registry default. If nothing is overridden for that
tag, behavior is 100% identical to the original inline `curses.color_pair(...)`
expression — this module is pure opt-in indirection.

Two independent customization layers, both persisted to theme.json AND
bakeable (via the 'W' hotkey) into this file — theme.py owns both the base
scheme palettes (COLOR_SCHEMES) and the per-site registry (SITE_REGISTRY),
so baking either layer only ever touches this one file:

  1. BASE SCHEME + ROLE COLORS — pick one of COLOR_SCHEMES (below) as a
     starting point, then optionally recolor any of the 13 named roles
     (C_CHROME, C_HILIGHT, ... C_DELETE) by fg/bg. This changes what each
     role *means* color-wise, same as editing the COLOR_SCHEMES tuple
     directly — every call site that uses that role updates together. Role
     color changes are stored PER base scheme (keyed by scheme index in
     theme.json), so each scheme keeps its own palette; the editor always
     targets whichever scheme is currently active.

  2. PER-SITE OVERRIDES — repoint an individual call site to a different
     role entirely (e.g. make one C_LOGO site render as C_REC instead) and/or
     toggle its bold flag independently, without affecting any other site
     that shares the same role.

Site overrides are resolved AFTER role colors, so a site pointed at C_REC
picks up whatever C_REC currently resolves to (base scheme or custom role
color), not a frozen snapshot.

One deliberate non-theme exception: the STREAMERS panel's selected row gets a
focus cue (`curses.A_REVERSE`, only while that sub-panel has keyboard focus) at
its call site in main.py. That's a focus/cursor indicator like the '>' row
markers, not a color or bold decision, and a static per-site flag can't express
"reverse only while focused", so it stays at the call site; the site's
role/bold are still fully theme-editable.

SITE_REGISTRY is maintained by hand. If call sites are added, removed, or
moved in the source files, update the corresponding entries here to match.
"""


import atexit
import curses
import json
import os
import re
import sys
import time

# ─────────────────────────────────────────────────────────────────────────
# The 13 named roles, in display order, matching JJDlpDashboard.C_* constants.
# ─────────────────────────────────────────────────────────────────────────
ROLE_ORDER = [
    'CHROME', 'HILIGHT', 'WARN', 'LIVE', 'INVHEAD', 'LOGO', 'REC', 'DIM',
    'LIVEBADGE', 'NORMAL', 'DISABLED', 'SYSTEM', 'DELETE',
]

ROLE_LABELS = {
    'CHROME':    'Borders & Labels',
    'HILIGHT':   'Selected Tab',
    'WARN':      'Warnings / Countdown',
    'LIVE':      'Live Status',
    'INVHEAD':   'Inverse Headers',
    'LOGO':      'Logo',
    'REC':       'Recording Indicator',
    'DIM':       'Dim / Offline Text',
    'LIVEBADGE': 'Live Badge',
    'NORMAL':    'Normal Text',
    'DISABLED':  'Disabled / Blocked',
    'SYSTEM':    'System Panel',
    'DELETE':    'Delete Warning',
}

# Roles that have their own explicit background slot in the COLOR_SCHEMES
# tuple (vs. sharing the scheme's ambient background). Matches _apply_color_scheme.
ROLE_HAS_OWN_BG = {'HILIGHT', 'INVHEAD', 'LIVEBADGE', 'DELETE'}

# fg/bg field indices into a COLOR_SCHEMES tuple, in COLOR_SCHEMES' own order.
# (chrome_fg, hilight_fg, hilight_bg, warn_fg, live_fg, invhead_fg, invhead_bg,
#  logo_fg, rec_fg, dim_fg, livebadge_fg, livebadge_bg, normal_fg, disabled_fg,
#  system_fg, delete_fg, delete_bg)
SCHEME_TUPLE_FIELDS = [
    ('CHROME',    'fg'), ('HILIGHT', 'fg'), ('HILIGHT', 'bg'),
    ('WARN',      'fg'), ('LIVE',    'fg'), ('INVHEAD', 'fg'), ('INVHEAD', 'bg'),
    ('LOGO',      'fg'), ('REC',     'fg'), ('DIM',     'fg'),
    ('LIVEBADGE', 'fg'), ('LIVEBADGE', 'bg'), ('NORMAL', 'fg'),
    ('DISABLED',  'fg'), ('SYSTEM',  'fg'),
    ('DELETE',    'fg'), ('DELETE',  'bg'),
]

COLOR_NAMES = ['BLACK', 'RED', 'GREEN', 'YELLOW', 'BLUE', 'MAGENTA', 'CYAN', 'WHITE']

# Pair number for each role — matches JJDlpDashboard.C_* constants exactly.
ROLE_PAIR_NUM = {
    'CHROME': 1, 'HILIGHT': 2, 'WARN': 3, 'LIVE': 4, 'INVHEAD': 5, 'LOGO': 6,
    'REC': 7, 'DIM': 8, 'LIVEBADGE': 9, 'NORMAL': 10, 'DISABLED': 11,
    'SYSTEM': 12, 'DELETE': 13,
}
PAIR_NUM_ROLE = {v: k for k, v in ROLE_PAIR_NUM.items()}


# ─────────────────────────────────────────────────────────────────────────
# COLOR_SCHEMES — the base palettes. This is the single source of truth for
# scheme colors; theme.py owns it so base-scheme role-color overrides can be
# baked here directly (see bake_to_source) without touching main.py.
#
# Each tuple is (chrome_fg, hilight_fg, hilight_bg, 
#                warn_fg, live_fg, invhead_fg, 
#                invhead_bg, logo_fg, rec_fg,
#                dim_fg, livebadge_fg, livebadge_bg,
#                normal_fg, disabled_fg, system_fg,
#                delete_fg, delete_bg) —
# see SCHEME_TUPLE_FIELDS above for the field order.
# ─────────────────────────────────────────────────────────────────────────
SCHEME_NAMES = [
    "Default (cyan/blue/green/magenta)", "Amber terminal", "Green phosphor",
    "Red alert", "Magenta/purple", "Ice blue", "DOS Blue", "DOS Red", "DOS White",
]

COLOR_SCHEMES = [
    # 0: Default (cyan/blue/green/magenta)
    (curses.COLOR_CYAN,    curses.COLOR_WHITE,   curses.COLOR_BLUE,
     curses.COLOR_YELLOW,  curses.COLOR_GREEN,   curses.COLOR_BLACK,
     curses.COLOR_CYAN,    curses.COLOR_MAGENTA, curses.COLOR_RED,
     curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_GREEN,
     curses.COLOR_WHITE,   curses.COLOR_YELLOW,  curses.COLOR_YELLOW,
     curses.COLOR_WHITE,   curses.COLOR_RED),
    # 1: Amber terminal
    (curses.COLOR_YELLOW,  curses.COLOR_WHITE,   curses.COLOR_YELLOW,
     curses.COLOR_WHITE,   curses.COLOR_GREEN,   curses.COLOR_BLACK,
     curses.COLOR_YELLOW,  curses.COLOR_YELLOW,  curses.COLOR_RED,
     curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_GREEN,
     curses.COLOR_WHITE,   curses.COLOR_WHITE,   curses.COLOR_CYAN,
     curses.COLOR_WHITE,   curses.COLOR_RED),
    # 2: Green phosphor
    (curses.COLOR_GREEN,   curses.COLOR_WHITE,   curses.COLOR_GREEN,
     curses.COLOR_CYAN,    curses.COLOR_WHITE,   curses.COLOR_BLACK,
     curses.COLOR_GREEN,   curses.COLOR_GREEN,   curses.COLOR_RED,
     curses.COLOR_GREEN,   curses.COLOR_BLACK,   curses.COLOR_WHITE,
     curses.COLOR_WHITE,   curses.COLOR_CYAN,    curses.COLOR_YELLOW,
     curses.COLOR_WHITE,   curses.COLOR_RED),
    # 3: Red alert
    (curses.COLOR_RED,     curses.COLOR_WHITE,   curses.COLOR_RED,
     curses.COLOR_YELLOW,  curses.COLOR_GREEN,   curses.COLOR_BLACK,
     curses.COLOR_RED,     curses.COLOR_RED,     curses.COLOR_MAGENTA,
     curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_GREEN,
     curses.COLOR_WHITE,   curses.COLOR_YELLOW,  curses.COLOR_CYAN,
     curses.COLOR_WHITE,   curses.COLOR_MAGENTA),
    # 4: Magenta/purple
    (curses.COLOR_MAGENTA, curses.COLOR_WHITE,   curses.COLOR_MAGENTA,
     curses.COLOR_CYAN,    curses.COLOR_GREEN,   curses.COLOR_BLACK,
     curses.COLOR_MAGENTA, curses.COLOR_CYAN,    curses.COLOR_RED,
     curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_GREEN,
     curses.COLOR_WHITE,   curses.COLOR_CYAN,    curses.COLOR_YELLOW,
     curses.COLOR_WHITE,   curses.COLOR_RED),
    # 5: Ice blue
    (curses.COLOR_CYAN,    curses.COLOR_WHITE,   curses.COLOR_CYAN,
     curses.COLOR_WHITE,   curses.COLOR_GREEN,   curses.COLOR_BLACK,
     curses.COLOR_WHITE,   curses.COLOR_BLUE,    curses.COLOR_RED,
     curses.COLOR_CYAN,    curses.COLOR_BLACK,   curses.COLOR_GREEN,
     curses.COLOR_WHITE,   curses.COLOR_YELLOW,  curses.COLOR_MAGENTA,
     curses.COLOR_WHITE,   curses.COLOR_RED),
    # 6: DOS Blue (classic QBasic/EDIT-style white-on-blue screen)
    (curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_WHITE,
     curses.COLOR_YELLOW,  curses.COLOR_GREEN,   curses.COLOR_BLUE,
     curses.COLOR_WHITE,   curses.COLOR_YELLOW,  curses.COLOR_RED,
     curses.COLOR_CYAN,    curses.COLOR_BLACK,   curses.COLOR_GREEN,
     curses.COLOR_WHITE,   curses.COLOR_CYAN,    curses.COLOR_YELLOW,
     curses.COLOR_WHITE,   curses.COLOR_RED),
    # 7: DOS Red (red alert / danger screen)
    (curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_WHITE,
     curses.COLOR_YELLOW,  curses.COLOR_GREEN,   curses.COLOR_RED,
     curses.COLOR_WHITE,   curses.COLOR_YELLOW,  curses.COLOR_WHITE,
     curses.COLOR_WHITE,   curses.COLOR_BLACK,   curses.COLOR_GREEN,
     curses.COLOR_YELLOW,  curses.COLOR_YELLOW,  curses.COLOR_WHITE,
     curses.COLOR_WHITE,   curses.COLOR_BLUE),
    # 8: DOS White (classic light-background word-processor screen)
    (curses.COLOR_BLUE,    curses.COLOR_WHITE,   curses.COLOR_BLUE,
     curses.COLOR_RED,     curses.COLOR_GREEN,   curses.COLOR_WHITE,
     curses.COLOR_BLUE,    curses.COLOR_MAGENTA, curses.COLOR_RED,
     curses.COLOR_BLACK,   curses.COLOR_WHITE,   curses.COLOR_GREEN,
     curses.COLOR_BLACK,   curses.COLOR_CYAN,    curses.COLOR_BLUE,
     curses.COLOR_WHITE,   curses.COLOR_RED),
]

# Main dashboard background for each scheme (index-aligned with
# COLOR_SCHEMES). Defaults to COLOR_BLACK when not listed here — the
# DOS Blue/Red/White schemes override this to recolor the whole screen.
_SCHEME_BACKGROUND = {
    6: curses.COLOR_BLUE,
    7: curses.COLOR_RED,
    8: curses.COLOR_WHITE,
}

# Default base scheme index: DOS Blue (6) for all platforms.
# Used as the fallback whenever theme.json doesn't specify a base_scheme_idx.
DEFAULT_SCHEME_IDX = 6


# ─────────────────────────────────────────────────────────────────────────
# SITE_REGISTRY — hand-maintained. tag -> {file, label, default_role,
# default_bold}. This registry is the single source of truth for each call
# site's default role/bold — call sites in the source files just pass their
# tag (and, for runtime-variable sites, their runtime pair) to theme.attr();
# they no longer carry their own default pair/bold literals.
# Every entry has a default_role, so every site is editable (role + bold) the
# same way. Sites whose pair genuinely varies at runtime (stat values colored
# by state, borders highlighted when focused) keep passing a runtime pair,
# which attr() lets win over the default_role; the splash/picker screens pass
# owner=None and no pair, so their default_role resolves through ROLE_PAIR_NUM.
# ─────────────────────────────────────────────────────────────────────────
SITE_REGISTRY = {
    'config_editor_priorityeditor_draw_live_1': {'file': 'config_editor.py', 'label': 'Priority Editor — Title (STREAMER SETTINGS)', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_priorityeditor_draw_live_2': {'file': 'config_editor.py', 'label': 'Priority Editor — Mode Indicator Badge', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_priorityeditor_draw_dim_1': {'file': 'config_editor.py', 'label': 'Priority Editor — Hints List', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_priorityeditor_draw_dim_2': {'file': 'config_editor.py', 'label': 'Priority Editor — \'No streamers.\' Message', 'default_role': 'DIM', 'default_bold': True},
    'config_editor_priorityeditor_draw_hilight_1': {'file': 'config_editor.py', 'label': 'Priority Editor — Bypass Streamer Row (Selected)', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_priorityeditor_draw_live_3': {'file': 'config_editor.py', 'label': 'Priority Editor — Bypass Streamer Row (Unselected)', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_priorityeditor_draw_hilight_2': {'file': 'config_editor.py', 'label': 'Priority Editor — Normal Streamer Row (Selected)', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_priorityeditor_draw_normal': {'file': 'config_editor.py', 'label': 'Priority Editor — Normal Streamer Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_priorityeditor_draw_live_4': {'file': 'config_editor.py', 'label': 'Priority Editor — Scroll-Up Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_priorityeditor_draw_live_5': {'file': 'config_editor.py', 'label': 'Priority Editor — Scroll-Down Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_streamersettingspopu_draw_normal': {'file': 'config_editor.py', 'label': 'Streamer Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_streamersettingspopu_draw_system': {'file': 'config_editor.py', 'label': 'Streamer Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_streamersettingspopu_draw_hilight': {'file': 'config_editor.py', 'label': 'Streamer Settings Popup — Menu Option (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_streamersettingspopu_draw_warn': {'file': 'config_editor.py', 'label': 'Streamer Settings Popup — Menu Option (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_streamersettingspopu_draw_invhead': {'file': 'config_editor.py', 'label': 'Streamer Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_qualitysettingspopup_draw_normal': {'file': 'config_editor.py', 'label': 'Quality Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_qualitysettingspopup_draw_system': {'file': 'config_editor.py', 'label': 'Quality Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_qualitysettingspopup_draw_hilight_1': {'file': 'config_editor.py', 'label': 'Quality Settings Popup — \'Low Quality Enabled\' Label', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_qualitysettingspopup_draw_hilight_2': {'file': 'config_editor.py', 'label': 'Quality Settings Popup — Checkbox Value', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_qualitysettingspopup_draw_invhead': {'file': 'config_editor.py', 'label': 'Quality Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_notificationsettings_draw_normal_1': {'file': 'config_editor.py', 'label': 'Notification Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_notificationsettings_draw_system': {'file': 'config_editor.py', 'label': 'Notification Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_notificationsettings_draw_hilight_1': {'file': 'config_editor.py', 'label': 'Notification Settings Popup — \'ntfy Notifications\' Label', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_notificationsettings_draw_hilight_2': {'file': 'config_editor.py', 'label': 'Notification Settings Popup — State Badge (Inherit/On/Off)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_notificationsettings_draw_normal_2': {'file': 'config_editor.py', 'label': 'Notification Settings Popup — \'Effective\' Label', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_notificationsettings_draw_warn': {'file': 'config_editor.py', 'label': 'Notification Settings Popup — Effective Value (ON/OFF)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_notificationsettings_draw_invhead': {'file': 'config_editor.py', 'label': 'Notification Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_splitsettingspopup_draw_normal_1': {'file': 'config_editor.py', 'label': 'Split Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_splitsettingspopup_draw_system': {'file': 'config_editor.py', 'label': 'Split Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_hilight_1': {'file': 'config_editor.py', 'label': 'Split Settings Popup — Field Label (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_warn_1': {'file': 'config_editor.py', 'label': 'Split Settings Popup — Field Label (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_hilight_2': {'file': 'config_editor.py', 'label': 'Split Settings Popup — Field Value (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_normal_2': {'file': 'config_editor.py', 'label': 'Split Settings Popup — Field Value (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_splitsettingspopup_draw_normal_3': {'file': 'config_editor.py', 'label': 'Split Settings Popup — \'Effective\' Label', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_splitsettingspopup_draw_warn_2': {'file': 'config_editor.py', 'label': 'Split Settings Popup — Effective Value', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_warn_3': {'file': 'config_editor.py', 'label': 'Split Settings Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_invhead': {'file': 'config_editor.py', 'label': 'Split Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_introdelaysettingspo_draw_normal_1': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_introdelaysettingspo_draw_system': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_hilight_1': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — Field Label (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_warn_1': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — Field Label (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_hilight_2': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — Field Value (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_normal_2': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — Field Value (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_introdelaysettingspo_draw_normal_3': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — \'Effective\' Label', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_introdelaysettingspo_draw_warn_2': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — Effective Value', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_warn_3': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_invhead': {'file': 'config_editor.py', 'label': 'Intro Delay Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_schedulesettingspopu_draw_normal_1': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_schedulesettingspopu_draw_system': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_hilight_1': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Field Label (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_warn_1': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Field Label (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_hilight_2': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Field Value (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_normal_2': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Field Value (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_schedulesettingspopu_draw_hilight_3': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Day-of-Week Token (Cursor)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_live': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Day-of-Week Token (Active)', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_dim': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Day-of-Week Token (Inactive)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_schedulesettingspopu_draw_normal_3': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Time Edit Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_warn_2': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_invhead': {'file': 'config_editor.py', 'label': 'Schedule Settings Popup — Legend/Hint Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_sitesortmanager_draw_popup_normal_1': {'file': 'config_editor.py', 'label': 'Dashboard Sort Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_sitesortmanager_draw_popup_chrome': {'file': 'config_editor.py', 'label': 'Dashboard Sort Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'config_editor_sitesortmanager_draw_popup_invhead': {'file': 'config_editor.py', 'label': 'Dashboard Sort Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_sitesortmanager_draw_popup_hilight': {'file': 'config_editor.py', 'label': 'Dashboard Sort Popup — Selected Option', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_sitesortmanager_draw_popup_live': {'file': 'config_editor.py', 'label': 'Dashboard Sort Popup — Currently Active Option', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_sitesortmanager_draw_popup_normal_2': {'file': 'config_editor.py', 'label': 'Dashboard Sort Popup — Unselected Option', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_normal_1': {'file': 'config_editor.py', 'label': 'Destinations Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_system': {'file': 'config_editor.py', 'label': 'Destinations Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_destinations_po_dim_1': {'file': 'config_editor.py', 'label': 'Destinations Popup — Streamer Comment Text', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_dim_2': {'file': 'config_editor.py', 'label': 'Destinations Popup — \'Paths:\' Header', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_dim_3': {'file': 'config_editor.py', 'label': '(none yet)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_normal_2': {'file': 'config_editor.py', 'label': 'Destinations Popup — Path Row', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_warn': {'file': 'config_editor.py', 'label': 'Destinations Popup — \'New path:\' Label', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_destinations_po_normal_3': {'file': 'config_editor.py', 'label': 'Destinations Popup — New-Path Entry Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_destinations_po_invhead': {'file': 'config_editor.py', 'label': 'Destinations Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_normal_1': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_system': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_hilight_1': {'file': 'config_editor.py', 'label': 'Message Filters Popup — \'Tag Enabled\' Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_normal_2': {'file': 'config_editor.py', 'label': 'Message Filters Popup — \'Tag Enabled\' Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_hilight_2': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Tag Badge (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_live_1': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Tag Badge (Enabled)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_warn': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Tag Badge (Disabled)', 'default_role': 'WARN', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_dim_1': {'file': 'config_editor.py', 'label': 'Message Filters Popup — \'Messages:\' Header', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_dim_2': {'file': 'config_editor.py', 'label': '(no dbg() calls found for this tag)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_hilight_3': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Message Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_normal_3': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Message Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_hilight_4': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Message Badge (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_live_2': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Message Badge (On)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_dim_3': {'file': 'config_editor.py', 'label': 'Message Filters Popup — Message Badge (Off)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_invhead': {'file': 'config_editor.py', 'label': 'Space:Toggle  Enter:Save  Esc:Back', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_normal_1': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_system': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_hilight_1': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — \'Enable Logging\' Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_normal_2': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — \'Enable Logging\' Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_hilight_2': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Enable Logging Badge (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_live_1': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Enable Logging Badge (On)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_warn': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Enable Logging Badge (Off)', 'default_role': 'WARN', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_dim_1': {'file': 'config_editor.py', 'label': 'Tag Filters:', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_hilight_3': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Tag Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_normal_3': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Tag Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_hilight_4': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Tag Badge (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_live_2': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Tag Badge (On)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_dim_2': {'file': 'config_editor.py', 'label': 'Debug Tags Popup — Tag Badge (Off)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_invhead': {'file': 'config_editor.py', 'label': 'Space:Messages  Enter:Save  Esc:Cancel', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_live_1': {'file': 'config_editor.py', 'label': 'Global Settings Panel — Title', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_live_2': {'file': 'config_editor.py', 'label': 'Global Settings Panel — Mode Indicator Badge', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_hilight_1': {'file': 'config_editor.py', 'label': 'Global Settings Panel — Item Key (Selected)', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_warn': {'file': 'config_editor.py', 'label': 'Global Settings Panel — Item Key (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_hilight_2': {'file': 'config_editor.py', 'label': 'Global Settings Panel — Item Value (Selected)', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_live_3': {'file': 'config_editor.py', 'label': 'Global Settings Panel — Item Value (Unselected)', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_live_4': {'file': 'config_editor.py', 'label': 'Global Settings Panel — Scroll-Up Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_live_5': {'file': 'config_editor.py', 'label': 'Global Settings Panel — Scroll-Down Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_normal_1': {'file': 'config_editor.py', 'label': 'Edit Global Value Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_popup_system_1': {'file': 'config_editor.py', 'label': 'Edit Global Value Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_chrome': {'file': 'config_editor.py', 'label': 'Edit Global Value Popup — \'Key:\' Line', 'default_role': 'CHROME', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_popup_dim': {'file': 'config_editor.py', 'label': 'Edit Global Value Popup — Comment Text', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_popup_system_2': {'file': 'config_editor.py', 'label': 'Edit Global Value Popup — \'New Value:\' Label', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_normal_2': {'file': 'config_editor.py', 'label': 'Edit Global Value Popup — New-Value Entry Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_warn': {'file': 'config_editor.py', 'label': 'Edit Global Value Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_invhead': {'file': 'config_editor.py', 'label': 'Enter: Save | Esc: Cancel #1', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_configeditor_draw_tab_dim_1': {'file': 'config_editor.py', 'label': 'Site:', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_configeditor_draw_tab_hilight_1': {'file': 'config_editor.py', 'label': 'Site Tab Bar — Selected Site Tab', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_configeditor_draw_tab_chrome': {'file': 'config_editor.py', 'label': 'Site Tab Bar — Unselected Site Tab', 'default_role': 'CHROME', 'default_bold': False},
    'config_editor_configeditor_draw_tab_dim_2': {'file': 'config_editor.py', 'label': '[: prev site  ]: next site  Tab: Next Panel', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_configeditor_draw_tab_live_1': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Mode Indicator Badge', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_configeditor_draw_tab_live_2': {'file': 'config_editor.py', 'label': 'No configurable items found. #1', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_configeditor_draw_tab_dim_3': {'file': 'config_editor.py', 'label': 'No configurable items found. #2', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_configeditor_draw_tab_hilight_2': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Item Row (Selected)', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_configeditor_draw_tab_warn_1': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Section Header Row (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_tab_normal': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Regular Item Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_configeditor_draw_tab_hilight_3': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Section Header (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_configeditor_draw_tab_warn_2': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Section Header (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_tab_warn_3': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Item Key (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_tab_live_3': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Item Value (Unselected)', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_configeditor_draw_tab_live_4': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Scroll-Up Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_configeditor_draw_tab_live_5': {'file': 'config_editor.py', 'label': 'Site Settings Panel — Scroll-Down Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_configeditor_draw_popup_normal_1': {'file': 'config_editor.py', 'label': 'Edit Config Value Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_configeditor_draw_popup_warn_1': {'file': 'config_editor.py', 'label': 'Edit Config Value Popup — Title', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_popup_chrome': {'file': 'config_editor.py', 'label': 'Edit Config Value Popup — \'Key:\' Line', 'default_role': 'CHROME', 'default_bold': False},
    'config_editor_configeditor_draw_popup_dim': {'file': 'config_editor.py', 'label': 'Edit Config Value Popup — Comment Text', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_configeditor_draw_popup_warn_2': {'file': 'config_editor.py', 'label': 'Edit Config Value Popup — \'New Value:\' Label', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_popup_normal_2': {'file': 'config_editor.py', 'label': 'Edit Config Value Popup — New-Value Entry Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_configeditor_draw_popup_warn_3': {'file': 'config_editor.py', 'label': 'Edit Config Value Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_popup_invhead': {'file': 'config_editor.py', 'label': 'Enter: Save | Esc: Cancel #2', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_popup_normal_1': {'file': 'file_manager.py', 'label': 'File Manager Sort Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_popup_chrome': {'file': 'file_manager.py', 'label': 'File Manager Sort Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_popup_invhead': {'file': 'file_manager.py', 'label': 'File Manager Sort Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_popup_hilight': {'file': 'file_manager.py', 'label': 'File Manager Sort Popup — Selected Option', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_popup_live': {'file': 'file_manager.py', 'label': 'File Manager Sort Popup — Currently Active Option', 'default_role': 'LIVE', 'default_bold': True},
    'file_manager_filemanagertab_draw_popup_normal_2': {'file': 'file_manager.py', 'label': 'File Manager Sort Popup — Unselected Option', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_menu_popup_normal_1': {'file': 'file_manager.py', 'label': 'File Options Menu Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_menu_popup_chrome': {'file': 'file_manager.py', 'label': 'File Options Menu Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_menu_popup_invhead': {'file': 'file_manager.py', 'label': 'File Options Menu Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_menu_popup_hilight': {'file': 'file_manager.py', 'label': 'File Options Menu Popup — Menu Option (Selected)', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_menu_popup_normal_2': {'file': 'file_manager.py', 'label': 'File Options Menu Popup — Menu Option (Unselected)', 'default_role': 'NORMAL', 'default_bold': True},
    'file_manager_filemanagertab_draw_fixup_popup_normal_1': {'file': 'file_manager.py', 'label': 'Fixup Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_fixup_popup_chrome': {'file': 'file_manager.py', 'label': 'Fixup Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_fixup_popup_invhead': {'file': 'file_manager.py', 'label': 'Fixup Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_fixup_popup_dim': {'file': 'file_manager.py', 'label': 'Fixup Popup — Target Filename', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_fixup_popup_hilight': {'file': 'file_manager.py', 'label': 'Fixup Popup — Checkbox Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_fixup_popup_normal_2': {'file': 'file_manager.py', 'label': 'Fixup Popup — Checkbox Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_normal_1': {'file': 'file_manager.py', 'label': 'Move Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_chrome': {'file': 'file_manager.py', 'label': 'Move Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_popup_invhead': {'file': 'file_manager.py', 'label': 'Move Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_dim': {'file': 'file_manager.py', 'label': 'Move Popup — \'Select a destination:\' Header', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_hilight_1': {'file': 'file_manager.py', 'label': 'Move File Popup — Selected Destination', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_popup_normal_2': {'file': 'file_manager.py', 'label': 'Move File Popup — Unselected Destination', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_hilight_2': {'file': 'file_manager.py', 'label': 'Move Popup — \'Configure New Destination\' (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_popup_system': {'file': 'file_manager.py', 'label': 'Move Popup — \'Configure New Destination\' (Unselected)', 'default_role': 'SYSTEM', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_hilight_3': {'file': 'file_manager.py', 'label': 'Move Popup — Checkbox Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_popup_normal_3': {'file': 'file_manager.py', 'label': 'Move Popup — Checkbox Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_filename_p_normal_1': {'file': 'file_manager.py', 'label': 'Rename/Move Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_filename_p_chrome': {'file': 'file_manager.py', 'label': 'Rename/Move Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_filename_p_invhead': {'file': 'file_manager.py', 'label': 'Rename/Move Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_filename_p_dim': {'file': 'file_manager.py', 'label': 'Streamer: {self._move_filename_streamer}', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_filename_p_warn': {'file': 'file_manager.py', 'label': 'Filename:', 'default_role': 'WARN', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_filename_p_normal_2': {'file': 'file_manager.py', 'label': 'Rename/Move Popup — Filename Entry Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'file_manager_filemanagertab_draw_trim_popup_normal_1': {'file': 'file_manager.py', 'label': 'Trim Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_trim_popup_chrome': {'file': 'file_manager.py', 'label': 'Trim Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_trim_popup_invhead': {'file': 'file_manager.py', 'label': 'Trim Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_trim_popup_dim': {'file': 'file_manager.py', 'label': 'Trim Popup — Target Filename', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_trim_popup_hilight_1': {'file': 'file_manager.py', 'label': 'Trim Popup — Start/End Field (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_trim_popup_normal_2': {'file': 'file_manager.py', 'label': 'Trim Popup — Unselected Field', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_trim_popup_hilight_2': {'file': 'file_manager.py', 'label': 'Trim Popup — Checkbox Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_trim_popup_normal_3': {'file': 'file_manager.py', 'label': 'Trim Popup — Checkbox Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_popup_normal_1': {'file': 'file_manager.py', 'label': 'Split Popup — Background Fill (Job Running)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_popup_chrome_1': {'file': 'file_manager.py', 'label': 'Split Popup — Title (Job Running)', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_split_popup_invhead_1': {'file': 'file_manager.py', 'label': 'Split Popup — Legend Line (Job Running)', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_popup_dim_1': {'file': 'file_manager.py', 'label': 'Split Popup — Target Filename (Job Running)', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_popup_hilight_1': {'file': 'file_manager.py', 'label': 'Split Popup — Stop Job Row (Job Running)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_split_popup_normal_2': {'file': 'file_manager.py', 'label': 'Split Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_popup_chrome_2': {'file': 'file_manager.py', 'label': 'Split Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_split_popup_invhead_2': {'file': 'file_manager.py', 'label': 'Split Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_popup_dim_2': {'file': 'file_manager.py', 'label': 'Split Popup — Target Filename', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_popup_system': {'file': 'file_manager.py', 'label': 'Split Popup — Mode Line', 'default_role': 'SYSTEM', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_popup_hilight_2': {'file': 'file_manager.py', 'label': 'Split Popup — Field Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_split_popup_normal_3': {'file': 'file_manager.py', 'label': 'Split Popup — Field Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_popup_hilight_3': {'file': 'file_manager.py', 'label': 'Split Popup — Stop Job Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_split_popup_normal_4': {'file': 'file_manager.py', 'label': 'Split Popup — Stop Job Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_confirm_normal': {'file': 'file_manager.py', 'label': 'Split Stop Confirm — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_confirm_chrome': {'file': 'file_manager.py', 'label': 'Split Stop Confirm — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_split_confirm_invhead': {'file': 'file_manager.py', 'label': 'Split Stop Confirm — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_split_confirm_warn': {'file': 'file_manager.py', 'label': 'Split Stop Confirm — Confirmation Message', 'default_role': 'WARN', 'default_bold': True},
    'file_manager_filemanagertab_draw_chrome': {'file': 'file_manager.py', 'label': 'FILE MANAGER', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_dim_1': {'file': 'file_manager.py', 'label': 'No OUTPUT_DIR configured on any site.', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_normal': {'file': 'file_manager.py', 'label': 'File Manager — Column Header Row', 'default_role': 'NORMAL', 'default_bold': True},
    'file_manager_filemanagertab_draw_system_1': {'file': 'file_manager.py', 'label': 'File Manager — Subfolder Group Header', 'default_role': 'SYSTEM', 'default_bold': True},
    'file_manager_filemanagertab_draw_dim_2': {'file': 'file_manager.py', 'label': 'File Manager — \'Empty\' Placeholder Row', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_hilight': {'file': 'file_manager.py', 'label': 'File Manager — File Row (Selected)', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_live': {'file': 'file_manager.py', 'label': 'File Manager — File Row (Writing)', 'default_role': 'LIVE', 'default_bold': True},
    'file_manager_filemanagertab_draw_live_2': {'file': 'file_manager.py', 'label': 'File Manager — Scroll-Up Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'file_manager_filemanagertab_draw_live_3': {'file': 'file_manager.py', 'label': 'File Manager — Scroll-Down Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'file_manager_filemanagertab_draw_dim_3': {'file': 'file_manager.py', 'label': 'File Manager — File Row (Idle)', 'default_role': 'NORMAL', 'default_bold': True},
    'file_manager_filemanagertab_draw_system_2': {'file': 'file_manager.py', 'label': 'File Manager — Subfolder Path Prefix', 'default_role': 'WARN', 'default_bold': False},
    'file_manager_filemanagertab_draw_delete': {'file': 'file_manager.py', 'label': 'File Manager — Delete-Mode Info (Permanent)', 'default_role': 'DELETE', 'default_bold': True},
    'file_manager_filemanagertab_draw_dim_4': {'file': 'file_manager.py', 'label': 'File Manager — Delete-Mode Info (Trash)', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_warn': {'file': 'file_manager.py', 'label': 'File Manager — Status Message Line', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_safe_ch_pair': {'file': 'main.py', 'label': 'Box Border (generic, all panels)', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_logo_logo': {'file': 'main.py', 'label': 'Main Logo Banner', 'default_role': 'LOGO', 'default_bold': True},
    'main_jjdlpdashboard_draw_disk_rate_graph': {'file': 'main.py', 'label': 'Top Bar Disk Rate Graph', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_christmas_easte_pair': {'file': 'main.py', 'label': 'Christmas Easter Egg — Tree', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_tabs_hilight': {'file': 'main.py', 'label': 'Tab Bar — Selected Tab', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_tabs_invhead': {'file': 'main.py', 'label': 'Tab Bar — Unselected Tab', 'default_role': 'CHROME', 'default_bold': True},
    'main_jjdlpdashboard_draw_system_panel_system': {'file': 'main.py', 'label': 'SYSTEM', 'default_role': 'SYSTEM', 'default_bold': True},
    'main_jjdlpdashboard_split_after_rows_dim': {'file': 'main.py', 'label': 'System Panel — Stat Row Label', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_split_after_rows_cpair': {'file': 'main.py', 'label': 'System Panel — Stat Row Value (color varies by stat)', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_split_after_rows_rec_1': {'file': 'main.py', 'label': 'System Panel — \'ffmpeg errors\' Section Header', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_2': {'file': 'main.py', 'label': 'System Panel — ffmpeg Error Streamer Name', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_3': {'file': 'main.py', 'label': 'System Panel — ffmpeg Error Count', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_4': {'file': 'main.py', 'label': 'System Panel — \'stalled\' Section Header', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_5': {'file': 'main.py', 'label': 'System Panel — Stalled Streamer Name', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_6': {'file': 'main.py', 'label': 'System Panel — Stalled Duration', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_warn_1': {'file': 'main.py', 'label': 'System Panel — \'ads\' Section Header', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_split_after_rows_warn_2': {'file': 'main.py', 'label': 'Ad detected', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_update_disk_usage_system': {'file': 'main.py', 'label': 'System Panel — \'Disk\' Section Header', 'default_role': 'SYSTEM', 'default_bold': True},
    'main_jjdlpdashboard_update_disk_usage_color': {'file': 'main.py', 'label': 'System Panel — Per-Drive Usage Line', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_update_disk_usage_chrome': {'file': 'main.py', 'label': 'System Panel — Uptime Line', 'default_role': 'CHROME', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_border_hilight': {'file': 'main.py', 'label': 'Site Panel — Border (Selected)', 'default_role': 'HILIGHT', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_border_chrome': {'file': 'main.py', 'label': 'Site Panel — Border (Unselected)', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_chrome_1': {'file': 'main.py', 'label': '{cfg_label}', 'default_role': 'CHROME', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_1': {'file': 'main.py', 'label': 'LIVE:{live_cnt}', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_rec_1': {'file': 'main.py', 'label': 'REC:{rec_cnt}', 'default_role': 'REC', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_1': {'file': 'main.py', 'label': 'OFF:{off_cnt}', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_1': {'file': 'main.py', 'label': 'DIS:{dis_cnt}', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_chrome_2': {'file': 'main.py', 'label': 'Site Panel (Compact) — Column Separator', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_2': {'file': 'main.py', 'label': 'Site Panel (Compact) — Name (Disabled)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_3': {'file': 'main.py', 'label': 'Site Panel (Compact) — Status Badge (Disabled, Flash On)', 'default_role': 'DISABLED', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_disabled_4': {'file': 'main.py', 'label': 'Site Panel (Compact) — Status Badge (Disabled, Flash Off)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_5': {'file': 'main.py', 'label': 'Site Panel (Compact) — Status Badge (Disabled, Never Live)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_live_2': {'file': 'main.py', 'label': 'Site Panel (Compact) — Name (Live)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_3': {'file': 'main.py', 'label': 'Site Panel (Compact) — Status Badge (Recording, Flash On)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_rec_2': {'file': 'main.py', 'label': 'Site Panel (Compact) — Status Badge (Recording, Flash Off)', 'default_role': 'REC', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_4': {'file': 'main.py', 'label': 'Site Panel (Compact) — Status Badge (Live, Not Recording)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_2': {'file': 'main.py', 'label': 'Site Panel (Compact) — Name (Offline)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_dim_3': {'file': 'main.py', 'label': 'Site Panel (Compact) — Status Badge (Offline)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_live_5': {'file': 'main.py', 'label': 'Site Panel (Compact) — Last Live, Recently Live', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_4': {'file': 'main.py', 'label': 'Site Panel (Compact) — Last Live, Older', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_6': {'file': 'main.py', 'label': 'Site Panel (Normal) — Name (Disabled)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_7': {'file': 'main.py', 'label': 'Site Panel (Normal) — Progress Bar (Disabled)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_8': {'file': 'main.py', 'label': 'Site Panel (Normal) — Status Badge (Disabled, Flash On)', 'default_role': 'DISABLED', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_disabled_9': {'file': 'main.py', 'label': 'Site Panel (Normal) — Status Badge (Disabled, Flash Off)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_10': {'file': 'main.py', 'label': 'Site Panel (Normal) — Status Badge (Disabled, Never Live)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_live_6': {'file': 'main.py', 'label': 'Site Panel (Normal) — Name (Live)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_7': {'file': 'main.py', 'label': 'Site Panel (Normal) — Status Badge (Recording, Flash On)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_rec_3': {'file': 'main.py', 'label': 'Site Panel (Normal) — Status Badge (Recording, Flash Off)', 'default_role': 'REC', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_8': {'file': 'main.py', 'label': 'Site Panel (Normal) — Status Badge (Live, Not Recording)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_9': {'file': 'main.py', 'label': 'Site Panel (Normal) — Progress Bar (Live)', 'default_role': 'LIVE', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_dim_5': {'file': 'main.py', 'label': 'Site Panel (Normal) — Name (Offline)', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_6': {'file': 'main.py', 'label': 'Site Panel (Normal) — Status Badge (Offline)', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_7': {'file': 'main.py', 'label': 'Site Panel (Normal) — Progress Bar (Offline)', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_chrome_3': {'file': 'main.py', 'label': 'Site Panel (Normal) — Duration Column', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_live_10': {'file': 'main.py', 'label': 'Site Panel (Normal) — Last Live, Recently Live', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_8': {'file': 'main.py', 'label': 'Site Panel (Normal) — Last Live, Older', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_warn': {'file': 'main.py', 'label': 'Next check: {_nxt_str}', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_dim_1': {'file': 'main.py', 'label': 'Activity Log — \'Site:\' Label', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_hilight': {'file': 'main.py', 'label': 'Activity Log — Site Tab (Selected)', 'default_role': 'HILIGHT', 'default_bold': False},
    'main_jjdlpdashboard_draw_log_tab_chrome': {'file': 'main.py', 'label': 'Activity Log — Site Tab (Unselected)', 'default_role': 'CHROME', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_dim_2': {'file': 'main.py', 'label': 'Activity Log — Title', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_dim_3': {'file': 'main.py', 'label': 'Activity Log — Line (Normal)', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_live': {'file': 'main.py', 'label': 'Activity Log — Line (Live/Recording Started)', 'default_role': 'LOGO', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_rec': {'file': 'main.py', 'label': 'Activity Log — Line (Error/Stall/Stopped)', 'default_role': 'REC', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_warn_1': {'file': 'main.py', 'label': 'Activity Log — Line (Warning)', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_warn_2': {'file': 'main.py', 'label': '↑{self._log_scroll}/{max_scroll}', 'default_role': 'WARN', 'default_bold': False},
    'main_jjdlpdashboard_draw_pipe_tab_bar_dim': {'file': 'main.py', 'label': 'Stdout/Stderr Tabs — \'Site:\' Label', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_pipe_tab_bar_hilight': {'file': 'main.py', 'label': 'Stdout/Stderr Tabs — Site Tab (Selected)', 'default_role': 'HILIGHT', 'default_bold': False},
    'main_jjdlpdashboard_draw_pipe_tab_bar_chrome': {'file': 'main.py', 'label': 'Stdout/Stderr Tabs — Site Tab (Unselected)', 'default_role': 'CHROME', 'default_bold': True},
    'main_jjdlpdashboard_draw_streamer_panel_border_pair': {'file': 'main.py', 'label': 'STREAMERS', 'default_role': 'HILIGHT', 'default_bold': False},
    'main_jjdlpdashboard_draw_streamer_panel_hilight': {'file': 'main.py', 'label': 'Streamer Sub-Tab List — Selected Row', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_streamer_panel_dim': {'file': 'main.py', 'label': 'Streamer Sub-Tab List — Unselected Row', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_pipe_tab_border_pair': {'file': 'main.py', 'label': '{title}{title_suffix}', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_pipe_tab_dim': {'file': 'main.py', 'label': 'Stdout/Stderr Panel — Content Line', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_pipe_tab_warn': {'file': 'main.py', 'label': 'Stdout/Stderr Panel — Scroll Indicator', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_eventsub_tab_invhead_1': {'file': 'main.py', 'label': 'TWITCH EVENTSUB', 'default_role': 'INVHEAD', 'default_bold': True},
    'main_jjdlpdashboard_draw_eventsub_tab_warn': {'file': 'main.py', 'label': 'EventSub Tab — Site Header (\'-- {label} --\')', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_eventsub_tab_dim': {'file': 'main.py', 'label': 'EventSub not available', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_eventsub_tab_invhead_2': {'file': 'main.py', 'label': 'EventSub Tab — Stat Row Label', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_eventsub_tab_cpair': {'file': 'main.py', 'label': 'EventSub Tab — Stat Row Value (color varies by stat)', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_footer_invhead': {'file': 'main.py', 'label': 'Bottom Footer / Key Legend Bar', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_normal_1': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_warn_1': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Title', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_dim_1': {'file': 'main.py', 'label': 'Site: {site_lbl}', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_live_1': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Result Message (Disable/Remove)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_dim_2': {'file': 'main.py', 'label': 'No enabled streamers. #2', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_invhead_1': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Legend (Empty List)', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_hilight_1': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Selected Streamer', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_normal_2': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Unselected Streamer', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_invhead_2': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Legend (List Picker)', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_live_2': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Result Message (Add)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_chrome': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — \'Re-enable disabled:\' Header', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_hilight_2': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Disabled Streamer Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_dim_3': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Disabled Streamer Row (Unselected)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_dim_4': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — \'No disabled streamers.\' Message', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_warn_2': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — \'New username:\' Label', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_hilight_3': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Username Input (Focused)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_normal_3': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Username Input (Unfocused)', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_invhead_3': {'file': 'main.py', 'label': 'Add/Remove/Disable Overlay — Legend (Add Mode)', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_refresh_screen_normal': {'file': 'main.py', 'label': 'Full-Screen Background Color', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_refresh_screen_chrome_1': {'file': 'main.py', 'label': 'Top-Right System Clock', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_refresh_screen_warn': {'file': 'main.py', 'label': 'Update Available', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_refresh_screen_dim': {'file': 'main.py', 'label': 'v{__version__}', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_refresh_screen_chrome_2': {'file': 'main.py', 'label': 'Separator', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_write_failure_a_delete': {'file': 'main.py', 'label': 'Recording Failure Alert — Box, Title, Message, Names', 'default_role': 'DELETE', 'default_bold': True},
    'main_jjdlpdashboard_draw_write_failure_a_invhead': {'file': 'main.py', 'label': 'Recording Failure Alert — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_exit_confirm_po_normal_1': {'file': 'main.py', 'label': 'Exit Confirm Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_exit_confirm_po_warn': {'file': 'main.py', 'label': 'Exit Confirm Popup — Title', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_exit_confirm_po_normal_2': {'file': 'main.py', 'label': 'Exit Confirm Popup — Message Text', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_exit_confirm_po_hilight_1': {'file': 'main.py', 'label': 'Exit Confirm Popup — \'Yes\' Button (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_exit_confirm_po_normal_3': {'file': 'main.py', 'label': 'Exit Confirm Popup — \'Yes\' Button (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_exit_confirm_po_hilight_2': {'file': 'main.py', 'label': 'Exit Confirm Popup — \'No\' Button (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_exit_confirm_po_normal_4': {'file': 'main.py', 'label': 'Exit Confirm Popup — \'No\' Button (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_exit_confirm_po_invhead': {'file': 'main.py', 'label': 'Exit Confirm Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_changelog_popup_normal_1': {'file': 'main.py', 'label': 'Changelog Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_changelog_popup_hilight': {'file': 'main.py', 'label': 'Changelog Popup — Title', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_changelog_popup_normal_2': {'file': 'main.py', 'label': 'Changelog Popup — Content Line', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_changelog_popup_invhead': {'file': 'main.py', 'label': 'Changelog Popup — Scroll Indicator / Legend', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum0': {'file': 'main.py', 'label': 'Config Picker Splash — Full-Screen Background', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum6': {'file': 'main.py', 'label': 'Config Picker Splash — Logo', 'default_role': 'LOGO', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum1_1': {'file': 'main.py', 'label': 'Config Picker Splash — System Clock', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum1_2': {'file': 'main.py', 'label': 'Config Picker Splash — Separator Line', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum5_1': {'file': 'main.py', 'label': 'Config Picker Splash — Title', 'default_role': 'INVHEAD', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum3_1': {'file': 'main.py', 'label': 'Config Picker Splash — Instructions Line', 'default_role': 'WARN', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum2': {'file': 'main.py', 'label': 'Config Picker Splash — File Row (Cursor)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum4': {'file': 'main.py', 'label': 'Config Picker Splash — File Row (Checked)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum1_3': {'file': 'main.py', 'label': 'Config Picker Splash — File Row (Unchecked)', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum3_2': {'file': 'main.py', 'label': 'Config Picker Splash — \'Do Not Show Again\' (Checked)', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum3_3': {'file': 'main.py', 'label': 'Config Picker Splash — \'Do Not Show Again\' (Unchecked)', 'default_role': 'WARN', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum5_2': {'file': 'main.py', 'label': 'Config Picker Splash — Footer', 'default_role': 'INVHEAD', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum0': {'file': 'main.py', 'label': 'Browser Picker Splash — Full-Screen Background', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum6': {'file': 'main.py', 'label': 'Browser Picker Splash — Logo', 'default_role': 'LOGO', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum1_1': {'file': 'main.py', 'label': 'Browser Picker Splash — System Clock', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum1_2': {'file': 'main.py', 'label': 'Browser Picker Splash — Separator Line', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum5_1': {'file': 'main.py', 'label': 'Browser Picker Splash — Title', 'default_role': 'INVHEAD', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum3_1': {'file': 'main.py', 'label': 'Browser Picker Splash — Instructions Line', 'default_role': 'WARN', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum3_2': {'file': 'main.py', 'label': 'Browser Picker Splash — Chrome-Unsupported Warning', 'default_role': 'WARN', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum4': {'file': 'main.py', 'label': 'Browser Picker Splash — \'Applies to:\' Line', 'default_role': 'LIVE', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum2': {'file': 'main.py', 'label': 'Browser Picker Splash — Browser Row (Cursor)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum1_3': {'file': 'main.py', 'label': 'Browser Picker Splash — Browser Row (Not Cursor)', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum3_3': {'file': 'main.py', 'label': 'Browser Picker Splash — \'Do Not Show Again\' (Checked)', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum3_4': {'file': 'main.py', 'label': 'Browser Picker Splash — \'Do Not Show Again\' (Unchecked)', 'default_role': 'WARN', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum5_2': {'file': 'main.py', 'label': 'Browser Picker Splash — Footer', 'default_role': 'INVHEAD', 'default_bold': True},
}


def _color_name_to_const(name):
    return getattr(curses, f'COLOR_{name}', curses.COLOR_WHITE)


def _color_const_to_name(value):
    for name in COLOR_NAMES:
        if _color_name_to_const(name) == value:
            return name
    return 'WHITE'


# ─────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────
def _theme_json_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.json")


def load_theme():
    """Load theme.json. Returns a dict with 'base_scheme_idx', 'role_overrides',
    and 'site_overrides' keys, filled with safe defaults if the file is
    missing, empty, or unparseable. role_overrides is stored per base scheme
    index: {str(scheme_idx): {role: {'fg': name, 'bg': name}}}."""
    path = _theme_json_path()
    default = {'base_scheme_idx': DEFAULT_SCHEME_IDX, 'role_overrides': {}, 'site_overrides': {}}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.loads(f.read())
        if not isinstance(data, dict):
            return default
        data.setdefault('base_scheme_idx', DEFAULT_SCHEME_IDX)
        data.setdefault('role_overrides', {})
        data.setdefault('site_overrides', {})
        return data
    except FileNotFoundError:
        return default
    except Exception:
        # Torn/corrupt write — don't crash startup, just fall back to defaults.
        return default


def save_theme(data):
    """Atomically write theme.json (tmp file + os.replace), matching the
    pattern used for global.json. Silently ignores errors."""
    path = _theme_json_path()
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────
# Runtime state — loaded once at startup, mutated by ThemeManager, applied
# to curses pairs whenever it changes.
# ─────────────────────────────────────────────────────────────────────────
_state = load_theme()


def get_state():
    return _state


def set_state(data):
    global _state
    _state = data


# ─────────────────────────────────────────────────────────────────────────
# The core indirection function every call site now goes through.
# ─────────────────────────────────────────────────────────────────────────
def _role_to_pair(owner, role, fallback=0):
    """Resolve a role name to a curses pair number. With a real owner
    (dashboard) use its C_<ROLE> constant — that's what actually defines the
    pair. With owner=None (the standalone splash/picker screens that run
    before any dashboard exists) fall back to the module-level ROLE_PAIR_NUM
    table, so role overrides work there exactly like everywhere else."""
    if owner is not None:
        return getattr(owner, f'C_{role}', fallback)
    return ROLE_PAIR_NUM.get(role, fallback)


def attr(owner, tag, runtime_pair=None):
    """Return the curses attribute (color pair | optional bold) to use for a
    given call site.

    owner:          the object whose C_* constants resolve a role name to a
                    pair number (self / db / self.dashboard / the
                    JJDlpDashboard class), or None for the standalone
                    splash/picker screens that run before any dashboard
                    exists (role pairs then come from ROLE_PAIR_NUM).
    tag:            the stable site identifier (see SITE_REGISTRY), which is
                    the single source of truth for this site's default role
                    and bold — call sites no longer pass their own defaults.
    runtime_pair:   optional pair number the call site computes at runtime
                    (e.g. a stat value colored by its current state). It wins
                    over the site's default_role — the role only kicks in as
                    a fallback. A saved per-site override role always wins
                    over both.

    Resolution order: override role → runtime_pair → default_role → pair 0.
    Every SITE_REGISTRY entry has a default_role, so every site is editable
    (role + bold) in the theme editor the same way.
    """
    entry = SITE_REGISTRY.get(tag, {})
    default_role = entry.get('default_role')
    default_bold = entry.get('default_bold', False)

    ov = _state.get('site_overrides', {}).get(tag)

    if ov is not None and ov.get('role'):
        pair_num = _role_to_pair(owner, ov['role'],
                                 runtime_pair if runtime_pair is not None else 0)
    elif runtime_pair is not None:
        pair_num = runtime_pair
    elif default_role is not None:
        pair_num = _role_to_pair(owner, default_role, 0)
    else:
        pair_num = 0
    bold = ov.get('bold', default_bold) if ov is not None else default_bold

    result = curses.color_pair(pair_num)
    if bold:
        result |= curses.A_BOLD
    return result


def role_overrides_for(scheme_idx=None):
    """Return the role-override dict ({role: {'fg': name, 'bg': name}}) for
    the given scheme index, defaulting to the active base scheme. The dict is
    created on demand, so callers can mutate it directly."""
    idx = str(_state.get('base_scheme_idx', DEFAULT_SCHEME_IDX) if scheme_idx is None else scheme_idx)
    return _state.setdefault('role_overrides', {}).setdefault(idx, {})


# ─────────────────────────────────────────────────────────────────────────
# Applying role fg/bg overrides on top of a base COLOR_SCHEMES tuple.
# ─────────────────────────────────────────────────────────────────────────
def resolve_scheme_values(dashboard=None, scheme_idx=None):
    """Return a dict {role: {'fg': curses.COLOR_*, 'bg': curses.COLOR_* or
    None}} for all 13 roles, combining a base scheme with any saved
    role_overrides for it (defaulting to the active base scheme). bg is None
    for roles that share the ambient background.

    dashboard is accepted for backward compatibility but no longer used —
    COLOR_SCHEMES now lives in theme.py."""
    if scheme_idx is None:
        scheme_idx = _state.get('base_scheme_idx', DEFAULT_SCHEME_IDX)
    scheme = COLOR_SCHEMES[scheme_idx % len(COLOR_SCHEMES)]
    values = {}
    for role in ROLE_ORDER:
        values[role] = {'fg': None, 'bg': None}
    for (role, field), value in zip(SCHEME_TUPLE_FIELDS, scheme):
        values[role][field] = value

    for role, ov in role_overrides_for(scheme_idx).items():
        if role not in values:
            continue
        if ov.get('fg'):
            values[role]['fg'] = _color_name_to_const(ov['fg'])
        if ov.get('bg'):
            values[role]['bg'] = _color_name_to_const(ov['bg'])
        # bold is handled separately per-site (or could be layered as a
        # role-level default in a future pass); role_overrides only carries
        # fg/bg today.
    return values


# ─────────────────────────────────────────────────────────────────────────
# Palette normalization — why this exists and what it does
#
# curses only sends color *indices* (0-7) to the terminal; the terminal
# maps each index to its own RGB. Distros ship wildly different default
# palettes (GNOME blue #3465A4, XFCE blue #0000EE, Konsole blue #1D99F3...),
# so the exact same app looks different on every Linux terminal. ANSI
# (ECMA-48) names the colors but doesn't define their RGB, so there's no
# standard to lean on at the curses level.
#
# Fix: when the terminal advertises can_change_color() (xterm, GNOME,
# XFCE, Konsole, kitty, wezterm, foot, ...), re-pin the 8 base color
# indices to the exact RGBs the app already renders on Windows (Windows
# Terminal's default "Campbell" palette). Every such Linux terminal then
# shows identical colors — matching the Windows look. Terminals that can't
# change colors (PDCurses/Windows, tmux/screen, 8-color) are left
# untouched and fall back to their own palette, exactly as before.
#
# The terminal's palette stays modified for the whole session, so the
# original values are saved here and written back on clean exit (atexit)
# via raw OSC 4 sequences (they work after curses has already torn down).
# If the process is SIGKILLed the palette lingers until `reset`.
#
# To tune the pinned colors, edit _PALETTE_RGB. To disable normalization
# entirely (e.g. if it misbehaves on your terminal), set _PALETTE_RGB = {}.
# See docs/color-scheme-options.txt for the alternative approaches.
# ─────────────────────────────────────────────────────────────────────────
# Windows Terminal "Campbell" palette (the default on Windows), hex ->
# curses 0-1000 scale (value * 1000 / 255).
_PALETTE_RGB = {
    curses.COLOR_BLACK:    (47, 47, 47),       # 0C0C0C
    curses.COLOR_RED:      (773, 59, 122),     # C50F1F
    curses.COLOR_GREEN:    (75, 631, 55),      # 13A10E
    curses.COLOR_YELLOW:   (757, 612, 0),      # C19C00
    curses.COLOR_BLUE:     (0, 216, 855),      # 0037DA
    curses.COLOR_MAGENTA:  (533, 90, 596),     # 881798
    curses.COLOR_CYAN:     (227, 588, 867),    # 3A96DD
    curses.COLOR_WHITE:    (800, 800, 800),    # CCCCCC
}

_PALETTE_NORMALIZED = False
_ORIG_PALETTE = {}   # index -> (r, g, b) saved before normalization


def _palette_1000_to_hex4(value):
    return '%04x' % round(value * 255 / 1000)


def _osc4_set(index, r, g, b):
    """Emit an OSC 4 (Set Palette Color) sequence straight to stdout —
    usable even after endwin(), so it can restore the palette on exit."""
    if not sys.stdout.isatty():
        return
    sys.stdout.write(
        '\x1b]4;%d;rgb:%s/%s/%s\x1b\\'
        % (index, _palette_1000_to_hex4(r),
           _palette_1000_to_hex4(g), _palette_1000_to_hex4(b)))
    sys.stdout.flush()


def _restore_palette():
    global _PALETTE_NORMALIZED
    if not _PALETTE_NORMALIZED:
        return
    for idx, (r, g, b) in _ORIG_PALETTE.items():
        try:
            _osc4_set(idx, r, g, b)
        except Exception:
            pass
    _PALETTE_NORMALIZED = False


def normalize_palette():
    """Pin the 8 base color indices to the app's exact RGBs (matching the
    Windows look) on any terminal that supports changing colors. Runs once
    per process; no-op on Windows/PDCurses, multiplexers, and 8-color
    terminals (can_change_color() is False there)."""
    global _PALETTE_NORMALIZED
    if _PALETTE_NORMALIZED:
        return True
    try:
        if not _PALETTE_RGB or not curses.has_colors() or not curses.can_change_color():
            return False
        for idx, rgb in _PALETTE_RGB.items():
            try:
                _ORIG_PALETTE[idx] = curses.color_content(idx)
                curses.init_color(idx, *rgb)
            except curses.error:
                _ORIG_PALETTE.pop(idx, None)
        if _ORIG_PALETTE:
            _PALETTE_NORMALIZED = True
            atexit.register(_restore_palette)
            return True
        return False
    except curses.error:
        return False


def apply_palette(dashboard):
    """Re-initialize all 13 curses pairs from the active base scheme + role
    overrides. Call this instead of dashboard._apply_color_scheme() once
    theme.py owns palette application; safe to call at any time (e.g. right
    after loading/saving theme.json, or when the base scheme changes)."""
    normalize_palette()
    values = resolve_scheme_values()
    ambient_bg = _SCHEME_BACKGROUND.get(
        _state.get('base_scheme_idx', DEFAULT_SCHEME_IDX), curses.COLOR_BLACK)

    for role in ROLE_ORDER:
        pair_num = ROLE_PAIR_NUM[role]
        fg = values[role]['fg']
        bg = values[role]['bg']
        if bg is None:
            bg = ambient_bg
        try:
            curses.init_pair(pair_num, fg, bg)
        except curses.error:
            pass

    # clearok(True) forces the next refresh() on this window
    # to do a full, uncached repaint, so the freshly defined pair colors
    # actually reach the terminal instead of staying stuck until unrelated
    # content happens to change.
    stdscr = getattr(dashboard, 'stdscr', None)
    if stdscr is not None:
        try:
            stdscr.clearok(True)
        except curses.error:
            pass


# ─────────────────────────────────────────────────────────────────────────
# Bake-to-source — DEV FEATURE (hidden 'W' hotkey).
#
# theme.py is the single source of truth for both customization layers, so
# baking just rewrites theme.py itself:
#   • role fg/bg overrides  → the COLOR_SCHEMES tuple for that scheme
#   • per-site role/bold    → the SITE_REGISTRY entry's default_role/default_bold
# No other source file needs to change.
#
# This is a snapshot convenience: runtime behavior is unchanged (theme.json
# keeps winning), and the next app update overwrites these edits.
# ─────────────────────────────────────────────────────────────────────────
_REG_DEFAULT_ROLE_RE = re.compile(r"'default_role':\s*(None|'[A-Z]+')")
_REG_DEFAULT_BOLD_RE = re.compile(r"'default_bold':\s*(True|False)")
_SCHEME_TUPLE_TOKEN_RE = re.compile(r'curses\.COLOR_[A-Z]+')


def _source_file_path(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _read_source_file(name):
    try:
        with open(_source_file_path(name), 'r', encoding='utf-8', newline='') as f:
            return f.read()
    except Exception:
        return None


def _atomic_write_text(path, text):
    """Atomically write a text file (tmp + os.replace), matching save_theme."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return False


def _effective_scheme_colors(scheme_idx):
    """List of effective fg/bg curses color constants (in SCHEME_TUPLE_FIELDS
    order) for the given scheme index, base tuple + role overrides applied."""
    values = resolve_scheme_values(scheme_idx=scheme_idx)
    return [values[role][field] for role, field in SCHEME_TUPLE_FIELDS]


def _rewrite_scheme_tuple(theme_text, scheme_idx, color_values):
    """Rewrite the scheme_idx-th tuple inside theme.py's own
    COLOR_SCHEMES = [...] so each curses.COLOR_X token (in
    SCHEME_TUPLE_FIELDS order) matches color_values.
    Returns (new_text, changed)."""
    lines = theme_text.splitlines(keepends=True)

    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith('COLOR_SCHEMES = ['):
            start = i
            break
    if start is None:
        return theme_text, False

    end = None
    for i in range(start, len(lines)):
        if lines[i].strip() == ']':
            end = i
            break
    if end is None:
        return theme_text, False

    group_starts = [i for i in range(start, end)
                    if lines[i].lstrip().startswith('(curses.COLOR_')]
    if scheme_idx >= len(group_starts):
        return theme_text, False

    gs = group_starts[scheme_idx]
    ge = gs
    while ge <= end and ')' not in lines[ge]:
        ge += 1
    if ge > end:
        return theme_text, False

    by_line = {}
    for li in range(gs, ge + 1):
        segs = [(m.start(), m.end()) for m in _SCHEME_TUPLE_TOKEN_RE.finditer(lines[li])]
        if segs:
            by_line[li] = segs
    token_count = sum(len(segs) for segs in by_line.values())
    if token_count != len(SCHEME_TUPLE_FIELDS):
        return theme_text, False

    new_names = [_color_const_to_name(v) for v in color_values]
    name_idx = 0
    changed = False
    for li in sorted(by_line):
        parts = []
        cursor = 0
        for s, e in by_line[li]:
            name = new_names[name_idx]
            name_idx += 1
            parts.append(lines[li][cursor:s])
            parts.append(f'curses.COLOR_{name}')
            cursor = e
        parts.append(lines[li][cursor:])
        rebuilt = ''.join(parts)
        if rebuilt != lines[li]:
            lines[li] = rebuilt
            changed = True
    return (''.join(lines), True) if changed else (theme_text, False)


def _rewrite_registry_entry(theme_text, tag, ov):
    """Rewrite a SITE_REGISTRY entry's default_role/default_bold to match the
    override. Returns (new_text, changed)."""
    lines = theme_text.splitlines(keepends=True)
    target = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("'" + tag + "':"):
            target = i
            break
    if target is None:
        return theme_text, False

    new_line = lines[target]
    if ov.get('role'):
        role_match = _REG_DEFAULT_ROLE_RE.search(new_line)
        if role_match and role_match.group(1) != 'None':
            new_line = _REG_DEFAULT_ROLE_RE.sub(
                f"'default_role': '{ov['role']}'", new_line, count=1)
    if 'bold' in ov:
        new_line = _REG_DEFAULT_BOLD_RE.sub(
            f"'default_bold': {ov['bold']}", new_line, count=1)

    if new_line == lines[target]:
        return theme_text, False
    lines[target] = new_line
    return ''.join(lines), True


def bake_to_source(dashboard=None):
    """Write the currently-active customizations into theme.py, so they
    become the new defaults. theme.py is the only file touched:
      • base-scheme role-color overrides → the COLOR_SCHEMES tuple here
      • per-site role/bold overrides     → the SITE_REGISTRY entry here
    Call sites elsewhere carry no defaults of their own, so there's nothing
    else to rewrite.

    dashboard is accepted for backward compatibility but no longer needed.

    Dev feature bound to the hidden 'W' hotkey. Returns a summary dict:
      {'ok': bool, 'error': str|None, 'schemes': [idx, ...],
       'role_sites': int, 'bold_sites': int, 'files': [name, ...]}
    """
    summary = {'ok': True, 'error': None, 'schemes': [],
               'role_sites': 0, 'bold_sites': 0, 'files': []}

    theme_text = _read_source_file('theme.py')
    if theme_text is None:
        summary['ok'] = False
        summary['error'] = "Could not read theme.py"
        return summary

    dirty = False

    # 1. Base-scheme role-color overrides → COLOR_SCHEMES tuples (this file)
    role_overrides = _state.get('role_overrides', {})
    for idx in sorted(int(k) for k in role_overrides if str(k).isdigit()):
        colors = _effective_scheme_colors(idx)
        new_text, changed = _rewrite_scheme_tuple(theme_text, idx, colors)
        if changed:
            theme_text = new_text
            dirty = True
            summary['schemes'].append(idx)

    # 2. Per-site overrides → SITE_REGISTRY entries (this file)
    site_overrides = _state.get('site_overrides', {})
    for tag, ov in sorted(site_overrides.items()):
        entry = SITE_REGISTRY.get(tag)
        if entry is None:
            continue
        if not ov.get('role') and 'bold' not in ov:
            continue
        new_text, changed = _rewrite_registry_entry(theme_text, tag, ov)
        if changed:
            theme_text = new_text
            dirty = True
            if ov.get('role'):
                summary['role_sites'] += 1
            if 'bold' in ov:
                summary['bold_sites'] += 1

    if not dirty:
        summary['ok'] = False
        summary['error'] = "Nothing to bake — no role or site overrides are active."
        return summary

    if _atomic_write_text(_source_file_path('theme.py'), theme_text):
        summary['files'].append('theme.py')
    else:
        summary['ok'] = False
        summary['error'] = "Failed to write theme.py"

    return summary


# ─────────────────────────────────────────────────────────────────────────
# ThemeManager — owns the theme popup (base scheme picker, role color editor,
# and per-site browser), bound to the 'n' key.
# ─────────────────────────────────────────────────────────────────────────
class ThemeManager:
    """Popup UI for customizing themes. Mirrors the shape of
    config_editor.SiteSortManager: owns its own open/close state, handles
    keys while open, draws itself on top of everything else."""

    MODE_MAIN = 'main'                # top-level menu: scheme / roles / sites
    MODE_SCHEME_SELECT = 'scheme'     # pick a base COLOR_SCHEMES entry
    MODE_ROLE_LIST = 'role_list'      # list of 13 roles
    MODE_ROLE_EDIT = 'role_edit'      # editing one role's fg/bg
    MODE_SITE_LIST = 'site_list'      # browsable/searchable list of 359 sites
    MODE_SITE_EDIT = 'site_edit'      # editing one site's role + bold

    # SCHEME_NAMES lives at module level now (alongside COLOR_SCHEMES);
    # kept accessible as self.SCHEME_NAMES below for existing call sites.
    SCHEME_NAMES = SCHEME_NAMES

    def __init__(self, dashboard):
        self.dashboard = dashboard
        self.popup_open = False
        self.mode = self.MODE_MAIN
        self._main_sel = 0
        self._solid_bg = True   # solid (opaque) popup background; 'f' toggles

        self._scheme_sel = _state.get('base_scheme_idx', DEFAULT_SCHEME_IDX)

        self._role_sel = 0
        self._role_edit_field = 'fg'   # 'fg' or 'bg'

        self._site_filter = ""
        self._site_filtered = []       # list of tags matching current filter
        self._site_sel = 0
        self._site_scroll = 0
        self._site_edit_field = 'role'  # 'role' or 'bold'
        self._site_edit_role_idx = 0

    # ── Public API ──────────────────────────────────────────────────────
    def open_popup(self):
        self.mode = self.MODE_MAIN
        self._main_sel = 0
        self.popup_open = True

    def close_popup(self):
        self.popup_open = False
        save_theme(_state)

    # ── Key handling ────────────────────────────────────────────────────
    def handle_key(self, key):
        """Handle keys while the theme popup is open. Always returns True
        (keys are consumed and never leak to the dashboard while open)."""
        if not self.popup_open:
            return False

        if key in (ord('f'), ord('F')) and self.mode != self.MODE_SITE_LIST:
            # Toggle between a solid (opaque) and translucent popup background.
            # Not active in the site browser so 'f' can still be typed into
            # the filter there.
            self._solid_bg = not self._solid_bg
            return True

        if self.mode == self.MODE_MAIN:
            self._handle_main_key(key)
        elif self.mode == self.MODE_SCHEME_SELECT:
            self._handle_scheme_key(key)
        elif self.mode == self.MODE_ROLE_LIST:
            self._handle_role_list_key(key)
        elif self.mode == self.MODE_ROLE_EDIT:
            self._handle_role_edit_key(key)
        elif self.mode == self.MODE_SITE_LIST:
            self._handle_site_list_key(key)
        elif self.mode == self.MODE_SITE_EDIT:
            self._handle_site_edit_key(key)
        return True

    def _handle_main_key(self, key):
        options = ["Pick base scheme", "Customize role colors",
                   "Customize individual call sites", "Close"]
        if key == 27:
            self.close_popup()
        elif key == curses.KEY_UP:
            self._main_sel = max(0, self._main_sel - 1)
        elif key == curses.KEY_DOWN:
            self._main_sel = min(len(options) - 1, self._main_sel + 1)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459, ord(' ')):
            if self._main_sel == 0:
                self._scheme_sel = _state.get('base_scheme_idx', DEFAULT_SCHEME_IDX)
                self.mode = self.MODE_SCHEME_SELECT
            elif self._main_sel == 1:
                self._role_sel = 0
                self.mode = self.MODE_ROLE_LIST
            elif self._main_sel == 2:
                self._site_filter = ""
                self._recompute_site_filter()
                self._site_sel = 0
                self._site_scroll = 0
                self.mode = self.MODE_SITE_LIST
            else:
                self.close_popup()

    def _handle_scheme_key(self, key):
        n = len(COLOR_SCHEMES)
        if key == 27:
            self.mode = self.MODE_MAIN
        elif key == curses.KEY_UP:
            self._scheme_sel = (self._scheme_sel - 1) % n
        elif key == curses.KEY_DOWN:
            self._scheme_sel = (self._scheme_sel + 1) % n
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459, ord(' ')):
            _state['base_scheme_idx'] = self._scheme_sel
            apply_palette(self.dashboard)
            save_theme(_state)
            self.mode = self.MODE_MAIN

    def _handle_role_list_key(self, key):
        n = len(ROLE_ORDER)
        if key == 27:
            self.mode = self.MODE_MAIN
        elif key == curses.KEY_UP:
            self._role_sel = max(0, self._role_sel - 1)
        elif key == curses.KEY_DOWN:
            self._role_sel = min(n - 1, self._role_sel + 1)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459, ord(' ')):
            self._role_edit_field = 'fg'
            self.mode = self.MODE_ROLE_EDIT
        elif key in (ord('r'), ord('R')):
            # Reset this role's override back to the active scheme's base value.
            role = ROLE_ORDER[self._role_sel]
            role_overrides_for().pop(role, None)
            apply_palette(self.dashboard)
            save_theme(_state)

    def _handle_role_edit_key(self, key):
        role = ROLE_ORDER[self._role_sel]
        values = resolve_scheme_values(self.dashboard)
        can_edit_bg = role in ROLE_HAS_OWN_BG

        if key == 27:
            self.mode = self.MODE_ROLE_LIST
        elif key in (ord('\t'),):
            if can_edit_bg:
                self._role_edit_field = 'bg' if self._role_edit_field == 'fg' else 'fg'
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN):
            field = self._role_edit_field
            if field == 'bg' and not can_edit_bg:
                field = 'fg'
            current = values[role][field]
            idx = COLOR_NAMES.index(_color_const_to_name(current))
            delta = 1 if key in (curses.KEY_RIGHT, curses.KEY_DOWN) else -1
            new_name = COLOR_NAMES[(idx + delta) % len(COLOR_NAMES)]

            role_overrides = role_overrides_for()
            ov = role_overrides.setdefault(role, {})
            ov[field] = new_name
            apply_palette(self.dashboard)
        elif key in (ord('r'), ord('R')):
            role_overrides_for().pop(role, None)
            apply_palette(self.dashboard)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            save_theme(_state)
            self.mode = self.MODE_ROLE_LIST

    def _recompute_site_filter(self):
        needle = self._site_filter.lower()
        tags = sorted(SITE_REGISTRY.keys(),
                      key=lambda t: (SITE_REGISTRY[t]['file'], SITE_REGISTRY[t]['label']))
        if not needle:
            self._site_filtered = tags
        else:
            self._site_filtered = [
                t for t in tags
                if needle in SITE_REGISTRY[t]['label'].lower()
                or needle in SITE_REGISTRY[t]['file'].lower()
                or needle in t.lower()
            ]
        self._site_sel = min(self._site_sel, max(0, len(self._site_filtered) - 1))

    def _handle_site_list_key(self, key):
        n = len(self._site_filtered)
        if key == 27:
            if self._site_filter:
                self._site_filter = ""
                self._recompute_site_filter()
            else:
                self.mode = self.MODE_MAIN
        elif key == curses.KEY_UP:
            self._site_sel = max(0, self._site_sel - 1)
        elif key == curses.KEY_DOWN:
            self._site_sel = min(max(0, n - 1), self._site_sel + 1)
        elif key == curses.KEY_PPAGE:
            self._site_sel = max(0, self._site_sel - 15)
        elif key == curses.KEY_NPAGE:
            self._site_sel = min(max(0, n - 1), self._site_sel + 15)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self._site_filter = self._site_filter[:-1]
            self._recompute_site_filter()
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            if n:
                tag = self._site_filtered[self._site_sel]
                entry = SITE_REGISTRY[tag]
                ov = _state.get('site_overrides', {}).get(tag, {})
                default_role = entry['default_role']
                current_role = ov.get('role', default_role)
                self._site_edit_role_idx = (
                    ROLE_ORDER.index(current_role) if current_role in ROLE_ORDER else 0
                )
                self.mode = self.MODE_SITE_EDIT
        elif 32 <= key < 127:
            self._site_filter += chr(key)
            self._recompute_site_filter()

    def _handle_site_edit_key(self, key):
        n = len(self._site_filtered)
        if not n:
            self.mode = self.MODE_SITE_LIST
            return
        tag = self._site_filtered[self._site_sel]
        entry = SITE_REGISTRY[tag]
        site_overrides = _state.setdefault('site_overrides', {})

        if key == 27:
            self.mode = self.MODE_SITE_LIST
        elif key in (ord('\t'),):
            self._site_edit_field = 'bold' if self._site_edit_field == 'role' else 'role'
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN):
            if self._site_edit_field == 'role' and entry['default_role'] is not None:
                delta = 1 if key in (curses.KEY_RIGHT, curses.KEY_DOWN) else -1
                self._site_edit_role_idx = (self._site_edit_role_idx + delta) % len(ROLE_ORDER)
                ov = site_overrides.setdefault(tag, {})
                ov['role'] = ROLE_ORDER[self._site_edit_role_idx]
        elif key in (ord(' '),) and self._site_edit_field == 'bold':
            ov = site_overrides.setdefault(tag, {})
            ov['bold'] = not ov.get('bold', entry['default_bold'])
        elif key in (ord('b'), ord('B')):
            ov = site_overrides.setdefault(tag, {})
            ov['bold'] = not ov.get('bold', entry['default_bold'])
        elif key in (ord('r'), ord('R')):
            site_overrides.pop(tag, None)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            save_theme(_state)
            self.mode = self.MODE_SITE_LIST

    # ── Drawing ─────────────────────────────────────────────────────────
    def draw_popup(self, stdscr):
        if self.mode == self.MODE_MAIN:
            self._draw_main(stdscr)
        elif self.mode == self.MODE_SCHEME_SELECT:
            self._draw_scheme_select(stdscr)
        elif self.mode == self.MODE_ROLE_LIST:
            self._draw_role_list(stdscr)
        elif self.mode == self.MODE_ROLE_EDIT:
            self._draw_role_edit(stdscr)
        elif self.mode == self.MODE_SITE_LIST:
            self._draw_site_list(stdscr)
        elif self.mode == self.MODE_SITE_EDIT:
            self._draw_site_edit(stdscr)

    def _box(self, stdscr, h, w):
        sh, sw = stdscr.getmaxyx()
        by1 = max(0, (sh - h) // 2)
        bx1 = max(0, (sw - w) // 2)
        by2 = min(sh - 1, by1 + h)
        bx2 = min(sw - 1, bx1 + w)
        db = self.dashboard
        if self._solid_bg:
            # Opaque fill so the dashboard content behind the popup is hidden.
            fill_attr = curses.color_pair(db.C_NORMAL)
            for y in range(by1, by2 + 1):
                db.safe_addstr(stdscr, y, bx1, " " * (bx2 - bx1 + 1), fill_attr)
        db.draw_box(stdscr, by1, bx1, by2, bx2, db.C_CHROME)
        return by1, bx1, by2, bx2

    def _draw_main(self, stdscr):
        db = self.dashboard
        options = ["Pick base scheme", "Customize role colors",
                   "Customize individual call sites", "Close"]
        h = len(options) + 4
        w = 42
        by1, bx1, by2, bx2 = self._box(stdscr, h, w)
        db.safe_addstr(stdscr, by1, bx1 + 2, " THEME ",
                        curses.color_pair(db.C_INVHEAD) | curses.A_BOLD)
        for i, opt in enumerate(options):
            attr_ = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                     if i == self._main_sel else curses.color_pair(db.C_NORMAL))
            prefix = "> " if i == self._main_sel else "  "
            db.safe_addstr(stdscr, by1 + 2 + i, bx1 + 2, prefix + opt, attr_)
        db.safe_addstr(stdscr, by2, bx1 + 2, " \u2191\u2193:Move  Enter:Select  f:BG  Esc:Close ",
                        curses.color_pair(db.C_INVHEAD))

    def _draw_scheme_select(self, stdscr):
        db = self.dashboard
        n = len(self.SCHEME_NAMES)
        h = n + 4
        w = 44
        by1, bx1, by2, bx2 = self._box(stdscr, h, w)
        db.safe_addstr(stdscr, by1, bx1 + 2, " BASE SCHEME ",
                        curses.color_pair(db.C_INVHEAD) | curses.A_BOLD)
        for i, name in enumerate(self.SCHEME_NAMES):
            is_cur = (i == _state.get('base_scheme_idx', DEFAULT_SCHEME_IDX))
            if i == self._scheme_sel:
                attr_ = curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
            elif is_cur:
                attr_ = curses.color_pair(db.C_LIVE) | curses.A_BOLD
            else:
                attr_ = curses.color_pair(db.C_NORMAL)
            prefix = "> " if i == self._scheme_sel else ("* " if is_cur else "  ")
            db.safe_addstr(stdscr, by1 + 2 + i, bx1 + 2, (prefix + name)[:w - 4], attr_)
        db.safe_addstr(stdscr, by2, bx1 + 2, " \u2191\u2193:Move  Enter:Apply  f:BG  Esc:Back ",
                        curses.color_pair(db.C_INVHEAD))

    def _draw_role_list(self, stdscr):
        db = self.dashboard
        n = len(ROLE_ORDER)
        h = n + 4
        w = 42
        by1, bx1, by2, bx2 = self._box(stdscr, h, w)
        scheme_idx = _state.get('base_scheme_idx', DEFAULT_SCHEME_IDX) % len(self.SCHEME_NAMES)
        title = f" ROLE COLORS - {self.SCHEME_NAMES[scheme_idx]} "
        db.safe_addstr(stdscr, by1, bx1 + 2, title[:w - 4],
                        curses.color_pair(db.C_INVHEAD) | curses.A_BOLD)
        values = resolve_scheme_values(self.dashboard)
        for i, role in enumerate(ROLE_ORDER):
            label = ROLE_LABELS[role]
            fg_name = _color_const_to_name(values[role]['fg'])
            attr_ = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                     if i == self._role_sel else curses.color_pair(db.C_NORMAL))
            prefix = "> " if i == self._role_sel else "  "
            line = f"{prefix}{label:<24}{fg_name}"
            db.safe_addstr(stdscr, by1 + 2 + i, bx1 + 2, line[:w - 4], attr_)
        db.safe_addstr(stdscr, by2, bx1 + 2,
                        " f:BG  r:Reset ",
                        curses.color_pair(db.C_INVHEAD))

    def _draw_role_edit(self, stdscr):
        db = self.dashboard
        role = ROLE_ORDER[self._role_sel]
        can_edit_bg = role in ROLE_HAS_OWN_BG
        values = resolve_scheme_values(self.dashboard)
        h = 8
        w = 40
        by1, bx1, by2, bx2 = self._box(stdscr, h, w)
        db.safe_addstr(stdscr, by1, bx1 + 2, f" {ROLE_LABELS[role]} ",
                        curses.color_pair(db.C_INVHEAD) | curses.A_BOLD)

        fg_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                   if self._role_edit_field == 'fg' else curses.color_pair(db.C_NORMAL))
        db.safe_addstr(stdscr, by1 + 2, bx1 + 2,
                        f"Foreground: {_color_const_to_name(values[role]['fg'])}", fg_attr)

        if can_edit_bg:
            bg_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                       if self._role_edit_field == 'bg' else curses.color_pair(db.C_NORMAL))
            db.safe_addstr(stdscr, by1 + 3, bx1 + 2,
                            f"Background: {_color_const_to_name(values[role]['bg'])}", bg_attr)
        else:
            db.safe_addstr(stdscr, by1 + 3, bx1 + 2,
                            "Background: (ambient)", curses.color_pair(db.C_DIM))

        db.safe_addstr(stdscr, by1 + 5, bx1 + 2, "Sample: ",
                        curses.color_pair(db.C_NORMAL))
        sample_pair = ROLE_PAIR_NUM[role]
        db.safe_addstr(stdscr, by1 + 5, bx1 + 10, " Aa 123 ",
                        curses.color_pair(sample_pair) | curses.A_BOLD)

        help_line = "Tab:Field  \u2190\u2192:Cycle  r:Reset  Enter:Done" if can_edit_bg \
            else "\u2190\u2192:Cycle  r:Reset  Enter:Done"
        db.safe_addstr(stdscr, by2, bx1 + 2, " " + help_line + " ",
                        curses.color_pair(db.C_INVHEAD))

    def _draw_site_list(self, stdscr):
        db = self.dashboard
        sh, sw = stdscr.getmaxyx()
        h = min(sh - 4, 26)
        w = min(sw - 4, 78)
        by1, bx1, by2, bx2 = self._box(stdscr, h, w)
        title = f" CALL SITES ({len(self._site_filtered)}/{len(SITE_REGISTRY)}) "
        db.safe_addstr(stdscr, by1, bx1 + 2, title,
                        curses.color_pair(db.C_INVHEAD) | curses.A_BOLD)

        filter_line = f"Filter: {self._site_filter}_" if self._site_filter else \
            "Filter: (type to search)"
        db.safe_addstr(stdscr, by1 + 1, bx1 + 2, filter_line[:w - 4],
                        curses.color_pair(db.C_WARN))

        visible = h - 5
        if self._site_sel < self._site_scroll:
            self._site_scroll = self._site_sel
        elif self._site_sel >= self._site_scroll + visible:
            self._site_scroll = self._site_sel - visible + 1

        overrides = _state.get('site_overrides', {})
        for row, i in enumerate(range(self._site_scroll,
                                       min(len(self._site_filtered), self._site_scroll + visible))):
            tag = self._site_filtered[i]
            entry = SITE_REGISTRY[tag]
            is_sel = (i == self._site_sel)
            has_override = tag in overrides
            mark = "*" if has_override else " "
            label = f"{mark}{entry['label']}"
            attr_ = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD if is_sel
                     else (curses.color_pair(db.C_LIVE) if has_override
                           else curses.color_pair(db.C_NORMAL)))
            prefix = "> " if is_sel else "  "
            db.safe_addstr(stdscr, by1 + 3 + row, bx1 + 2, (prefix + label)[:w - 4], attr_)

        db.safe_addstr(stdscr, by2, bx1 + 2,
                        " Enter:Edit  Type:Filter  Esc:Back ",
                        curses.color_pair(db.C_INVHEAD))

    def _draw_site_edit(self, stdscr):
        db = self.dashboard
        n = len(self._site_filtered)
        if not n:
            self.mode = self.MODE_SITE_LIST
            return
        tag = self._site_filtered[self._site_sel]
        entry = SITE_REGISTRY[tag]
        ov = _state.get('site_overrides', {}).get(tag, {})

        h = 11
        w = min(70, max(50, len(entry['label']) + 8))
        by1, bx1, by2, bx2 = self._box(stdscr, h, w)
        db.safe_addstr(stdscr, by1, bx1 + 2, " EDIT CALL SITE ",
                        curses.color_pair(db.C_INVHEAD) | curses.A_BOLD)

        db.safe_addstr(stdscr, by1 + 2, bx1 + 2, entry['label'][:w - 4],
                        curses.color_pair(db.C_WARN) | curses.A_BOLD)
        db.safe_addstr(stdscr, by1 + 3, bx1 + 2,
                        f"Tag: {tag}"[:w - 4], curses.color_pair(db.C_DIM))
        db.safe_addstr(stdscr, by1 + 4, bx1 + 2,
                        f"File: {entry['file']}"[:w - 4], curses.color_pair(db.C_DIM))

        if entry['default_role'] is not None:
            current_role = ov.get('role', entry['default_role'])
            role_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                         if self._site_edit_field == 'role' else curses.color_pair(db.C_NORMAL))
            db.safe_addstr(stdscr, by1 + 6, bx1 + 2,
                            f"Color role: {ROLE_LABELS.get(current_role, current_role)}",
                            role_attr)
        else:
            db.safe_addstr(stdscr, by1 + 6, bx1 + 2,
                            "Color role: (set at runtime, not overridable)",
                            curses.color_pair(db.C_DIM))

        bold_val = ov.get('bold', entry['default_bold'])
        bold_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                     if self._site_edit_field == 'bold' else curses.color_pair(db.C_NORMAL))
        db.safe_addstr(stdscr, by1 + 7, bx1 + 2,
                        f"Bold: {'ON' if bold_val else 'off'}", bold_attr)

        sample_role = ov.get('role', entry['default_role']) if entry['default_role'] else None
        if sample_role:
            sample_pair = ROLE_PAIR_NUM[sample_role]
            sample_attr = curses.color_pair(sample_pair)
            if bold_val:
                sample_attr |= curses.A_BOLD
            db.safe_addstr(stdscr, by1 + 9, bx1 + 2, "Sample: ", curses.color_pair(db.C_NORMAL))
            db.safe_addstr(stdscr, by1 + 9, bx1 + 10, " Aa 123 ", sample_attr)

        db.safe_addstr(stdscr, by2, bx1 + 2,
                        " Tab:Field  \u2190\u2192/Space:Change  r:Reset  Esc:Back ",
                        curses.color_pair(db.C_INVHEAD))
