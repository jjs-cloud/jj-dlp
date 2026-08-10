"""
jj-dlp  —  top-bar disk-rate graph (hot-swappable module)
═══════════════════════════════════════════════════════════════════════════════

This module owns ALL of the top-bar disk-rate sparkline logic: the running
state, the fast sub-sampler / bar-value pipeline, the bar-drawing routine,
and the dev 'p' key knob popup that tunes the ``_GRAPH_*`` defaults live.

Because the dashboard keeps a single module reference (``from . import graph
as _graph``) and constructs ``Graph`` from it, you can edit this file while
the app is running and pick the changes up from the 'p' popup — select
"Reload graph.py" and press Enter. The reload re-executes this module, swaps
in a fresh ``Graph`` (disk-rate history is carried over; live-tuned knobs
reset to the newly-loaded defaults) and, as long as this module keeps
exposing a ``Graph`` class that constructs from ``(dashboard)``, the app
keeps running. A broken edit never takes the app down — the reload fails
safely and the previous graph keeps running.
"""

import time
from collections import deque

import curses  # noqa: E402

from . import theme
from .file_manager import human_rate


class Graph:
    """Owns state/behavior for the top-bar disk-rate sparkline.

    Constructed by ``JJDlpDashboard`` as ``self.graph`` (mirroring
    ``FileManagerTab`` / ``ThemeManager``) and driven by the dashboard's draw
    loop and key dispatcher. Reload-safe: history survives a module reload,
    live-tuned knobs do not.
    """

    # Density ramp for the disk-rate bars, ordered by actual visual ink
    # coverage (not by codepoint): ' '=0%, ░=25%, ▒=50%, ▄=50%, ▓=75%, █=100%.
    # ▄ (LOWER HALF BLOCK) is a *solid* half-fill, so to the eye it reads as
    # roughly the same weight as ▒ (a 50% dither) — it is NOT denser than ▓.
    # It used to sit between ▓ and █ in this list, which meant a bar's tip
    # could land on a *lighter-looking* glyph (▄) to represent a value that
    # was actually higher than another bar's tip landing on ▓ — i.e. a
    # shorter/lighter-looking bar could legitimately represent a higher
    # rate than a taller one. Placing ▄ at its correct weight (tied with ▒,
    # ahead of ▓) fixes that: density now only ever increases (or ties) as
    # you move up the ramp. All glyphs are standard codepage-437 glyphs
    # (confirmed against supported_characters.txt), so — unlike the
    # eighth-block chars — these are safe on cmd.exe/PowerShell as well as
    # real terminals. Index 0 is "empty".
    _GRAPH_RAMP = [" ", "\u2591", "\u2592", "\u2584", "\u2593", "\u2588"]  # ' ░▒▄▓█'
    # Cadence (seconds) of the instantaneous-rate sub-sampler feeding the
    # top-bar graph. Smaller = burstier/more random bars (each bar becomes a
    # point reading over a shorter window); larger = smoother. Only the
    # actively-recording files are statted, so sub-second values are cheap.
    _GRAPH_SUBSAMPLE_S = 0.5

    # ── Top-bar disk-rate bar value pipeline ──────────
    # How each bar's value is derived from the sub-samples. One of:
    #   "instantaneous"  – the raw spot rate at the moment the bar falls.
    #                      Bursty/blank — it zeroes out between yt-dlp's
    #                      disk flushes, so at long GRAPH_SCALE values most
    #                      bars come up empty (that's the "blank bars"
    #                      symptom). Unchanged legacy behavior.
    #   "window_average" – mean write rate across the whole GRAPH_SCALE
    #                      window (bytes grown ÷ elapsed). Never zero while
    #                      anything is recording; the closest semantic match
    #                      to the File Manager tab's Rate column.
    #   "window_peak"    – the peak spot rate seen during the window. Keeps
    #                      a spiky look and never blanks, but every bar is a
    #                      max-flush spike, so heights run fairly uniform.
    #   "decay_hold"     – per sub-sample: held = max(spot, held * decay).
    #                      A flush jumps the bar up instantly, then it decays
    #                      back toward zero between flushes. Only useful at
    #                      short GRAPH_SCALE values — at 600s/bar it decays
    #                      to ~zero long before the next bar falls.
    _GRAPH_MODE = "window_average"

    # decay_hold: multiplier applied to the held rate every sub-sample.
    # Lower = the bar falls back toward zero faster after a flush.
    # 0.9 per 0.5s sub-sample leaves ~35% of the peak after ~5s.
    _GRAPH_DECAY_PER_SUBSAMPLE = 0.9

    # window_average: if >0, average only the most recent N sub-samples
    # (N × _GRAPH_SUBSAMPLE_S seconds) instead of the whole GRAPH_SCALE
    # window. E.g. 2 → a ~1s rolling average — snappy, very File-Manager
    # like; 0 → smooth average over the entire GRAPH_SCALE window.
    _GRAPH_AVG_SUBSAMPLES = 0

    # Bars below this rate (B/s) are recorded as 0 (drawn blank). Raise it
    # to de-emphasize a slow trickle; keep at 0 to show every non-zero
    # window.
    _GRAPH_MIN_BAR_RATE = 0.0

    # draw: minimum visual height (in 1/11-row ramp units) for any non-zero
    # bar, so a tiny-but-real rate draws as a faint ░ instead of a fully
    # blank column. Set to 0 for strict auto-scaling.
    _GRAPH_MIN_BAR_HEIGHT = 0
    # Body density levels (ramp indices into the reordered _GRAPH_RAMP
    # above: ' '=0 ░=1 ▒=2 ▄=3 ▓=4 █=5). The half-block (▄, index 3) is
    # excluded from bodies — a body of ▄ would read as a half-height bar —
    # so it only ever appears as a *tip* glyph, picked up automatically by
    # the tip formula in draw() because it now sits at its correct weight
    # inside the ramp.
    _GRAPH_BODY_LEVELS = [1, 2, 4, 5]  # ░ ▒ ▓ █
    _GRAPH_BODY_STARTS = [0, 1, 3, 6]  # state offset of each body level

    def __init__(self, dashboard):
        self.dashboard = dashboard

        # ── Top-bar disk-rate sparkline ──────────────────────────────────────
        # One bar per GRAPH_SCALE seconds (GRAPH_SCALE is dashboard-owned:
        # self.dashboard.graph_scale). Each bar's value is derived from the
        # fast sub-sampler (dashboard.file_manager.sample_active_write_rates)
        # according to the _GRAPH_MODE knob — window_average by default, so
        # bars stop blanking out between yt-dlp's disk flushes. It counts
        # only files that are actively being recorded by yt-dlp (per each
        # site's recording_output_paths registry), never File Manager
        # artifact files (Move/Fixup/Trim/Split output). History is kept far
        # longer than any realistic terminal width so widening the window
        # doesn't lose data.
        self.disk_rate_history: deque = deque(maxlen=2000)
        _graph_start = time.time()
        self._disk_graph_last_tick: float = _graph_start
        self._disk_graph_last_subsample: float = 0.0
        self._disk_graph_instant_rate: float = 0.0
        # Value-pipeline state for the selectable bar modes in tick() (see
        # the _GRAPH_* knobs just above the draw method). _disk_graph_window_*
        # accumulate across the current GRAPH_SCALE window;
        # _disk_graph_bytes_ring holds the last N sub-sample byte-deltas for
        # the rolling-average variant.
        self._disk_graph_window_bytes: float = 0.0
        self._disk_graph_window_start: float = _graph_start
        self._disk_graph_window_peak: float = 0.0
        self._disk_graph_held_rate: float = 0.0
        self._disk_graph_bytes_ring: deque = deque(maxlen=self._GRAPH_AVG_SUBSAMPLES or 1)

        # ── Graph-knob popup state (dev feature, hidden 'p' hotkey) ─────────
        # Lets you tune the top-bar disk-rate graph's _GRAPH_* knobs live
        # (instance attrs shadow the class defaults) and see the result
        # immediately, without restarting. The last row of the popup is a
        # dev action that hot-reloads this module from disk.
        self.popup_open   = False
        self.popup_sel    = 0
        self.popup_scroll = 0
        self.popup_edit       = None  # str buffer while typing a value
        self.popup_edit_idx   = 0
        self.popup_status     = ""    # last reload result, shown in the legend

    # ── Top-bar disk-rate sparkline ─────────────────────────────────────────
    def tick(self):
        """Feed the top-bar disk-rate sparkline.

        A fast sub-sampler stats only the actively-recording files every
        ``_GRAPH_SUBSAMPLE_S`` seconds (cheap — no os.walk) and keeps a
        running total of bytes grown plus the peak and instantaneous spot
        rate for the current window. Once every GRAPH_SCALE seconds a bar
        falls, derived from those accumulated sub-samples per
        ``_GRAPH_MODE`` — so each bar can be an average, a spot reading, a
        peak, or a decay-hold, whichever you set the knobs to.
        """
        now = time.time()
        # Fast sub-sampler: only stats the actively-recording files, so it
        # can run at a sub-second cadence without re-walking the OUTPUT_DIRs.
        if now - self._disk_graph_last_subsample >= self._GRAPH_SUBSAMPLE_S:
            self._disk_graph_last_subsample = now
            try:
                inst_rate, grown = self.dashboard.file_manager.sample_active_write_rates()
            except Exception:
                inst_rate, grown = 0.0, 0.0
            self._disk_graph_instant_rate = inst_rate
            self._disk_graph_window_bytes += grown
            if self._GRAPH_AVG_SUBSAMPLES > 0:
                self._disk_graph_bytes_ring.append(grown)
            if inst_rate > self._disk_graph_window_peak:
                self._disk_graph_window_peak = inst_rate
            self._disk_graph_held_rate = max(
                inst_rate,
                self._disk_graph_held_rate * self._GRAPH_DECAY_PER_SUBSAMPLE,
            )
        if now - self._disk_graph_last_tick < self.dashboard.graph_scale:
            return
        elapsed = max(now - self._disk_graph_window_start, 0.001)
        if self._GRAPH_MODE == "window_average":
            if self._GRAPH_AVG_SUBSAMPLES > 0 and self._disk_graph_bytes_ring:
                n = len(self._disk_graph_bytes_ring)
                value = sum(self._disk_graph_bytes_ring) / (n * self._GRAPH_SUBSAMPLE_S)
            else:
                value = self._disk_graph_window_bytes / elapsed
        elif self._GRAPH_MODE == "window_peak":
            value = self._disk_graph_window_peak
        elif self._GRAPH_MODE == "decay_hold":
            value = self._disk_graph_held_rate
        else:  # "instantaneous"
            value = self._disk_graph_instant_rate
        value = max(0.0, value)
        self.disk_rate_history.append(value if value >= self._GRAPH_MIN_BAR_RATE else 0.0)
        # Reset per-window accumulators for the next bar. The decay-hold
        # carry-over is intentionally NOT reset — it keeps decaying across
        # windows until the next flush raises it again.
        self._disk_graph_window_bytes = 0.0
        self._disk_graph_window_start = now
        self._disk_graph_window_peak = 0.0
        self._disk_graph_last_tick = now

    def draw(self, y0: int, x0: int, x1: int, y1: int):
        """Draw the growing/scrolling disk-rate sparkline between the logo
        and the system-time clock, plus the auto-scale "max …" label above
        its right edge.

        (y0, x0) is the top-left, (x1, y1) the bottom-right (inclusive) of
        the available area. The graph fills right-to-left as new samples
        arrive, then scrolls once it reaches the logo. Height is auto-scaled
        to the tallest sample currently on screen (not a fixed constant) so
        the bars use the full available height and stay as visually varied
        as possible.

        Each bar encodes rate along three independent axes: height (whole
        rows), the body's density (a texture from the ramp picked per bar, so
        two bars landing on the same height still read as distinct), and the
        tip's density (the topmost row, which is capped at the body's density).
        That gives 11 distinguishable sub-states per row instead of 1. The
        half-block (▄) never appears as a body (a body of ▄ would read as a
        half-height bar) — only as a tip glyph, at its correct visual weight
        in _GRAPH_RAMP (tied with ▒, below ▓) so tip density is monotonic
        with the underlying rate instead of a lighter-looking glyph ever
        standing in for a higher value. This is the finest resolution
        available without risking the eighth-block glyphs that don't render
        on cmd.exe/PowerShell.

        Monochrome — a single color/attr for the whole graph, no height-based
        color tiering. That single attr is theme-editable (see SITE_REGISTRY
        in theme.py: 'main_jjdlpdashboard_draw_disk_rate_graph') — it shows
        up in the in-app theme manager as "Top-Bar Disk-Rate Graph" and
        defaults to the SYSTEM role, bold.
        """
        d = self.dashboard
        graph_w = max(0, x1 - x0 + 1)
        graph_h = max(1, y1 - y0 + 1)
        if graph_w <= 0 or not self.disk_rate_history:
            return None

        visible = list(self.disk_rate_history)[-graph_w:]
        # Auto-scale to the loudest sample currently visible, so the graph
        # always uses its full height instead of being capped by a constant.
        # A small floor avoids a single near-zero screen looking artificially
        # "maxed out" from float noise.
        scale_max = max(visible) if any(v > 0.01 for v in visible) else 1.0

        per_row = self._GRAPH_BODY_STARTS[-1] + len(self._GRAPH_RAMP) - 1  # 11 (body, tip) pairs per row
        res_units = graph_h * per_row
        # Theme-editable — appears in the theme manager (n) as "Top-Bar
        # Disk-Rate Graph"; defaults to the SYSTEM role, bold.
        attr = theme.attr(d, "main_jjdlpdashboard_draw_disk_rate_graph")

        n = len(visible)
        for i, rate in enumerate(visible):
            col = x1 - (n - 1 - i)
            if col < x0:
                continue
            units = int(round((rate / scale_max) * res_units)) if scale_max > 0 else 0
            units = max(0, min(res_units, units))
            if units <= 0:
                # Keep tiny-but-real rates from blanking the column entirely
                # (see _GRAPH_MIN_BAR_HEIGHT).
                if rate > 0.01 and self._GRAPH_MIN_BAR_HEIGHT > 0:
                    units = min(res_units, self._GRAPH_MIN_BAR_HEIGHT)
                else:
                    continue

            # Height (whole rows) plus which (body, tip) density pair this
            # exact value picked within that row's bucket.
            height = min(graph_h, (units - 1) // per_row + 1)
            local = (units - 1) % per_row
            bidx = 0
            while (bidx + 1 < len(self._GRAPH_BODY_STARTS)
                   and self._GRAPH_BODY_STARTS[bidx + 1] <= local):
                bidx += 1
            body_char = self._GRAPH_RAMP[self._GRAPH_BODY_LEVELS[bidx]]
            tip_char = self._GRAPH_RAMP[local - self._GRAPH_BODY_STARTS[bidx] + 1]

            for r in range(height):
                row = y1 - r
                if row < y0:
                    break
                ch = tip_char if r == height - 1 else body_char
                d.safe_addstr(d.stdscr, row, col, ch, attr)

        # Y-axis label above the graph's right edge: the fastest rate
        # currently on screen. The scale is auto-resizing, so this gives
        # the top of the axis a concrete value.
        if scale_max >= 5.0:
            _label = f"max {human_rate(scale_max)}"
            _label_x = x1 - len(_label) + 1
            if _label_x >= x0:
                d.safe_addstr(d.stdscr, 0, _label_x, _label,
                            theme.attr(d, "main_jjdlpdashboard_draw_disk_rate_graph"))
        return scale_max

    # ── Graph-knob popup (dev feature, 'p' hotkey) ──────────────────────────
    # Bare-bones live editor for the top-bar disk-rate graph's _GRAPH_*
    # knobs. Setting them as instance attributes shadows the class defaults
    # in the constants block, so the graph reflects changes on the very next
    # frame — no restart needed. Nothing here is persisted; defaults in the
    # code remain the source of truth across restarts. The last row reloads
    # this module from disk.
    def toggle_popup(self):
        """Open/close the 'p' knob popup (dev feature)."""
        self.popup_open = not self.popup_open
        self.popup_sel = 0
        self.popup_scroll = 0
        self.popup_edit = None

    def _knob_rows(self):
        """Row spec for each live-tunable graph knob, plus the
        hot-reload dev action at the end."""
        return [
            {"label": "MODE",             "attr": "_GRAPH_MODE",               "kind": "enum",
             "choices": ["instantaneous", "window_average", "window_peak", "decay_hold"]},
            {"label": "SUBSAMPLE_S",      "attr": "_GRAPH_SUBSAMPLE_S",       "kind": "float",
             "step": 0.1, "lo": 0.05, "hi": 5.0, "fmt": "{:.2f}"},
            {"label": "DECAY_PER_SUB",    "attr": "_GRAPH_DECAY_PER_SUBSAMPLE", "kind": "float",
             "step": 0.05, "lo": 0.0, "hi": 1.0, "fmt": "{:.2f}"},
            {"label": "AVG_SUBSAMPLES",   "attr": "_GRAPH_AVG_SUBSAMPLES",    "kind": "int",
             "step": 1, "lo": 0, "hi": 500, "fmt": "{}"},
            {"label": "MIN_BAR_RATE",     "attr": "_GRAPH_MIN_BAR_RATE",      "kind": "float",
             "step": 1000, "lo": 0.0, "hi": 1e12, "fmt": "{:.0f}"},
            {"label": "MIN_BAR_HEIGHT",   "attr": "_GRAPH_MIN_BAR_HEIGHT",    "kind": "int",
             "step": 1, "lo": 0, "hi": 11, "fmt": "{}"},
            {"label": "Clear tallest bar", "attr": None, "kind": "action", "action": "clear_tallest"},
            {"label": "Clear graph bars", "attr": None, "kind": "action", "action": "clear"},
            {"label": "Reload graph.py",  "attr": None, "kind": "action", "action": "reload"},
        ]

    def _popup_set(self, row, value):
        """Clamp and store *value* for *row*, applying side effects (e.g.
        resizing the rolling-average ring when AVG_SUBSAMPLES changes)."""
        if row["kind"] == "enum":
            setattr(self, row["attr"], value)
            return
        if row["kind"] == "int":
            value = int(float(value))
        else:
            value = float(value)
        lo, hi = row.get("lo"), row.get("hi")
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        setattr(self, row["attr"], value)
        if row["attr"] == "_GRAPH_AVG_SUBSAMPLES":
            self._disk_graph_bytes_ring = deque(maxlen=int(value) or 1)

    def _popup_step(self, delta):
        row = self._knob_rows()[self.popup_sel]
        if row["kind"] == "action":
            return
        if row["kind"] == "enum":
            choices = row["choices"]
            cur = getattr(self, row["attr"])
            idx = choices.index(cur) if cur in choices else 0
            self._popup_set(row, choices[(idx + delta) % len(choices)])
        else:
            self._popup_set(row, getattr(self, row["attr"]) + delta * row["step"])

    def clear_all_bars(self) -> None:
        """Clear every bar from the top-bar disk-rate graph — in memory and
        out of global.json — so no stale history comes back on the next
        launch.

        Persisting the empty history is delegated to main.py's existing
        _save_disk_rate_history (lazily imported: only graph.py hot-reloads
        via the 'p' popup, main.py does not). Writing [] is equivalent to
        removing the key — _load_disk_rate_history reads it back as no bars.
        """
        self.disk_rate_history.clear()
        try:
            from . import main as _main
            _main._save_disk_rate_history([])
            self.popup_status = "graph bars cleared"
        except Exception as _e:
            self.popup_status = f"clear failed: {_e}"

    def clear_tallest_bar(self) -> None:
        """Remove just the single tallest bar from the top-bar disk-rate
        graph — in memory and in global.json — leaving the rest of the
        history intact.

        Useful for knocking out one freak spike (e.g. a brief burst that's
        now permanently pinning the auto-scale ceiling) without wiping the
        whole graph. If multiple bars are tied for tallest, only the first
        one encountered is removed. No-op (with a status message) if the
        graph is currently empty.
        """
        if not self.disk_rate_history:
            self.popup_status = "graph is empty — nothing to clear"
            return
        bars = list(self.disk_rate_history)
        tallest_idx = max(range(len(bars)), key=lambda i: bars[i])
        removed = bars.pop(tallest_idx)
        self.disk_rate_history = deque(bars, maxlen=self.disk_rate_history.maxlen)
        try:
            from . import main as _main
            _main._save_disk_rate_history(bars)
            self.popup_status = f"tallest bar cleared ({human_rate(removed)})"
        except Exception as _e:
            self.popup_status = f"clear tallest failed: {_e}"

    def handle_key(self, key) -> bool:
        rows = self._knob_rows()
        if self.popup_edit is not None:
            if key == 27:  # Esc cancels the in-progress edit
                self.popup_edit = None
            elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):  # Enter commits
                try:
                    if rows[self.popup_edit_idx]["kind"] == "int":
                        val = int(self.popup_edit.strip())
                    else:
                        val = float(self.popup_edit.strip())
                    self._popup_set(rows[self.popup_edit_idx], val)
                except ValueError:
                    pass  # leave the buffer so the user can fix it
                self.popup_edit = None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.popup_edit = self.popup_edit[:-1]
            elif 32 <= key < 127:
                ch = chr(key)
                if ch in "0123456789.":
                    if ch == "." and "." in self.popup_edit:
                        return True
                    self.popup_edit += ch
            return True

        if key in (27, ord('q'), ord('Q')):  # Esc / Q closes the popup
            self.popup_open = False
        elif key == curses.KEY_UP:
            self.popup_sel = max(0, self.popup_sel - 1)
        elif key == curses.KEY_DOWN:
            self.popup_sel = min(len(rows) - 1, self.popup_sel + 1)
        elif key in (curses.KEY_LEFT, ord('h')):
            self._popup_step(-1)
        elif key in (curses.KEY_RIGHT, ord('l')):
            self._popup_step(1)
        elif key in (ord('\n'), ord('\r'), curses.KEY_ENTER, 459):
            row = rows[self.popup_sel]
            if row["kind"] == "action":
                if row.get("action") == "clear_tallest":
                    # Drop only the single tallest bar (in memory + on disk).
                    self.clear_tallest_bar()
                elif row.get("action") == "clear":
                    # Clear every bar from the on-screen graph and from
                    # global.json so no history comes back on restart.
                    self.clear_all_bars()
                else:
                    # Hot-reload this module. On success the dashboard swaps
                    # in a fresh Graph (popup closes); on failure the popup
                    # stays open and the legend shows the error.
                    self.dashboard.reload_graph_module()
            elif row["kind"] == "enum":
                self._popup_step(1)
            else:
                self.popup_edit = ""
                self.popup_edit_idx = self.popup_sel
        return True

    def draw_popup(self) -> None:
        if not self.popup_open:
            return
        d = self.dashboard
        h, w = d.stdscr.getmaxyx()
        rows = self._knob_rows()
        box_w = min(w - 4, 60)
        box_h = min(h - 4, len(rows) + 6)
        by1 = max(0, (h - box_h) // 2)
        bx1 = max(0, (w - box_w) // 2)
        by2 = by1 + box_h
        bx2 = bx1 + box_w

        attr_bg = theme.attr(d, "main_jjdlpdashboard_draw_changelog_popup_normal_1")
        for y in range(by1, by2 + 1):
            d.safe_addstr(d.stdscr, y, bx1, " " * (box_w + 1), attr_bg)
        d.draw_box(d.stdscr, by1, bx1, by2, bx2, d.C_CHROME)
        d.safe_addstr(d.stdscr, by1, bx1 + 2, " GRAPH KNOBS ",
                    theme.attr(d, "main_jjdlpdashboard_draw_changelog_popup_hilight"))

        vis = max(1, box_h - 4)
        if self.popup_sel < self.popup_scroll:
            self.popup_scroll = self.popup_sel
        elif self.popup_sel >= self.popup_scroll + vis:
            self.popup_scroll = self.popup_sel - vis + 1

        for i in range(self.popup_scroll, min(len(rows), self.popup_scroll + vis)):
            row = rows[i]
            if row["kind"] == "action":
                _action = row.get("action")
                if _action == "clear_tallest":
                    val_txt = "Enter to clear tallest"
                elif _action == "clear":
                    val_txt = "Enter to clear"
                else:
                    val_txt = "Enter to reload"
            else:
                cur = getattr(self, row["attr"])
                if row["kind"] == "enum":
                    val_txt = str(cur)
                else:
                    val_txt = row["fmt"].format(cur)
            if self.popup_edit is not None and self.popup_edit_idx == i:
                val_txt = self.popup_edit + "_"
            prefix = "> " if i == self.popup_sel else "  "
            attr = (theme.attr(d, "main_jjdlpdashboard_draw_changelog_popup_hilight")
                    if i == self.popup_sel
                    else theme.attr(d, "main_jjdlpdashboard_draw_changelog_popup_normal_2"))
            text = (prefix + row["label"].ljust(18) + val_txt)[:box_w - 4]
            d.safe_addstr(d.stdscr, by1 + 2 + (i - self.popup_scroll), bx1 + 2, text, attr)

        if self.popup_status:
            _legend = " " + self.popup_status[:box_w - 6] + " "
        else:
            _legend = " \u2190/\u2192 step   Enter: edit / reload   Esc: close "
        d.safe_addstr(d.stdscr, by2, bx1 + 2, _legend[:box_w - 4],
                    theme.attr(d, "main_jjdlpdashboard_draw_changelog_popup_invhead"))
