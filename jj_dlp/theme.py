"""
theme.py — Per-call-site color/bold customization for the JJDlpDashboard TUI.

WHAT THIS DOES
───────────────
Every `curses.color_pair(...)` [`| curses.A_BOLD`] expression in main.py,
config_editor.py, and file_manager.py has been rewritten to go through
`theme.attr(owner, tag, default_pair_arg, default_bold)`. That function looks
up `tag` in the user's saved overrides (theme.json) and, if found, uses the
overridden pair/bold instead of the code's built-in default. If nothing is
overridden for that tag, behavior is 100% identical to the original inline
`curses.color_pair(...)` expression — this module is pure opt-in indirection.

Two independent customization layers, both persisted to theme.json:

  1. BASE SCHEME + ROLE COLORS — pick one of JJDlpDashboard.COLOR_SCHEMES as
     a starting point, then optionally recolor any of the 13 named roles
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

SITE_REGISTRY is auto-generated (see the scan/tag/rewrite pipeline used to
build this file) and should be treated as generated data — if call sites are
added, removed, or moved in the source files, re-run that pipeline rather
than hand-editing entries here.
"""

import curses
import json
import os
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
# SITE_REGISTRY — auto-generated. tag -> {file, line, label, default_role,
# default_bold}. default_role is None for sites whose default pair argument
# is a runtime variable or a raw numeric literal outside the 13-role system
# (mostly the splash/browser-picker screens) — those sites can still have
# their bold flag overridden, but not be repointed to a role by number.
# ─────────────────────────────────────────────────────────────────────────
SITE_REGISTRY = {
    'config_editor_priorityeditor_draw_live_1': {'file': 'config_editor.py', 'line': 531, 'label': 'Priority Editor — Title (STREAMER SETTINGS)', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_priorityeditor_draw_live_2': {'file': 'config_editor.py', 'line': 535, 'label': 'Priority Editor — Mode Indicator Badge', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_priorityeditor_draw_dim_1': {'file': 'config_editor.py', 'line': 546, 'label': 'Priority Editor — Hints List', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_priorityeditor_draw_dim_2': {'file': 'config_editor.py', 'line': 553, 'label': 'Priority Editor — \'No streamers.\' Message', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_priorityeditor_draw_hilight_1': {'file': 'config_editor.py', 'line': 577, 'label': 'Priority Editor — Bypass Streamer Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_priorityeditor_draw_live_3': {'file': 'config_editor.py', 'line': 579, 'label': 'Priority Editor — Bypass Streamer Row (Unselected)', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_priorityeditor_draw_hilight_2': {'file': 'config_editor.py', 'line': 581, 'label': 'Priority Editor — Normal Streamer Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_priorityeditor_draw_normal': {'file': 'config_editor.py', 'line': 583, 'label': 'Priority Editor — Normal Streamer Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_priorityeditor_draw_live_4': {'file': 'config_editor.py', 'line': 589, 'label': 'Priority Editor — Scroll-Up Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_priorityeditor_draw_live_5': {'file': 'config_editor.py', 'line': 591, 'label': 'Priority Editor — Scroll-Down Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_streamersettingspopu_draw_normal': {'file': 'config_editor.py', 'line': 726, 'label': 'Streamer Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_streamersettingspopu_draw_system': {'file': 'config_editor.py', 'line': 730, 'label': 'Streamer Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_streamersettingspopu_draw_hilight': {'file': 'config_editor.py', 'line': 736, 'label': 'Streamer Settings Popup — Menu Option (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_streamersettingspopu_draw_warn': {'file': 'config_editor.py', 'line': 736, 'label': 'Streamer Settings Popup — Menu Option (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_streamersettingspopu_draw_invhead': {'file': 'config_editor.py', 'line': 740, 'label': 'Streamer Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_qualitysettingspopup_draw_normal': {'file': 'config_editor.py', 'line': 821, 'label': 'Quality Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_qualitysettingspopup_draw_system': {'file': 'config_editor.py', 'line': 825, 'label': 'Quality Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_qualitysettingspopup_draw_hilight_1': {'file': 'config_editor.py', 'line': 828, 'label': 'Quality Settings Popup — \'Low Quality Enabled\' Label', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_qualitysettingspopup_draw_hilight_2': {'file': 'config_editor.py', 'line': 829, 'label': 'Quality Settings Popup — Checkbox Value', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_qualitysettingspopup_draw_invhead': {'file': 'config_editor.py', 'line': 831, 'label': 'Quality Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_notificationsettings_draw_normal_1': {'file': 'config_editor.py', 'line': 950, 'label': 'Notification Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_notificationsettings_draw_system': {'file': 'config_editor.py', 'line': 954, 'label': 'Notification Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_notificationsettings_draw_hilight_1': {'file': 'config_editor.py', 'line': 957, 'label': 'Notification Settings Popup — \'ntfy Notifications\' Label', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_notificationsettings_draw_hilight_2': {'file': 'config_editor.py', 'line': 958, 'label': 'Notification Settings Popup — State Badge (Inherit/On/Off)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_notificationsettings_draw_normal_2': {'file': 'config_editor.py', 'line': 966, 'label': 'Notification Settings Popup — \'Effective\' Label', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_notificationsettings_draw_warn': {'file': 'config_editor.py', 'line': 967, 'label': 'Notification Settings Popup — Effective Value (ON/OFF)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_notificationsettings_draw_invhead': {'file': 'config_editor.py', 'line': 969, 'label': 'Notification Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_splitsettingspopup_draw_normal_1': {'file': 'config_editor.py', 'line': 1198, 'label': 'Split Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_splitsettingspopup_draw_system': {'file': 'config_editor.py', 'line': 1202, 'label': 'Split Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_hilight_1': {'file': 'config_editor.py', 'line': 1208, 'label': 'Split Settings Popup — Field Label (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_warn_1': {'file': 'config_editor.py', 'line': 1209, 'label': 'Split Settings Popup — Field Label (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_hilight_2': {'file': 'config_editor.py', 'line': 1210, 'label': 'Split Settings Popup — Field Value (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_normal_2': {'file': 'config_editor.py', 'line': 1211, 'label': 'Split Settings Popup — Field Value (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_splitsettingspopup_draw_normal_3': {'file': 'config_editor.py', 'line': 1234, 'label': 'Split Settings Popup — \'Effective\' Label', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_splitsettingspopup_draw_warn_2': {'file': 'config_editor.py', 'line': 1235, 'label': 'Split Settings Popup — Effective Value', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_warn_3': {'file': 'config_editor.py', 'line': 1240, 'label': 'Split Settings Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_splitsettingspopup_draw_invhead': {'file': 'config_editor.py', 'line': 1243, 'label': 'Split Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_introdelaysettingspo_draw_normal_1': {'file': 'config_editor.py', 'line': 1449, 'label': 'Intro Delay Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_introdelaysettingspo_draw_system': {'file': 'config_editor.py', 'line': 1453, 'label': 'Intro Delay Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_hilight_1': {'file': 'config_editor.py', 'line': 1459, 'label': 'Intro Delay Settings Popup — Field Label (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_warn_1': {'file': 'config_editor.py', 'line': 1460, 'label': 'Intro Delay Settings Popup — Field Label (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_hilight_2': {'file': 'config_editor.py', 'line': 1461, 'label': 'Intro Delay Settings Popup — Field Value (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_normal_2': {'file': 'config_editor.py', 'line': 1462, 'label': 'Intro Delay Settings Popup — Field Value (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_introdelaysettingspo_draw_normal_3': {'file': 'config_editor.py', 'line': 1484, 'label': 'Intro Delay Settings Popup — \'Effective\' Label', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_introdelaysettingspo_draw_warn_2': {'file': 'config_editor.py', 'line': 1485, 'label': 'Intro Delay Settings Popup — Effective Value', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_warn_3': {'file': 'config_editor.py', 'line': 1490, 'label': 'Intro Delay Settings Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_introdelaysettingspo_draw_invhead': {'file': 'config_editor.py', 'line': 1493, 'label': 'Intro Delay Settings Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_schedulesettingspopu_draw_normal_1': {'file': 'config_editor.py', 'line': 1797, 'label': 'Schedule Settings Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_schedulesettingspopu_draw_system': {'file': 'config_editor.py', 'line': 1802, 'label': 'Schedule Settings Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_hilight_1': {'file': 'config_editor.py', 'line': 1811, 'label': 'Schedule Settings Popup — Field Label (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_warn_1': {'file': 'config_editor.py', 'line': 1812, 'label': 'Schedule Settings Popup — Field Label (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_hilight_2': {'file': 'config_editor.py', 'line': 1813, 'label': 'Schedule Settings Popup — Field Value (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_normal_2': {'file': 'config_editor.py', 'line': 1814, 'label': 'Schedule Settings Popup — Field Value (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_schedulesettingspopu_draw_hilight_3': {'file': 'config_editor.py', 'line': 1829, 'label': 'Schedule Settings Popup — Day-of-Week Token (Cursor)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_live': {'file': 'config_editor.py', 'line': 1831, 'label': 'Schedule Settings Popup — Day-of-Week Token (Active)', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_dim': {'file': 'config_editor.py', 'line': 1833, 'label': 'Schedule Settings Popup — Day-of-Week Token (Inactive)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_schedulesettingspopu_draw_normal_3': {'file': 'config_editor.py', 'line': 1842, 'label': 'Schedule Settings Popup — Time Edit Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_warn_2': {'file': 'config_editor.py', 'line': 1852, 'label': 'Schedule Settings Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_schedulesettingspopu_draw_invhead': {'file': 'config_editor.py', 'line': 1859, 'label': 'Schedule Settings Popup — Legend/Hint Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_sitesortmanager_draw_popup_normal_1': {'file': 'config_editor.py', 'line': 2021, 'label': 'Dashboard Sort Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_sitesortmanager_draw_popup_chrome': {'file': 'config_editor.py', 'line': 2025, 'label': 'Dashboard Sort Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'config_editor_sitesortmanager_draw_popup_invhead': {'file': 'config_editor.py', 'line': 2028, 'label': 'Dashboard Sort Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_sitesortmanager_draw_popup_hilight': {'file': 'config_editor.py', 'line': 2045, 'label': 'Dashboard Sort Popup — Selected Option', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_sitesortmanager_draw_popup_live': {'file': 'config_editor.py', 'line': 2047, 'label': 'Dashboard Sort Popup — Currently Active Option', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_sitesortmanager_draw_popup_normal_2': {'file': 'config_editor.py', 'line': 2049, 'label': 'Dashboard Sort Popup — Unselected Option', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_normal_1': {'file': 'config_editor.py', 'line': 2532, 'label': 'Destinations Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_system': {'file': 'config_editor.py', 'line': 2535, 'label': 'Destinations Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_destinations_po_dim_1': {'file': 'config_editor.py', 'line': 2540, 'label': 'Destinations Popup — Streamer Comment Text', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_dim_2': {'file': 'config_editor.py', 'line': 2544, 'label': 'Destinations Popup — \'Paths:\' Header', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_dim_3': {'file': 'config_editor.py', 'line': 2549, 'label': '(none yet)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_normal_2': {'file': 'config_editor.py', 'line': 2558, 'label': 'Destinations Popup — Path Row', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_destinations_po_warn': {'file': 'config_editor.py', 'line': 2563, 'label': 'Destinations Popup — \'New path:\' Label', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_destinations_po_normal_3': {'file': 'config_editor.py', 'line': 2566, 'label': 'Destinations Popup — New-Path Entry Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_destinations_po_invhead': {'file': 'config_editor.py', 'line': 2570, 'label': 'Destinations Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_normal_1': {'file': 'config_editor.py', 'line': 2590, 'label': 'Message Filters Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_system': {'file': 'config_editor.py', 'line': 2593, 'label': 'Message Filters Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_hilight_1': {'file': 'config_editor.py', 'line': 2602, 'label': 'Message Filters Popup — \'Tag Enabled\' Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_normal_2': {'file': 'config_editor.py', 'line': 2603, 'label': 'Message Filters Popup — \'Tag Enabled\' Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_hilight_2': {'file': 'config_editor.py', 'line': 2604, 'label': 'Message Filters Popup — Tag Badge (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_live_1': {'file': 'config_editor.py', 'line': 2606, 'label': 'Message Filters Popup — Tag Badge (Enabled)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_warn': {'file': 'config_editor.py', 'line': 2606, 'label': 'Message Filters Popup — Tag Badge (Disabled)', 'default_role': 'WARN', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_dim_1': {'file': 'config_editor.py', 'line': 2611, 'label': 'Message Filters Popup — \'Messages:\' Header', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_dim_2': {'file': 'config_editor.py', 'line': 2616, 'label': '(no dbg() calls found for this tag)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_hilight_3': {'file': 'config_editor.py', 'line': 2635, 'label': 'Message Filters Popup — Message Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_normal_3': {'file': 'config_editor.py', 'line': 2636, 'label': 'Message Filters Popup — Message Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_hilight_4': {'file': 'config_editor.py', 'line': 2637, 'label': 'Message Filters Popup — Message Badge (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_live_2': {'file': 'config_editor.py', 'line': 2639, 'label': 'Message Filters Popup — Message Badge (On)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_dim_3': {'file': 'config_editor.py', 'line': 2639, 'label': 'Message Filters Popup — Message Badge (Off)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_msg_filters_pop_invhead': {'file': 'config_editor.py', 'line': 2647, 'label': 'Space:Toggle  Enter:Save  Esc:Back', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_normal_1': {'file': 'config_editor.py', 'line': 2669, 'label': 'Debug Tags Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_system': {'file': 'config_editor.py', 'line': 2672, 'label': 'Debug Tags Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_hilight_1': {'file': 'config_editor.py', 'line': 2681, 'label': 'Debug Tags Popup — \'Enable Logging\' Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_normal_2': {'file': 'config_editor.py', 'line': 2682, 'label': 'Debug Tags Popup — \'Enable Logging\' Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_hilight_2': {'file': 'config_editor.py', 'line': 2683, 'label': 'Debug Tags Popup — Enable Logging Badge (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_live_1': {'file': 'config_editor.py', 'line': 2685, 'label': 'Debug Tags Popup — Enable Logging Badge (On)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_warn': {'file': 'config_editor.py', 'line': 2687, 'label': 'Debug Tags Popup — Enable Logging Badge (Off)', 'default_role': 'WARN', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_dim_1': {'file': 'config_editor.py', 'line': 2696, 'label': 'Tag Filters:', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_hilight_3': {'file': 'config_editor.py', 'line': 2717, 'label': 'Debug Tags Popup — Tag Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_normal_3': {'file': 'config_editor.py', 'line': 2718, 'label': 'Debug Tags Popup — Tag Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_hilight_4': {'file': 'config_editor.py', 'line': 2719, 'label': 'Debug Tags Popup — Tag Badge (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_live_2': {'file': 'config_editor.py', 'line': 2721, 'label': 'Debug Tags Popup — Tag Badge (On)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_dim_2': {'file': 'config_editor.py', 'line': 2723, 'label': 'Debug Tags Popup — Tag Badge (Off)', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_debug_tags_popu_invhead': {'file': 'config_editor.py', 'line': 2734, 'label': 'Space:Messages  Enter:Save  Esc:Cancel', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_live_1': {'file': 'config_editor.py', 'line': 2866, 'label': 'Global Settings Panel — Title', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_live_2': {'file': 'config_editor.py', 'line': 2870, 'label': 'Global Settings Panel — Mode Indicator Badge', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_hilight_1': {'file': 'config_editor.py', 'line': 2878, 'label': 'Global Settings Panel — Item Key (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_warn': {'file': 'config_editor.py', 'line': 2879, 'label': 'Global Settings Panel — Item Key (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_hilight_2': {'file': 'config_editor.py', 'line': 2880, 'label': 'Global Settings Panel — Item Value (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_live_3': {'file': 'config_editor.py', 'line': 2881, 'label': 'Global Settings Panel — Item Value (Unselected)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_live_4': {'file': 'config_editor.py', 'line': 2896, 'label': 'Global Settings Panel — Scroll-Up Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_live_5': {'file': 'config_editor.py', 'line': 2898, 'label': 'Global Settings Panel — Scroll-Down Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_normal_1': {'file': 'config_editor.py', 'line': 2931, 'label': 'Edit Global Value Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_popup_system_1': {'file': 'config_editor.py', 'line': 2934, 'label': 'Edit Global Value Popup — Title', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_chrome': {'file': 'config_editor.py', 'line': 2938, 'label': 'Edit Global Value Popup — \'Key:\' Line', 'default_role': 'CHROME', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_popup_dim': {'file': 'config_editor.py', 'line': 2942, 'label': 'Edit Global Value Popup — Comment Text', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_globalconfigeditor_draw_popup_system_2': {'file': 'config_editor.py', 'line': 2948, 'label': 'Edit Global Value Popup — \'New Value:\' Label', 'default_role': 'SYSTEM', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_normal_2': {'file': 'config_editor.py', 'line': 2950, 'label': 'Edit Global Value Popup — New-Value Entry Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_warn': {'file': 'config_editor.py', 'line': 2953, 'label': 'Edit Global Value Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_globalconfigeditor_draw_popup_invhead': {'file': 'config_editor.py', 'line': 2956, 'label': 'Enter: Save | Esc: Cancel #1', 'default_role': 'INVHEAD', 'default_bold': False},
    'config_editor_configeditor_draw_tab_dim_1': {'file': 'config_editor.py', 'line': 3134, 'label': 'Site:', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_configeditor_draw_tab_hilight_1': {'file': 'config_editor.py', 'line': 3139, 'label': 'Site Tab Bar — Selected Site Tab', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_configeditor_draw_tab_chrome': {'file': 'config_editor.py', 'line': 3141, 'label': 'Site Tab Bar — Unselected Site Tab', 'default_role': 'CHROME', 'default_bold': False},
    'config_editor_configeditor_draw_tab_dim_2': {'file': 'config_editor.py', 'line': 3145, 'label': '[: prev site  ]: next site  Tab: Next Panel', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_configeditor_draw_tab_live_1': {'file': 'config_editor.py', 'line': 3153, 'label': 'Site Settings Panel — Mode Indicator Badge', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_configeditor_draw_tab_live_2': {'file': 'config_editor.py', 'line': 3164, 'label': 'No configurable items found. #1', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_configeditor_draw_tab_dim_3': {'file': 'config_editor.py', 'line': 3169, 'label': 'No configurable items found. #2', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_configeditor_draw_tab_hilight_2': {'file': 'config_editor.py', 'line': 3178, 'label': 'Site Settings Panel — Item Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_configeditor_draw_tab_warn_1': {'file': 'config_editor.py', 'line': 3182, 'label': 'Site Settings Panel — Section Header Row (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_tab_normal': {'file': 'config_editor.py', 'line': 3183, 'label': 'Site Settings Panel — Regular Item Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_configeditor_draw_tab_hilight_3': {'file': 'config_editor.py', 'line': 3187, 'label': 'Site Settings Panel — Section Header (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'config_editor_configeditor_draw_tab_warn_2': {'file': 'config_editor.py', 'line': 3188, 'label': 'Site Settings Panel — Section Header (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_tab_warn_3': {'file': 'config_editor.py', 'line': 3192, 'label': 'Site Settings Panel — Item Key (Unselected)', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_tab_live_3': {'file': 'config_editor.py', 'line': 3194, 'label': 'Site Settings Panel — Item Value (Unselected)', 'default_role': 'LIVE', 'default_bold': False},
    'config_editor_configeditor_draw_tab_live_4': {'file': 'config_editor.py', 'line': 3210, 'label': 'Site Settings Panel — Scroll-Up Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_configeditor_draw_tab_live_5': {'file': 'config_editor.py', 'line': 3212, 'label': 'Site Settings Panel — Scroll-Down Arrow', 'default_role': 'LIVE', 'default_bold': True},
    'config_editor_configeditor_draw_popup_normal_1': {'file': 'config_editor.py', 'line': 3249, 'label': 'Edit Config Value Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'config_editor_configeditor_draw_popup_warn_1': {'file': 'config_editor.py', 'line': 3253, 'label': 'Edit Config Value Popup — Title', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_popup_chrome': {'file': 'config_editor.py', 'line': 3258, 'label': 'Edit Config Value Popup — \'Key:\' Line', 'default_role': 'CHROME', 'default_bold': False},
    'config_editor_configeditor_draw_popup_dim': {'file': 'config_editor.py', 'line': 3263, 'label': 'Edit Config Value Popup — Comment Text', 'default_role': 'DIM', 'default_bold': False},
    'config_editor_configeditor_draw_popup_warn_2': {'file': 'config_editor.py', 'line': 3269, 'label': 'Edit Config Value Popup — \'New Value:\' Label', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_popup_normal_2': {'file': 'config_editor.py', 'line': 3270, 'label': 'Edit Config Value Popup — New-Value Entry Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'config_editor_configeditor_draw_popup_warn_3': {'file': 'config_editor.py', 'line': 3273, 'label': 'Edit Config Value Popup — Error Message', 'default_role': 'WARN', 'default_bold': True},
    'config_editor_configeditor_draw_popup_invhead': {'file': 'config_editor.py', 'line': 3275, 'label': 'Enter: Save | Esc: Cancel #2', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_popup_normal_1': {'file': 'file_manager.py', 'line': 763, 'label': 'File Manager Sort Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_popup_chrome': {'file': 'file_manager.py', 'line': 767, 'label': 'File Manager Sort Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_popup_invhead': {'file': 'file_manager.py', 'line': 770, 'label': 'File Manager Sort Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_popup_hilight': {'file': 'file_manager.py', 'line': 786, 'label': 'File Manager Sort Popup — Selected Option', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_popup_live': {'file': 'file_manager.py', 'line': 788, 'label': 'File Manager Sort Popup — Currently Active Option', 'default_role': 'LIVE', 'default_bold': True},
    'file_manager_filemanagertab_draw_popup_normal_2': {'file': 'file_manager.py', 'line': 790, 'label': 'File Manager Sort Popup — Unselected Option', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_menu_popup_normal_1': {'file': 'file_manager.py', 'line': 858, 'label': 'File Options Menu Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_menu_popup_chrome': {'file': 'file_manager.py', 'line': 862, 'label': 'File Options Menu Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_menu_popup_invhead': {'file': 'file_manager.py', 'line': 865, 'label': 'File Options Menu Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_menu_popup_hilight': {'file': 'file_manager.py', 'line': 871, 'label': 'File Options Menu Popup — Menu Option (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_menu_popup_normal_2': {'file': 'file_manager.py', 'line': 872, 'label': 'File Options Menu Popup — Menu Option (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_fixup_popup_normal_1': {'file': 'file_manager.py', 'line': 924, 'label': 'Fixup Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_fixup_popup_chrome': {'file': 'file_manager.py', 'line': 928, 'label': 'Fixup Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_fixup_popup_invhead': {'file': 'file_manager.py', 'line': 931, 'label': 'Fixup Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_fixup_popup_dim': {'file': 'file_manager.py', 'line': 936, 'label': 'Fixup Popup — Target Filename', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_fixup_popup_hilight': {'file': 'file_manager.py', 'line': 944, 'label': 'Fixup Popup — Checkbox Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_fixup_popup_normal_2': {'file': 'file_manager.py', 'line': 945, 'label': 'Fixup Popup — Checkbox Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_normal_1': {'file': 'file_manager.py', 'line': 1040, 'label': 'Move Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_chrome': {'file': 'file_manager.py', 'line': 1043, 'label': 'Move Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_popup_invhead': {'file': 'file_manager.py', 'line': 1046, 'label': 'Move Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_dim': {'file': 'file_manager.py', 'line': 1050, 'label': 'Move Popup — \'Select a destination:\' Header', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_hilight_1': {'file': 'file_manager.py', 'line': 1057, 'label': 'Move File Popup — Selected Destination', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_popup_normal_2': {'file': 'file_manager.py', 'line': 1058, 'label': 'Move File Popup — Unselected Destination', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_hilight_2': {'file': 'file_manager.py', 'line': 1065, 'label': 'Move Popup — \'Configure New Destination\' (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_popup_system': {'file': 'file_manager.py', 'line': 1066, 'label': 'Move Popup — \'Configure New Destination\' (Unselected)', 'default_role': 'SYSTEM', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_popup_hilight_3': {'file': 'file_manager.py', 'line': 1077, 'label': 'Move Popup — Checkbox Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_popup_normal_3': {'file': 'file_manager.py', 'line': 1078, 'label': 'Move Popup — Checkbox Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_filename_p_normal_1': {'file': 'file_manager.py', 'line': 1150, 'label': 'Rename/Move Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_filename_p_chrome': {'file': 'file_manager.py', 'line': 1153, 'label': 'Rename/Move Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_filename_p_invhead': {'file': 'file_manager.py', 'line': 1156, 'label': 'Rename/Move Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_filename_p_dim': {'file': 'file_manager.py', 'line': 1161, 'label': 'Streamer: {self._move_filename_streamer}', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_move_filename_p_warn': {'file': 'file_manager.py', 'line': 1164, 'label': 'Filename:', 'default_role': 'WARN', 'default_bold': True},
    'file_manager_filemanagertab_draw_move_filename_p_normal_2': {'file': 'file_manager.py', 'line': 1171, 'label': 'Rename/Move Popup — Filename Entry Buffer', 'default_role': 'NORMAL', 'default_bold': True},
    'file_manager_filemanagertab_draw_trim_popup_normal_1': {'file': 'file_manager.py', 'line': 1575, 'label': 'Trim Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_trim_popup_chrome': {'file': 'file_manager.py', 'line': 1579, 'label': 'Trim Popup — Title', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_trim_popup_invhead': {'file': 'file_manager.py', 'line': 1582, 'label': 'Trim Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'file_manager_filemanagertab_draw_trim_popup_dim': {'file': 'file_manager.py', 'line': 1587, 'label': 'Trim Popup — Target Filename', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_trim_popup_hilight_1': {'file': 'file_manager.py', 'line': 1597, 'label': 'Trim Popup — Start/End Field (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_trim_popup_normal_2': {'file': 'file_manager.py', 'line': 1598, 'label': 'Trim Popup — Unselected Field', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_trim_popup_hilight_2': {'file': 'file_manager.py', 'line': 1619, 'label': 'Trim Popup — Checkbox Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_trim_popup_normal_3': {'file': 'file_manager.py', 'line': 1620, 'label': 'Trim Popup — Checkbox Row (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'file_manager_filemanagertab_draw_chrome': {'file': 'file_manager.py', 'line': 1713, 'label': 'FILE MANAGER', 'default_role': 'CHROME', 'default_bold': True},
    'file_manager_filemanagertab_draw_dim_1': {'file': 'file_manager.py', 'line': 1719, 'label': 'No OUTPUT_DIR configured on any site.', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_normal': {'file': 'file_manager.py', 'line': 1736, 'label': 'File Manager — Column Header Row', 'default_role': 'NORMAL', 'default_bold': True},
    'file_manager_filemanagertab_draw_system_1': {'file': 'file_manager.py', 'line': 1773, 'label': 'File Manager — Subfolder Group Header', 'default_role': 'SYSTEM', 'default_bold': True},
    'file_manager_filemanagertab_draw_dim_2': {'file': 'file_manager.py', 'line': 1776, 'label': 'File Manager — \'Empty\' Placeholder Row', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_hilight': {'file': 'file_manager.py', 'line': 1796, 'label': 'File Manager — File Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'file_manager_filemanagertab_draw_live': {'file': 'file_manager.py', 'line': 1798, 'label': 'File Manager — File Row (Writing)', 'default_role': 'LIVE', 'default_bold': True},
    'file_manager_filemanagertab_draw_dim_3': {'file': 'file_manager.py', 'line': 1800, 'label': 'File Manager — File Row (Idle)', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_system_2': {'file': 'file_manager.py', 'line': 1817, 'label': 'File Manager — Subfolder Path Prefix', 'default_role': 'SYSTEM', 'default_bold': False},
    'file_manager_filemanagertab_draw_delete': {'file': 'file_manager.py', 'line': 1830, 'label': 'File Manager — Delete-Mode Info (Permanent)', 'default_role': 'DELETE', 'default_bold': True},
    'file_manager_filemanagertab_draw_dim_4': {'file': 'file_manager.py', 'line': 1830, 'label': 'File Manager — Delete-Mode Info (Trash)', 'default_role': 'DIM', 'default_bold': False},
    'file_manager_filemanagertab_draw_warn': {'file': 'file_manager.py', 'line': 1835, 'label': 'File Manager — Status Message Line', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_safe_ch_pair': {'file': 'main.py', 'line': 4318, 'label': 'Box Border (generic, all panels)', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_draw_logo_logo': {'file': 'main.py', 'line': 4559, 'label': 'Main Logo Banner', 'default_role': 'LOGO', 'default_bold': True},
    'main_jjdlpdashboard_draw_christmas_easte_pair': {'file': 'main.py', 'line': 4589, 'label': 'Christmas Easter Egg — Tree', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_draw_christmas_easte_live': {'file': 'main.py', 'line': 4592, 'label': 'Christmas Easter Egg — \'Merry Christmas!\' Text', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_tabs_hilight': {'file': 'main.py', 'line': 4601, 'label': 'Tab Bar — Selected Tab', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_tabs_invhead': {'file': 'main.py', 'line': 4603, 'label': 'Tab Bar — Unselected Tab', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_system_panel_system': {'file': 'main.py', 'line': 4614, 'label': 'SYSTEM', 'default_role': 'SYSTEM', 'default_bold': True},
    'main_jjdlpdashboard_split_after_rows_dim': {'file': 'main.py', 'line': 4710, 'label': 'System Panel — Stat Row Label', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_cpair': {'file': 'main.py', 'line': 4713, 'label': 'System Panel — Stat Row Value (color varies by stat)', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_split_after_rows_rec_1': {'file': 'main.py', 'line': 4734, 'label': 'System Panel — \'ffmpeg errors\' Section Header', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_2': {'file': 'main.py', 'line': 4743, 'label': 'System Panel — ffmpeg Error Streamer Name', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_3': {'file': 'main.py', 'line': 4746, 'label': 'System Panel — ffmpeg Error Count', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_4': {'file': 'main.py', 'line': 4768, 'label': 'System Panel — \'stalled\' Section Header', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_5': {'file': 'main.py', 'line': 4777, 'label': 'System Panel — Stalled Streamer Name', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_rec_6': {'file': 'main.py', 'line': 4780, 'label': 'System Panel — Stalled Duration', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_split_after_rows_warn_1': {'file': 'main.py', 'line': 4799, 'label': 'System Panel — \'ads\' Section Header', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_split_after_rows_warn_2': {'file': 'main.py', 'line': 4805, 'label': 'Ad detected', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_update_disk_usage_system': {'file': 'main.py', 'line': 4893, 'label': 'System Panel — \'Disk\' Section Header', 'default_role': 'SYSTEM', 'default_bold': False},
    'main_jjdlpdashboard_update_disk_usage_color': {'file': 'main.py', 'line': 4909, 'label': 'System Panel — Per-Drive Usage Line', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_update_disk_usage_chrome': {'file': 'main.py', 'line': 4917, 'label': 'System Panel — Uptime Line', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_chrome_1': {'file': 'main.py', 'line': 4975, 'label': '{cfg_label}', 'default_role': 'CHROME', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_1': {'file': 'main.py', 'line': 4981, 'label': 'LIVE:{live_cnt}', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_rec_1': {'file': 'main.py', 'line': 4984, 'label': 'REC:{rec_cnt}', 'default_role': 'REC', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_1': {'file': 'main.py', 'line': 4987, 'label': 'OFF:{off_cnt}', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_1': {'file': 'main.py', 'line': 4991, 'label': 'DIS:{dis_cnt}', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_chrome_2': {'file': 'main.py', 'line': 5020, 'label': 'Site Panel (Compact) — Column Separator', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_2': {'file': 'main.py', 'line': 5050, 'label': 'Site Panel (Compact) — Name (Disabled)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_3': {'file': 'main.py', 'line': 5054, 'label': 'Site Panel (Compact) — Status Badge (Disabled, Flash On)', 'default_role': 'DISABLED', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_disabled_4': {'file': 'main.py', 'line': 5057, 'label': 'Site Panel (Compact) — Status Badge (Disabled, Flash Off)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_5': {'file': 'main.py', 'line': 5060, 'label': 'Site Panel (Compact) — Status Badge (Disabled, Never Live)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_live_2': {'file': 'main.py', 'line': 5062, 'label': 'Site Panel (Compact) — Name (Live)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_3': {'file': 'main.py', 'line': 5066, 'label': 'Site Panel (Compact) — Status Badge (Recording, Flash On)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_rec_2': {'file': 'main.py', 'line': 5069, 'label': 'Site Panel (Compact) — Status Badge (Recording, Flash Off)', 'default_role': 'REC', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_4': {'file': 'main.py', 'line': 5072, 'label': 'Site Panel (Compact) — Status Badge (Live, Not Recording)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_2': {'file': 'main.py', 'line': 5076, 'label': 'Site Panel (Compact) — Name (Offline)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_dim_3': {'file': 'main.py', 'line': 5078, 'label': 'Site Panel (Compact) — Status Badge (Offline)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_live_5': {'file': 'main.py', 'line': 5091, 'label': 'Site Panel (Compact) — Last Live, Recently Live', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_4': {'file': 'main.py', 'line': 5093, 'label': 'Site Panel (Compact) — Last Live, Older', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_6': {'file': 'main.py', 'line': 5134, 'label': 'Site Panel (Normal) — Name (Disabled)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_7': {'file': 'main.py', 'line': 5136, 'label': 'Site Panel (Normal) — Progress Bar (Disabled)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_8': {'file': 'main.py', 'line': 5141, 'label': 'Site Panel (Normal) — Status Badge (Disabled, Flash On)', 'default_role': 'DISABLED', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_disabled_9': {'file': 'main.py', 'line': 5144, 'label': 'Site Panel (Normal) — Status Badge (Disabled, Flash Off)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_disabled_10': {'file': 'main.py', 'line': 5147, 'label': 'Site Panel (Normal) — Status Badge (Disabled, Never Live)', 'default_role': 'DISABLED', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_live_6': {'file': 'main.py', 'line': 5150, 'label': 'Site Panel (Normal) — Name (Live)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_7': {'file': 'main.py', 'line': 5154, 'label': 'Site Panel (Normal) — Status Badge (Recording, Flash On)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_rec_3': {'file': 'main.py', 'line': 5157, 'label': 'Site Panel (Normal) — Status Badge (Recording, Flash Off)', 'default_role': 'REC', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_8': {'file': 'main.py', 'line': 5160, 'label': 'Site Panel (Normal) — Status Badge (Live, Not Recording)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_live_9': {'file': 'main.py', 'line': 5162, 'label': 'Site Panel (Normal) — Progress Bar (Live)', 'default_role': 'LIVE', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_dim_5': {'file': 'main.py', 'line': 5167, 'label': 'Site Panel (Normal) — Name (Offline)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_dim_6': {'file': 'main.py', 'line': 5169, 'label': 'Site Panel (Normal) — Status Badge (Offline)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_dim_7': {'file': 'main.py', 'line': 5171, 'label': 'Site Panel (Normal) — Progress Bar (Offline)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_chrome_3': {'file': 'main.py', 'line': 5185, 'label': 'Site Panel (Normal) — Duration Column', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_live_10': {'file': 'main.py', 'line': 5193, 'label': 'Site Panel (Normal) — Last Live, Recently Live', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_site_panel_dim_8': {'file': 'main.py', 'line': 5195, 'label': 'Site Panel (Normal) — Last Live, Older', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_site_panel_warn': {'file': 'main.py', 'line': 5215, 'label': 'Next check: {_nxt_str}', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_dim_1': {'file': 'main.py', 'line': 5322, 'label': 'Activity Log — \'Site:\' Label', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_log_tab_hilight': {'file': 'main.py', 'line': 5328, 'label': 'Activity Log — Site Tab (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_chrome': {'file': 'main.py', 'line': 5330, 'label': 'Activity Log — Site Tab (Unselected)', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_log_tab_dim_2': {'file': 'main.py', 'line': 5341, 'label': 'Activity Log — Title', 'default_role': 'DIM', 'default_bold': True},
    'main_jjdlpdashboard_draw_log_tab_dim_3': {'file': 'main.py', 'line': 5372, 'label': 'Activity Log — Line (Normal)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_log_tab_live': {'file': 'main.py', 'line': 5374, 'label': 'Activity Log — Line (Live/Recording Started)', 'default_role': 'LIVE', 'default_bold': False},
    'main_jjdlpdashboard_draw_log_tab_rec': {'file': 'main.py', 'line': 5376, 'label': 'Activity Log — Line (Error/Stall/Stopped)', 'default_role': 'REC', 'default_bold': False},
    'main_jjdlpdashboard_draw_log_tab_warn_1': {'file': 'main.py', 'line': 5378, 'label': 'Activity Log — Line (Warning)', 'default_role': 'WARN', 'default_bold': False},
    'main_jjdlpdashboard_draw_log_tab_warn_2': {'file': 'main.py', 'line': 5385, 'label': '↑{self._log_scroll}/{max_scroll}', 'default_role': 'WARN', 'default_bold': False},
    'main_jjdlpdashboard_draw_pipe_tab_bar_dim': {'file': 'main.py', 'line': 5392, 'label': 'Stdout/Stderr Tabs — \'Site:\' Label', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_pipe_tab_bar_hilight': {'file': 'main.py', 'line': 5398, 'label': 'Stdout/Stderr Tabs — Site Tab (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_pipe_tab_bar_chrome': {'file': 'main.py', 'line': 5400, 'label': 'Stdout/Stderr Tabs — Site Tab (Unselected)', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_streamer_panel_border_pair': {'file': 'main.py', 'line': 5414, 'label': 'STREAMERS', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_draw_streamer_panel_hilight': {'file': 'main.py', 'line': 5439, 'label': 'Streamer Sub-Tab List — Selected Row', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_streamer_panel_dim': {'file': 'main.py', 'line': 5442, 'label': 'Streamer Sub-Tab List — Unselected Row', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_pipe_tab_border_pair': {'file': 'main.py', 'line': 5455, 'label': '{title}{title_suffix}', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_draw_pipe_tab_dim': {'file': 'main.py', 'line': 5472, 'label': 'Stdout/Stderr Panel — Content Line', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_pipe_tab_warn': {'file': 'main.py', 'line': 5478, 'label': 'Stdout/Stderr Panel — Scroll Indicator', 'default_role': 'WARN', 'default_bold': False},
    'main_jjdlpdashboard_draw_eventsub_tab_invhead_1': {'file': 'main.py', 'line': 5573, 'label': 'TWITCH EVENTSUB', 'default_role': 'INVHEAD', 'default_bold': True},
    'main_jjdlpdashboard_draw_eventsub_tab_warn': {'file': 'main.py', 'line': 5582, 'label': 'EventSub Tab — Site Header (\'-- {label} --\')', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_eventsub_tab_dim': {'file': 'main.py', 'line': 5588, 'label': 'EventSub not available', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_eventsub_tab_invhead_2': {'file': 'main.py', 'line': 5616, 'label': 'EventSub Tab — Stat Row Label', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_eventsub_tab_cpair': {'file': 'main.py', 'line': 5617, 'label': 'EventSub Tab — Stat Row Value (color varies by stat)', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_draw_footer_invhead': {'file': 'main.py', 'line': 5702, 'label': 'Bottom Footer / Key Legend Bar', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_normal_1': {'file': 'main.py', 'line': 5737, 'label': 'Add/Remove/Disable Overlay — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_warn_1': {'file': 'main.py', 'line': 5742, 'label': 'Add/Remove/Disable Overlay — Title', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_dim_1': {'file': 'main.py', 'line': 5744, 'label': 'Site: {site_lbl}', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_live_1': {'file': 'main.py', 'line': 5754, 'label': 'Add/Remove/Disable Overlay — Result Message (Disable/Remove)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_dim_2': {'file': 'main.py', 'line': 5759, 'label': 'No enabled streamers. #2', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_invhead_1': {'file': 'main.py', 'line': 5762, 'label': 'Add/Remove/Disable Overlay — Legend (Empty List)', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_hilight_1': {'file': 'main.py', 'line': 5784, 'label': 'Add/Remove/Disable Overlay — Selected Streamer', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_normal_2': {'file': 'main.py', 'line': 5785, 'label': 'Add/Remove/Disable Overlay — Unselected Streamer', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_invhead_2': {'file': 'main.py', 'line': 5791, 'label': 'Add/Remove/Disable Overlay — Legend (List Picker)', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_live_2': {'file': 'main.py', 'line': 5801, 'label': 'Add/Remove/Disable Overlay — Result Message (Add)', 'default_role': 'LIVE', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_chrome': {'file': 'main.py', 'line': 5820, 'label': 'Add/Remove/Disable Overlay — \'Re-enable disabled:\' Header', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_hilight_2': {'file': 'main.py', 'line': 5838, 'label': 'Add/Remove/Disable Overlay — Disabled Streamer Row (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_dim_3': {'file': 'main.py', 'line': 5839, 'label': 'Add/Remove/Disable Overlay — Disabled Streamer Row (Unselected)', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_dim_4': {'file': 'main.py', 'line': 5845, 'label': 'Add/Remove/Disable Overlay — \'No disabled streamers.\' Message', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_draw_mgmt_overlay_warn_2': {'file': 'main.py', 'line': 5849, 'label': 'Add/Remove/Disable Overlay — \'New username:\' Label', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_hilight_3': {'file': 'main.py', 'line': 5850, 'label': 'Add/Remove/Disable Overlay — Username Input (Focused)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_normal_3': {'file': 'main.py', 'line': 5852, 'label': 'Add/Remove/Disable Overlay — Username Input (Unfocused)', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_mgmt_overlay_invhead_3': {'file': 'main.py', 'line': 5862, 'label': 'Add/Remove/Disable Overlay — Legend (Add Mode)', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_refresh_screen_normal': {'file': 'main.py', 'line': 5868, 'label': 'Full-Screen Background Color', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_refresh_screen_chrome_1': {'file': 'main.py', 'line': 5885, 'label': 'Top-Right System Clock', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_refresh_screen_warn': {'file': 'main.py', 'line': 5895, 'label': 'Update Available', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_refresh_screen_dim': {'file': 'main.py', 'line': 5901, 'label': 'v{__version__}', 'default_role': 'DIM', 'default_bold': False},
    'main_jjdlpdashboard_refresh_screen_chrome_2': {'file': 'main.py', 'line': 5908, 'label': 'Separator', 'default_role': 'CHROME', 'default_bold': False},
    'main_jjdlpdashboard_draw_write_failure_a_delete': {'file': 'main.py', 'line': 6375, 'label': 'Recording Failure Alert — Box, Title, Message, Names', 'default_role': 'DELETE', 'default_bold': True},
    'main_jjdlpdashboard_draw_write_failure_a_invhead': {'file': 'main.py', 'line': 6401, 'label': 'Recording Failure Alert — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_exit_confirm_po_normal_1': {'file': 'main.py', 'line': 6446, 'label': 'Exit Confirm Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_exit_confirm_po_warn': {'file': 'main.py', 'line': 6451, 'label': 'Exit Confirm Popup — Title', 'default_role': 'WARN', 'default_bold': True},
    'main_jjdlpdashboard_draw_exit_confirm_po_normal_2': {'file': 'main.py', 'line': 6454, 'label': 'Exit Confirm Popup — Message Text', 'default_role': 'NORMAL', 'default_bold': True},
    'main_jjdlpdashboard_draw_exit_confirm_po_hilight_1': {'file': 'main.py', 'line': 6461, 'label': 'Exit Confirm Popup — \'Yes\' Button (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_exit_confirm_po_normal_3': {'file': 'main.py', 'line': 6462, 'label': 'Exit Confirm Popup — \'Yes\' Button (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_exit_confirm_po_hilight_2': {'file': 'main.py', 'line': 6463, 'label': 'Exit Confirm Popup — \'No\' Button (Selected)', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_exit_confirm_po_normal_4': {'file': 'main.py', 'line': 6464, 'label': 'Exit Confirm Popup — \'No\' Button (Unselected)', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_exit_confirm_po_invhead': {'file': 'main.py', 'line': 6470, 'label': 'Exit Confirm Popup — Legend Line', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_draw_changelog_popup_normal_1': {'file': 'main.py', 'line': 6488, 'label': 'Changelog Popup — Background Fill', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_changelog_popup_hilight': {'file': 'main.py', 'line': 6493, 'label': 'Changelog Popup — Title', 'default_role': 'HILIGHT', 'default_bold': True},
    'main_jjdlpdashboard_draw_changelog_popup_normal_2': {'file': 'main.py', 'line': 6507, 'label': 'Changelog Popup — Content Line', 'default_role': 'NORMAL', 'default_bold': False},
    'main_jjdlpdashboard_draw_changelog_popup_invhead': {'file': 'main.py', 'line': 6518, 'label': 'Changelog Popup — Scroll Indicator / Legend', 'default_role': 'INVHEAD', 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum0': {'file': 'main.py', 'line': 6614, 'label': 'Config Picker Splash — Full-Screen Background', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum6': {'file': 'main.py', 'line': 6618, 'label': 'Config Picker Splash — Logo', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum1_1': {'file': 'main.py', 'line': 6621, 'label': 'Config Picker Splash — System Clock', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum1_2': {'file': 'main.py', 'line': 6622, 'label': 'Config Picker Splash — Separator Line', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum5_1': {'file': 'main.py', 'line': 6626, 'label': 'Config Picker Splash — Title', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum3_1': {'file': 'main.py', 'line': 6631, 'label': 'Config Picker Splash — Instructions Line', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum2': {'file': 'main.py', 'line': 6639, 'label': 'Config Picker Splash — File Row (Cursor)', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum4': {'file': 'main.py', 'line': 6641, 'label': 'Config Picker Splash — File Row (Checked)', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum1_3': {'file': 'main.py', 'line': 6643, 'label': 'Config Picker Splash — File Row (Unchecked)', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum3_2': {'file': 'main.py', 'line': 6649, 'label': 'Config Picker Splash — \'Do Not Show Again\' (Checked)', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_config_pairnum3_3': {'file': 'main.py', 'line': 6649, 'label': 'Config Picker Splash — \'Do Not Show Again\' (Unchecked)', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_config_pairnum5_2': {'file': 'main.py', 'line': 6660, 'label': 'Config Picker Splash — Footer', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum0': {'file': 'main.py', 'line': 6736, 'label': 'Browser Picker Splash — Full-Screen Background', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum6': {'file': 'main.py', 'line': 6740, 'label': 'Browser Picker Splash — Logo', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum1_1': {'file': 'main.py', 'line': 6743, 'label': 'Browser Picker Splash — System Clock', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum1_2': {'file': 'main.py', 'line': 6744, 'label': 'Browser Picker Splash — Separator Line', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum5_1': {'file': 'main.py', 'line': 6750, 'label': 'Browser Picker Splash — Title', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum3_1': {'file': 'main.py', 'line': 6753, 'label': 'Browser Picker Splash — Instructions Line', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum3_2': {'file': 'main.py', 'line': 6756, 'label': 'Browser Picker Splash — Chrome-Unsupported Warning', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum4': {'file': 'main.py', 'line': 6764, 'label': 'Browser Picker Splash — \'Applies to:\' Line', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum2': {'file': 'main.py', 'line': 6773, 'label': 'Browser Picker Splash — Browser Row (Cursor)', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum1_3': {'file': 'main.py', 'line': 6775, 'label': 'Browser Picker Splash — Browser Row (Not Cursor)', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum3_3': {'file': 'main.py', 'line': 6782, 'label': 'Browser Picker Splash — \'Do Not Show Again\' (Checked)', 'default_role': None, 'default_bold': True},
    'main_jjdlpdashboard_curses_choose_browse_pairnum3_4': {'file': 'main.py', 'line': 6782, 'label': 'Browser Picker Splash — \'Do Not Show Again\' (Unchecked)', 'default_role': None, 'default_bold': False},
    'main_jjdlpdashboard_curses_choose_browse_pairnum5_2': {'file': 'main.py', 'line': 6791, 'label': 'Browser Picker Splash — Footer', 'default_role': None, 'default_bold': True},
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
    default = {'base_scheme_idx': 0, 'role_overrides': {}, 'site_overrides': {}}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.loads(f.read())
        if not isinstance(data, dict):
            return default
        data.setdefault('base_scheme_idx', 0)
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
def attr(owner, tag, default_pair_arg, default_bold):
    """Return the curses attribute (color pair | optional bold) to use for a
    given call site.

    owner:             the object whose C_* constants `default_pair_arg` may
                        reference (self / db / self.dashboard / the
                        JJDlpDashboard class / None). Only used to resolve a
                        *role name* override back into a pair number when the
                        owner exposes C_* attributes; unused otherwise.
    tag:                the stable site identifier (see SITE_REGISTRY).
    default_pair_arg:   the value the original code passed to
                         curses.color_pair(...) at this site — either a role
                         pair number (self.C_X) or a runtime variable/literal.
    default_bold:       True if the original code included "| curses.A_BOLD".
    """
    overrides = _state.get('site_overrides', {})
    ov = overrides.get(tag)

    if ov is None:
        pair_num = default_pair_arg
        bold = default_bold
    else:
        role = ov.get('role')
        if role and owner is not None:
            pair_num = getattr(owner, f'C_{role}', default_pair_arg)
        else:
            pair_num = default_pair_arg
        bold = ov.get('bold', default_bold)

    result = curses.color_pair(pair_num)
    if bold:
        result |= curses.A_BOLD
    return result


def role_overrides_for(scheme_idx=None):
    """Return the role-override dict ({role: {'fg': name, 'bg': name}}) for
    the given scheme index, defaulting to the active base scheme. The dict is
    created on demand, so callers can mutate it directly."""
    idx = str(_state.get('base_scheme_idx', 0) if scheme_idx is None else scheme_idx)
    return _state.setdefault('role_overrides', {}).setdefault(idx, {})


# ─────────────────────────────────────────────────────────────────────────
# Applying role fg/bg overrides on top of a base COLOR_SCHEMES tuple.
# ─────────────────────────────────────────────────────────────────────────
def resolve_scheme_values(dashboard):
    """Return a dict {role: {'fg': curses.COLOR_*, 'bg': curses.COLOR_* or
    None}} for all 13 roles, combining the active base scheme with any saved
    role_overrides for that scheme. bg is None for roles that share the
    ambient background."""
    scheme = dashboard.COLOR_SCHEMES[_state.get('base_scheme_idx', 0) % len(dashboard.COLOR_SCHEMES)]
    values = {}
    for role in ROLE_ORDER:
        values[role] = {'fg': None, 'bg': None}
    for (role, field), value in zip(SCHEME_TUPLE_FIELDS, scheme):
        values[role][field] = value

    for role, ov in role_overrides_for().items():
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


def apply_palette(dashboard):
    """Re-initialize all 13 curses pairs from the active base scheme + role
    overrides. Call this instead of dashboard._apply_color_scheme() once
    theme.py owns palette application; safe to call at any time (e.g. right
    after loading/saving theme.json, or when the base scheme changes)."""
    values = resolve_scheme_values(dashboard)
    ambient_bg = dashboard._SCHEME_BACKGROUND.get(
        _state.get('base_scheme_idx', 0), curses.COLOR_BLACK)

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


# ─────────────────────────────────────────────────────────────────────────
# ThemeManager — owns the theme popup (base scheme picker, role color editor,
# and per-site browser), bound to the repurposed 'c' key.
# ─────────────────────────────────────────────────────────────────────────
class ThemeManager:
    """Popup UI for customizing themes. Mirrors the shape of
    config_editor.SiteSortManager: owns its own open/close state, handles
    keys while open, draws itself on top of everything else."""

    MODE_MAIN = 'main'                # top-level menu: scheme / roles / sites
    MODE_SCHEME_SELECT = 'scheme'     # pick a base COLOR_SCHEMES entry
    MODE_ROLE_LIST = 'role_list'      # list of 13 roles
    MODE_ROLE_EDIT = 'role_edit'      # editing one role's fg/bg
    MODE_SITE_LIST = 'site_list'      # browsable/searchable list of 337 sites
    MODE_SITE_EDIT = 'site_edit'      # editing one site's role + bold

    SCHEME_NAMES = [
        "Default (cyan/blue/green/magenta)", "Amber terminal", "Green phosphor",
        "Red alert", "Magenta/purple", "Ice blue", "DOS Blue", "DOS Red", "DOS White",
    ]

    def __init__(self, dashboard):
        self.dashboard = dashboard
        self.popup_open = False
        self.mode = self.MODE_MAIN
        self._main_sel = 0

        self._scheme_sel = _state.get('base_scheme_idx', 0)

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
                self._scheme_sel = _state.get('base_scheme_idx', 0)
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
        n = len(self.dashboard.COLOR_SCHEMES)
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
                      key=lambda t: (SITE_REGISTRY[t]['file'], SITE_REGISTRY[t]['line']))
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
        elif key in (ord('x'), ord('X')):
            # Clear override for the currently-selected site.
            if n:
                tag = self._site_filtered[self._site_sel]
                _state.get('site_overrides', {}).pop(tag, None)
                save_theme(_state)
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
        db.safe_addstr(stdscr, by2, bx1 + 2, " \u2191\u2193:Move  Enter:Select  Esc:Close ",
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
            is_cur = (i == _state.get('base_scheme_idx', 0))
            if i == self._scheme_sel:
                attr_ = curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
            elif is_cur:
                attr_ = curses.color_pair(db.C_LIVE) | curses.A_BOLD
            else:
                attr_ = curses.color_pair(db.C_NORMAL)
            prefix = "> " if i == self._scheme_sel else ("* " if is_cur else "  ")
            db.safe_addstr(stdscr, by1 + 2 + i, bx1 + 2, (prefix + name)[:w - 4], attr_)
        db.safe_addstr(stdscr, by2, bx1 + 2, " \u2191\u2193:Move  Enter:Apply  Esc:Back ",
                        curses.color_pair(db.C_INVHEAD))

    def _draw_role_list(self, stdscr):
        db = self.dashboard
        n = len(ROLE_ORDER)
        h = n + 4
        w = 42
        by1, bx1, by2, bx2 = self._box(stdscr, h, w)
        scheme_idx = _state.get('base_scheme_idx', 0) % len(self.SCHEME_NAMES)
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
                        " Apply to active scheme.  r:Reset ",
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
                        " Enter:Edit  x:Clear  Type:Filter  Esc:Back ",
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

        h = 10
        w = min(70, max(50, len(entry['label']) + 8))
        by1, bx1, by2, bx2 = self._box(stdscr, h, w)
        db.safe_addstr(stdscr, by1, bx1 + 2, " EDIT CALL SITE ",
                        curses.color_pair(db.C_INVHEAD) | curses.A_BOLD)

        db.safe_addstr(stdscr, by1 + 2, bx1 + 2, entry['label'][:w - 4],
                        curses.color_pair(db.C_WARN) | curses.A_BOLD)
        db.safe_addstr(stdscr, by1 + 3, bx1 + 2,
                        f"{entry['file']}:{entry['line']}", curses.color_pair(db.C_DIM))

        if entry['default_role'] is not None:
            current_role = ov.get('role', entry['default_role'])
            role_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                         if self._site_edit_field == 'role' else curses.color_pair(db.C_NORMAL))
            db.safe_addstr(stdscr, by1 + 5, bx1 + 2,
                            f"Color role: {ROLE_LABELS.get(current_role, current_role)}",
                            role_attr)
        else:
            db.safe_addstr(stdscr, by1 + 5, bx1 + 2,
                            "Color role: (set at runtime, not overridable)",
                            curses.color_pair(db.C_DIM))

        bold_val = ov.get('bold', entry['default_bold'])
        bold_attr = (curses.color_pair(db.C_HILIGHT) | curses.A_BOLD
                     if self._site_edit_field == 'bold' else curses.color_pair(db.C_NORMAL))
        db.safe_addstr(stdscr, by1 + 6, bx1 + 2,
                        f"Bold: {'ON' if bold_val else 'off'}", bold_attr)

        sample_role = ov.get('role', entry['default_role']) if entry['default_role'] else None
        if sample_role:
            sample_pair = ROLE_PAIR_NUM[sample_role]
            sample_attr = curses.color_pair(sample_pair)
            if bold_val:
                sample_attr |= curses.A_BOLD
            db.safe_addstr(stdscr, by1 + 8, bx1 + 2, "Sample: ", curses.color_pair(db.C_NORMAL))
            db.safe_addstr(stdscr, by1 + 8, bx1 + 10, " Aa 123 ", sample_attr)

        db.safe_addstr(stdscr, by2, bx1 + 2,
                        " Tab:Field  \u2190\u2192/Space:Change  r:Reset  Esc:Back ",
                        curses.color_pair(db.C_INVHEAD))
