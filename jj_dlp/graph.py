"""
jj-dlp  —  top-bar disk-rate graph (hot-swappable module)
═══════════════════════════════════════════════════════════════════════════════

This module owns ALL of the top-bar disk-rate sparkline logic: the running
state, the fast sub-sampler / multi-resolution history, the bar-drawing
routine, and the dev 'p' key knob popup that tunes the ``_GRAPH_*`` defaults
live.

Because the dashboard keeps a single module reference (``from . import graph
as _graph``) and constructs ``Graph`` from it, you can edit this file while
the app is running and pick the changes up from the 'p' popup — select
"Reload graph.py" and press Enter. The reload re-executes this module, swaps
in a fresh ``Graph`` (history is carried over; live-tuned knobs reset to the
newly-loaded defaults) and, as long as this module keeps exposing a ``Graph``
class that constructs from ``(dashboard)``, the app keeps running. A broken
edit never takes the app down — the reload fails safely and the previous
graph keeps running.

── Recording vs. rendering ─────────────────────────────────────────────────
Recording (tick()) and rendering (draw()/GRAPH_SCALE) are fully decoupled.
tick() samples the actively-recording files' write rate every
_GRAPH_SUBSAMPLE_S seconds and feeds it into a small waterfall of
fixed-size, fixed-resolution "tiers" (see _GRAPH_TIERS below) — the same
technique RRDtool/Cacti/Graphite use for keeping bounded-size history at
multiple time resolutions. Each tier stores (timestamp, avg_rate,
peak_rate) rows and independently covers "now back to however far its own
rows reach"; older rows just roll off (fixed memory, no unbounded growth,
no dependency on GRAPH_SCALE). Editing GRAPH_SCALE just changes which tier
draw() reads from and how it buckets that tier's rows into on-screen bars —
instant, since the data's already there.
"""

import time
from collections import deque
from typing import Dict, List

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

    # Per-row (body, tip) glyph pairs for local level 0..10, ordered so that a
    # rate increase (local level up) always reads as a fuller bar — the exact
    # downward ramp verified in demo.py (see docs/bar_order.txt). The body
    # fills every row below the bar's topmost row; the tip sits on that
    # topmost row. All glyphs are standard codepage-437 glyphs (confirmed
    # against supported_characters.txt), so — unlike the eighth-block chars —
    # these are safe on cmd.exe/PowerShell as well as real terminals. The
    # half-block (▄) never reads as a body — a body of ▄ would look like a
    # half-height bar — so every ▄ here is a *tip* glyph.
    _GRAPH_LEVELS = [  # local → (body, tip); index 0 is the emptiest bar
        ("\u2592", "\u2584"),  # 0  ▒▄  (lightest: dither body + half-block tip)
        ("\u2593", "\u2584"),  # 1  ▓▄
        ("\u2588", "\u2584"),  # 2  █▄
        ("\u2591", "\u2591"),  # 3  ░░
        ("\u2592", "\u2591"),  # 4  ▒░
        ("\u2593", "\u2591"),  # 5  ▓░
        ("\u2593", "\u2592"),  # 6  ▓▒
        ("\u2588", "\u2591"),  # 7  █░
        ("\u2588", "\u2592"),  # 8  █▒
        ("\u2588", "\u2593"),  # 9  █▓
        ("\u2588", "\u2588"),  # 10 ██  (fullest)
    ]

    # Cadence (seconds) of the instantaneous-rate sub-sampler feeding the
    # top-bar graph, and the finest possible tier resolution (see
    # _GRAPH_TIERS below). Only the actively-recording files are statted,
    # so sub-second values are cheap.
    _GRAPH_SUBSAMPLE_S = 0.5

    # ── Multi-resolution history (RRDtool-style tier waterfall) ─────────────
    # Every _GRAPH_SUBSAMPLE_S seconds, tick() feeds one raw sample into the
    # FIRST tier below. Once that tier's own bucket (its "step") has
    # accumulated enough real time, it's closed out — (avg, peak) of
    # whatever fell inside it gets appended as one row — and that same
    # closed value is fed into the NEXT (coarser) tier's bucket, and so on.
    # This is a waterfall: tier N only ever receives already-consolidated
    # values from tier N-1, never raw sub-samples directly (except tier 0).
    #
    # Each tier is a fixed-size ring (deque(maxlen=rows)) of
    # (timestamp, avg_rate, peak_rate) rows, so total memory/disk footprint
    # never grows no matter how long the app runs. "step" is that tier's
    # time resolution (seconds/row); "rows" is how many rows it keeps.
    # rows × step = that tier's own maximum retained time span:
    #
    #   label   step      rows   span (once fully populated)
    #   1s      1 s        500   ~8.3 min
    #   15s     15 s       500   ~2.1 hr
    #   5m      300 s      500   ~1.7 days
    #   2h      7200 s     500   ~41.7 days
    #
    # Total: 2000 rows (same footprint as the old flat 2000-entry history —
    # just reorganized into 4 resolutions instead of one). GRAPH_SCALE can be
    # set to anything ≥1 second; draw() (see _select_tier()/_bucket_bars())
    # automatically picks whichever tier gives the best resolution while
    # still having enough retained history to fill the screen — so "last
    # week"/"last month" views are served by the 2h tier once the app has
    # been running that long (chunky ~2h-wide real data points, not
    # fabricated ones), while a 30s or 5-minute view gets full sub-minute
    # resolution from a finer tier. Want smoother week/month views instead
    # of the 2h-blocky look? Raise the "2h" tier's rows (or its own real
    # span shrinks proportionally) or insert an in-between tier (e.g.
    # step=1800, rows=500 for a 30-min tier reaching ~10.4 days) — each
    # added/enlarged tier just costs its own row count in total footprint.
    _GRAPH_TIERS = [
        {"label": "1s",  "step": 1,    "rows": 500},
        {"label": "15s", "step": 15,   "rows": 500},
        {"label": "5m",  "step": 300,  "rows": 500},
        {"label": "2h",  "step": 7200, "rows": 500},
    ]

    # ── Top-bar disk-rate bar value pipeline ──────────
    # How each bar's value is derived from the selected tier's rows (see
    # _bucket_bars()). Applied at draw() time, NOT at recording time —
    # recording (tick()) always runs the same regardless of this knob; this
    # only controls how already-recorded rows get combined into bars. One of:
    #   "instantaneous"  – each bar uses the average of whatever tier rows
    #                      land in it. At the "1s" tier this is close to a
    #                      true spot reading (~1-2 sub-samples per row); at
    #                      coarser tiers it's really an average (the whole
    #                      point of a tier is that it's already consolidated,
    #                      so a true "instant spot value" isn't retained past
    #                      the 1s tier — this mode just doesn't apply its own
    #                      extra smoothing on top of what the tier stores).
    #   "window_average" – mean of the avg_rate field across the tier rows
    #                      landing in each bar. Never zero while anything is
    #                      recording; the closest semantic match to the File
    #                      Manager tab's Rate column.
    #   "window_peak"    – max of the peak_rate field across the tier rows
    #                      landing in each bar. Keeps a spiky look and never
    #                      blanks.
    #   "decay_hold"     – held = max(avg_rate, held × decay), replayed in
    #                      chronological order across the selected tier's
    #                      rows (decay-per-row is _GRAPH_DECAY_PER_SUBSAMPLE
    #                      scaled up to that tier's row spacing, so the same
    #                      real-world decay rate applies regardless of which
    #                      tier ends up selected). A flush jumps the bar up,
    #                      then it decays back toward zero between flushes.
    _GRAPH_MODE = "window_average"

    # decay_hold: multiplier applied to the held rate every _GRAPH_SUBSAMPLE_S
    # sub-sample (scaled up per-tier-row in _bucket_bars() — see above).
    # Lower = the bar falls back toward zero faster after a flush. 0.9 per
    # 0.5s sub-sample leaves ~35% of the peak after ~5s.
    _GRAPH_DECAY_PER_SUBSAMPLE = 0.9

    # Bars below this rate (B/s) are recorded as 0 (drawn blank). Raise it
    # to de-emphasize a slow trickle; keep at 0 to show every non-zero bar.
    _GRAPH_MIN_BAR_RATE = 0.0

    # draw: minimum visual height (in 1/11-row ramp units) for any non-zero
    # bar, so a tiny-but-real rate draws as a faint bar instead of a fully
    # blank column. Set to 0 for strict auto-scaling.
    _GRAPH_MIN_BAR_HEIGHT = 0

    def __init__(self, dashboard):
        self.dashboard = dashboard

        # ── Multi-resolution disk-rate history ───────────────────────────
        # One fixed-size deque of (timestamp, avg_rate, peak_rate) rows per
        # tier in _GRAPH_TIERS — see the class-level comment above for the
        # full design. It counts only files that are actively being
        # recorded by yt-dlp (per each site's recording_output_paths
        # registry), never File Manager artifact files (Move/Fixup/Trim/
        # Split output).
        self._tier_data: Dict[str, deque] = {
            t["label"]: deque(maxlen=t["rows"]) for t in self._GRAPH_TIERS
        }
        # In-progress (not-yet-closed) accumulator per tier: the bucket
        # currently being filled, closed out by _ingest() once its own
        # "step" worth of real time has elapsed.
        self._tier_accum: Dict[str, dict] = {
            t["label"]: {"start": None, "sum": 0.0, "count": 0, "peak": 0.0}
            for t in self._GRAPH_TIERS
        }
        self._disk_graph_last_subsample: float = 0.0
        self._disk_graph_instant_rate: float = 0.0
        # Live "current tip" value for decay_hold — kept up to date every
        # sub-sample for potential display use; the actual bucketed decay
        # curve used for drawing is recomputed fresh from tier rows in
        # _bucket_bars() so it stays correct even after the DECAY_PER_SUB
        # knob is tuned live.
        self._disk_graph_held_rate: float = 0.0

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

    # ── Multi-resolution history: recording ─────────────────────────────────
    def _ingest(self, avg_value: float, peak_value: float, now: float, tier_idx: int = 0) -> None:
        """Feed one already-consolidated (avg, peak) sample into tier
        ``_GRAPH_TIERS[tier_idx]``'s in-progress bucket. If that bucket has
        accumulated a full "step" worth of real time, close it out (append
        the row to that tier's deque) and cascade the closed value into the
        next coarser tier. Recurses at most len(_GRAPH_TIERS) deep.
        """
        if tier_idx >= len(self._GRAPH_TIERS):
            return
        tier = self._GRAPH_TIERS[tier_idx]
        acc = self._tier_accum[tier["label"]]
        if acc["start"] is None:
            acc["start"] = now
        acc["sum"] += avg_value
        acc["count"] += 1
        if peak_value > acc["peak"]:
            acc["peak"] = peak_value
        if now - acc["start"] >= tier["step"]:
            closed_avg = acc["sum"] / acc["count"]
            closed_peak = acc["peak"]
            closed_ts = acc["start"]
            self._tier_data[tier["label"]].append((closed_ts, closed_avg, closed_peak))
            acc["start"] = None
            acc["sum"] = 0.0
            acc["count"] = 0
            acc["peak"] = 0.0
            self._ingest(closed_avg, closed_peak, now, tier_idx + 1)

    def tick(self):
        """Feed the top-bar disk-rate graph's multi-resolution history.

        A fast sub-sampler stats only the actively-recording files every
        ``_GRAPH_SUBSAMPLE_S`` seconds (cheap — no os.walk) and feeds the
        instantaneous spot rate into the tier waterfall via ``_ingest()``.
        Recording never depends on GRAPH_SCALE or _GRAPH_MODE — those only
        affect how draw() reads back and buckets the already-recorded tier
        rows (see ``_select_tier()`` / ``_bucket_bars()``).
        """
        now = time.time()
        if now - self._disk_graph_last_subsample < self._GRAPH_SUBSAMPLE_S:
            return
        self._disk_graph_last_subsample = now
        try:
            inst_rate, _grown = self.dashboard.file_manager.sample_active_write_rates()
        except Exception:
            inst_rate = 0.0
        inst_rate = max(0.0, inst_rate)
        self._disk_graph_instant_rate = inst_rate
        self._disk_graph_held_rate = max(
            inst_rate,
            self._disk_graph_held_rate * self._GRAPH_DECAY_PER_SUBSAMPLE,
        )
        self._ingest(inst_rate, inst_rate, now)

    # ── Multi-resolution history: rendering ─────────────────────────────────
    def _select_tier(self, graph_w: int) -> dict:
        """Pick the best tier to render *graph_w* bars at the current
        GRAPH_SCALE: the FINEST tier that already has enough retained
        history to fill the whole width, so bars use the best resolution
        available without leaving the left side of the graph blank.

        If no tier has enough history yet (e.g. right after startup, or
        GRAPH_SCALE was just raised past what any tier has accumulated),
        falls back to whichever tier currently holds the most history —
        the same "still filling up" behavior the graph has always had,
        just automatically picking the best available tier instead of
        going blank. That tier will typically be the coarsest one, since
        all tiers grow in lock-step with uptime until each hits its own
        cap (see the class-level tier table), and the coarsest caps last.
        """
        scale = max(1, self.dashboard.graph_scale)
        needed_span = graph_w * scale
        now = time.time()

        def span(t):
            dq = self._tier_data[t["label"]]
            return (now - dq[0][0]) if dq else 0.0

        for t in self._GRAPH_TIERS:  # ascending by step → finest first
            if span(t) >= needed_span:
                return t
        return max(self._GRAPH_TIERS, key=span)

    def _bucket_bars(self, graph_w: int) -> List[float]:
        """Group the selected tier's rows into up to *graph_w* on-screen
        bars, each spanning GRAPH_SCALE seconds of wall-clock time (bar
        boundaries are anchored to "now" and walked backward), combined per
        _GRAPH_MODE. If a tier's own row spacing is coarser than
        GRAPH_SCALE (happens when _select_tier() has to fall back to a
        tier that's coarser than ideal — see there), the same row's value
        will naturally repeat across several consecutive bars, since one
        row covers more real time than one bar does. That's expected: it's
        real recorded data rendered a bit blockier, not fabricated filler.
        """
        tier = self._select_tier(graph_w)
        rows = list(self._tier_data[tier["label"]])  # oldest → newest
        if not rows:
            return []
        scale = max(1, self.dashboard.graph_scale)
        now = time.time()

        buckets: Dict[int, list] = {}
        for ts, avg, peak in rows:
            idx = max(0, int((now - ts) // scale))  # 0 = most recent bucket
            if idx >= graph_w:
                continue
            buckets.setdefault(idx, []).append((ts, avg, peak))
        if not buckets:
            return []

        held_by_ts = {}
        if self._GRAPH_MODE == "decay_hold":
            # Replay the decay curve across ALL rows in chronological order
            # (not just the visible window) so it carries continuously
            # across bucket boundaries, matching the "never resets" intent
            # of the original per-sub-sample design. The per-row decay
            # factor is the sub-sample decay raised to (tier step ÷
            # sub-sample seconds) so the same real-world decay rate holds
            # regardless of which tier ends up selected.
            decay_factor = self._GRAPH_DECAY_PER_SUBSAMPLE ** (
                tier["step"] / max(self._GRAPH_SUBSAMPLE_S, 1e-6)
            )
            held = 0.0
            for ts, avg, _peak in rows:
                held = max(avg, held * decay_factor)
                held_by_ts[ts] = held

        max_idx = max(buckets)
        bars_recent_first = []
        for idx in range(0, max_idx + 1):
            chunk = buckets.get(idx)
            if not chunk:
                bars_recent_first.append(0.0)
                continue
            if self._GRAPH_MODE == "window_peak":
                value = max(p for _, _, p in chunk)
            elif self._GRAPH_MODE == "decay_hold":
                value = max(held_by_ts[ts] for ts, _, _ in chunk)
            else:  # "window_average" / "instantaneous"
                value = sum(a for _, a, _ in chunk) / len(chunk)
            value = max(0.0, value)
            bars_recent_first.append(value if value >= self._GRAPH_MIN_BAR_RATE else 0.0)
        return list(reversed(bars_recent_first))  # oldest → newest, matches draw()

    def draw(self, y0: int, x0: int, x1: int, y1: int):
        """Draw the growing/scrolling disk-rate sparkline between the logo
        and the system-time clock, plus the auto-scale "max …" label above
        its right edge.

        (y0, x0) is the top-left, (x1, y1) the bottom-right (inclusive) of
        the available area. The graph fills right-to-left as new samples
        arrive, then scrolls once it reaches the logo. Height is auto-scaled
        to the samples currently on screen (not a fixed constant) so the
        bars use the full available height and stay as visually varied as
        possible. As soon as two distinct bars are on screen the Y-axis zooms
        to the visible [min, max] range: the shortest bar draws as the
        shortest possible bar (a single ramp unit) and the tallest fills the
        whole graph.

        Each on-screen bar is a live GRAPH_SCALE-second bucket of rows
        pulled fresh from whichever history tier best fits every call (see
        ``_select_tier()`` / ``_bucket_bars()``) — recording and rendering
        are decoupled, so tweaking GRAPH_SCALE or _GRAPH_MODE re-groups the
        existing history immediately instead of waiting for new bars to
        accrue.

        Each bar encodes rate along three independent axes: height (whole
        rows), the body's density (a texture picked per bar, so two bars
        landing on the same height still read as distinct), and the tip's
        density (the topmost row). That gives 11 distinguishable sub-states
        per row instead of 1. The (body, tip) pair for each of those 11
        levels comes straight from the _GRAPH_LEVELS ramp, which is ordered
        so the bars read as monotonically fuller as the rate climbs — a rate
        increase never moves to a lighter-looking bar. This is the finest
        resolution available without risking the eighth-block glyphs that
        don't render on cmd.exe/PowerShell.

        Monochrome — a single color/attr for the whole graph, no height-based
        color tiering. That single attr is theme-editable (see SITE_REGISTRY
        in theme.py: 'main_jjdlpdashboard_draw_disk_rate_graph') — it shows
        up in the in-app theme manager as "Top-Bar Disk-Rate Graph" and
        defaults to the SYSTEM role, bold.
        """
        d = self.dashboard
        graph_w = max(0, x1 - x0 + 1)
        graph_h = max(1, y1 - y0 + 1)
        if graph_w <= 0:
            return None

        visible = self._bucket_bars(graph_w)
        if not visible:
            return None
        n = len(visible)
        # Auto-scale to the samples currently visible, so the graph always
        # uses its full height instead of being capped by a constant. As soon
        # as two distinct bars are on screen, zoom the Y-axis to the visible
        # [min, max] range so the shortest bar is the shortest possible bar
        # and the tallest fills the whole graph. A small floor avoids a
        # single near-zero screen looking artificially "maxed out" from
        # float noise.
        if n >= 2 and max(visible) > min(visible):
            zoomed = True
            scale_min, scale_max = min(visible), max(visible)
        else:
            zoomed = False
            scale_min, scale_max = 0.0, max(visible) if any(v > 0.01 for v in visible) else 1.0

        per_row = len(self._GRAPH_LEVELS)  # 11 (body, tip) pairs per row
        res_units = graph_h * per_row
        # Theme-editable — appears in the theme manager (n) as "Top-Bar
        # Disk-Rate Graph"; defaults to the SYSTEM role, bold.
        attr = theme.attr(d, "main_jjdlpdashboard_draw_disk_rate_graph")

        for i, rate in enumerate(visible):
            col = x1 - (n - 1 - i)
            if col < x0:
                continue
            if zoomed:
                # Stretch the visible [min, max] range across the full
                # height. The shortest bar maps to the shortest possible bar
                # (a single ramp unit — never blank), the tallest to the
                # full graph. _GRAPH_MIN_BAR_HEIGHT, if set, still floors
                # the bars.
                span = scale_max - scale_min
                units = int(round(((rate - scale_min) / span) * res_units)) if span > 0 else res_units
                units = max(1, self._GRAPH_MIN_BAR_HEIGHT, min(res_units, units))
            else:
                units = int(round((rate / scale_max) * res_units)) if scale_max > 0 else 0
                units = max(0, min(res_units, units))
                if units <= 0:
                    # Keep tiny-but-real rates from blanking the column
                    # entirely (see _GRAPH_MIN_BAR_HEIGHT).
                    if rate > 0.01 and self._GRAPH_MIN_BAR_HEIGHT > 0:
                        units = min(res_units, self._GRAPH_MIN_BAR_HEIGHT)
                    else:
                        continue

            # Height (whole rows) plus which (body, tip) density pair this
            # exact value picked within that row's bucket.
            height = min(graph_h, (units - 1) // per_row + 1)
            local = (units - 1) % per_row
            body_char, tip_char = self._GRAPH_LEVELS[local]

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

    # ── Persistence (graph.json, via main.py's _load_graph_state/_save_graph_state) ─
    def to_persist_dict(self) -> dict:
        """Serialize every tier's rows into a plain JSON-able dict.

        main.py treats this as opaque — it just reads/writes it to
        graph.json verbatim (_load_graph_state()/_save_graph_state()) — so
        the on-disk format can evolve here without touching main.py.
        """
        return {
            "version": 1,
            "saved_at": time.time(),
            "tiers": {
                t["label"]: [[ts, avg, peak] for ts, avg, peak in self._tier_data[t["label"]]]
                for t in self._GRAPH_TIERS
            },
        }

    def load_persist_dict(self, data) -> None:
        """Restore tier rows from a dict previously produced by
        to_persist_dict() (i.e. the contents of graph.json). Tolerant of
        missing/malformed data — restores whatever tiers/rows validate and
        silently skips the rest, so a partially-corrupt graph.json still
        gets you back the tiers that did parse instead of starting empty.
        """
        if not isinstance(data, dict):
            return
        tiers = data.get("tiers")
        if not isinstance(tiers, dict):
            return
        for t in self._GRAPH_TIERS:
            raw_rows = tiers.get(t["label"])
            if not isinstance(raw_rows, list):
                continue
            dq = self._tier_data[t["label"]]
            dq.clear()
            for row in raw_rows[-t["rows"]:]:
                try:
                    ts, avg, peak = float(row[0]), float(row[1]), float(row[2])
                except (TypeError, ValueError, IndexError):
                    continue
                dq.append((ts, avg, peak))

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
            {"label": "MIN_BAR_RATE",     "attr": "_GRAPH_MIN_BAR_RATE",      "kind": "float",
             "step": 1000, "lo": 0.0, "hi": 1e12, "fmt": "{:.0f}"},
            {"label": "MIN_BAR_HEIGHT",   "attr": "_GRAPH_MIN_BAR_HEIGHT",    "kind": "int",
             "step": 1, "lo": 0, "hi": 11, "fmt": "{}"},
            {"label": "Clear tallest bar", "attr": None, "kind": "action", "action": "clear_tallest"},
            {"label": "Clear graph bars", "attr": None, "kind": "action", "action": "clear"},
            {"label": "Reload graph.py",  "attr": None, "kind": "action", "action": "reload"},
        ]

    def _popup_set(self, row, value):
        """Clamp and store *value* for *row*."""
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
        """Clear every tier's history from the top-bar disk-rate graph — in
        memory and out of graph.json — so no stale history comes back on
        the next launch.

        Persisting the empty history is delegated to main.py's
        _save_graph_state (lazily imported: only graph.py hot-reloads via
        the 'p' popup, main.py does not).
        """
        for dq in self._tier_data.values():
            dq.clear()
        for acc in self._tier_accum.values():
            acc["start"] = None
            acc["sum"] = 0.0
            acc["count"] = 0
            acc["peak"] = 0.0
        try:
            from . import main as _main
            _main._save_graph_state(self.to_persist_dict())
            self.popup_status = "graph history cleared"
        except Exception as _e:
            self.popup_status = f"clear failed: {_e}"

    def clear_tallest_bar(self) -> None:
        """Remove just the single largest row from whichever tier is
        currently being displayed (the one _select_tier() would pick for
        the live GRAPH_SCALE) — in memory and in graph.json — leaving the
        rest of that tier's history, and every other tier, intact.

        Useful for knocking out one freak spike (e.g. a brief burst that's
        now permanently pinning the auto-scale ceiling) without wiping the
        whole graph. Compares by peak_rate when _GRAPH_MODE is
        "window_peak" (since that's the field driving the ceiling in that
        mode), avg_rate otherwise. If multiple rows are tied for largest,
        only the first one encountered is removed. No-op (with a status
        message) if that tier is currently empty.
        """
        # graph_w doesn't affect which tier has the most data, only whether
        # it's "enough" to fill the screen — pass a generous width so this
        # picks the same tier the graph is actually drawing with right now.
        tier = self._select_tier(graph_w=1000)
        dq = self._tier_data[tier["label"]]
        if not dq:
            self.popup_status = f"{tier['label']} tier is empty — nothing to clear"
            return
        field_idx = 2 if self._GRAPH_MODE == "window_peak" else 1  # (ts, avg, peak)
        rows = list(dq)
        tallest_i = max(range(len(rows)), key=lambda i: rows[i][field_idx])
        removed = rows.pop(tallest_i)
        self._tier_data[tier["label"]] = deque(rows, maxlen=dq.maxlen)
        try:
            from . import main as _main
            _main._save_graph_state(self.to_persist_dict())
            self.popup_status = f"tallest {tier['label']} sample cleared ({human_rate(removed[field_idx])})"
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
                    # Drop only the single tallest row of the currently
                    # displayed tier (in memory + on disk).
                    self.clear_tallest_bar()
                elif row.get("action") == "clear":
                    # Clear every tier's history from the on-screen graph
                    # and from graph.json so no history comes back on
                    # restart.
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
