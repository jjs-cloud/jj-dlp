"""Per-site engine orchestrator.

Doc 1 specifies checker/recording/stall/quality/segments/write_failure/ad_alerts
and priority overrides as separate modules but does not say how one process
combines them into a single running site. This module is that integration
glue, added during Step 17.1's wiring pass.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jj_dlp.core.config import priority as priority_config
from jj_dlp.core.config.app import AppConfig
from jj_dlp.core.engine import ad_alerts
from jj_dlp.core.engine import quality as quality_mod
from jj_dlp.core.engine import recording as recording_mod
from jj_dlp.core.engine import segments as segments_mod
from jj_dlp.core.engine import stall as stall_mod
from jj_dlp.core.engine import write_failure as write_failure_mod
from jj_dlp.core.engine.checker import Checker
from jj_dlp.core.engine.site_state import SiteState
from jj_dlp.core.notify import activity_log, desktop, ntfy, pipe_capture

log = logging.getLogger("jj_dlp.engine.site_engine")

_DAY_ABBR = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _bypass_pairs(data_dir: Path) -> set:
    """Return the {(site, streamer)} pairs currently flagged bypass in priority.json."""
    return {
        (e.get("site"), e.get("streamer"))
        for e in priority_config.get_order(data_dir)
        if e.get("bypass")
    }


def may_start_recording(
    data_dir: Path, app_state: Any, app_cfg: AppConfig, site: str, streamer: str
) -> bool:
    """Return whether a new non-bypass recording may start under recording.max_concurrent."""
    max_concurrent = app_cfg.recording.max_concurrent
    if max_concurrent <= 0:
        return True
    bypass = _bypass_pairs(data_dir)
    if (site, streamer) in bypass:
        return True
    with app_state.recording_decision_lock:
        count = 0
        for site_label, site_state in app_state.site_states().items():
            for name in site_state.known_streamers():
                if site_state.is_recording(name) and (site_label, name) not in bypass:
                    count += 1
        return count < max_concurrent


@dataclass
class EffectiveConfig:
    """One streamer's fully-resolved config: site config plus all seven priority overrides."""

    config: dict
    lq_fallback_disabled: bool
    popup_enabled: bool
    ntfy_enabled: bool
    intro_delay: int
    schedule: Optional[dict]


def resolve_effective_config(data_dir: Path, site: str, streamer: str, config: dict) -> EffectiveConfig:
    """Resolve all seven priority.json override types (Doc 1 §6.1) via resolve_effective into one config.

    This is the single place override resolution happens; checker.py, downloader.py,
    recording.py and friends only ever see the resulting merged config, never a raw
    site config plus their own separate overrides lookup.
    """
    cfg = copy.deepcopy(config)

    quality = priority_config.resolve_effective(data_dir, site, streamer, "quality", None) or {}
    if quality.get("format"):
        cfg["downloader"]["format"] = quality["format"]
    lq_fallback_disabled = bool(quality.get("lq_fallback_disabled"))

    site_notif = cfg.get("notifications", {})
    notif = priority_config.resolve_effective(data_dir, site, streamer, "notifications", None) or {}
    popup_enabled = site_notif.get("popup_enabled", True) if notif.get("popup_enabled") is None else notif["popup_enabled"]
    ntfy_enabled = site_notif.get("ntfy_enabled", False) if notif.get("ntfy_enabled") is None else notif["ntfy_enabled"]
    cfg["notifications"] = {**site_notif, "popup_enabled": popup_enabled, "ntfy_enabled": ntfy_enabled}

    auto_suffix = priority_config.resolve_effective(
        data_dir, site, streamer, "auto_suffix", cfg["output"].get("auto_suffix", True)
    )
    cfg["output"]["auto_suffix"] = bool(auto_suffix)

    output_dir = priority_config.resolve_effective(data_dir, site, streamer, "output_dir", cfg["output"].get("dir"))
    cfg["output"]["dir"] = output_dir

    split = priority_config.resolve_effective(data_dir, site, streamer, "split", None)
    if split is not None:
        if split.get("enabled") is False:
            cfg["timing"]["split_after_minutes"] = 0
        elif split.get("enabled") and split.get("split_after_minutes"):
            cfg["timing"]["split_after_minutes"] = split["split_after_minutes"]

    intro_delay = int(priority_config.resolve_effective(data_dir, site, streamer, "intro_delay", 0) or 0)
    schedule = priority_config.resolve_effective(data_dir, site, streamer, "schedule", None)

    return EffectiveConfig(
        config=cfg,
        lq_fallback_disabled=lq_fallback_disabled,
        popup_enabled=popup_enabled,
        ntfy_enabled=ntfy_enabled,
        intro_delay=intro_delay,
        schedule=schedule,
    )


def within_schedule(schedule: Optional[dict], now: Optional[datetime] = None) -> bool:
    """Return whether now falls inside a resolved schedule override, if one is set."""
    if not schedule or not schedule.get("days"):
        return True
    now = now or datetime.now()
    today = _DAY_ABBR[now.weekday()]
    days = {d.lower()[:3] for d in schedule.get("days", [])}
    if today not in days:
        return False
    start, end = schedule.get("start"), schedule.get("end")
    if not start or not end:
        return True
    return start <= now.strftime("%H:%M") <= end


class SiteEngine:
    """Owns one loaded site's recording pipeline: checker plus its restart-triggering monitors."""

    def __init__(
        self,
        data_dir: Path,
        app_state: Any,
        app_cfg: AppConfig,
        label: str,
        config: dict,
        site_state: SiteState,
        plugin: Any,
        schema_path: Optional[Path] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.app_state = app_state
        self.app_cfg = app_cfg
        self.label = label
        self.config = config
        self.site_state = site_state
        self.plugin = plugin

        self.coordinator = recording_mod.RecordingCoordinator(app_cfg, schema_path)
        self.quality = quality_mod.QualityMonitor(app_cfg, config, site_state, on_restart=self._restart)
        self.stall = stall_mod.StallMonitor(config, site_state, on_stall=self._restart)
        self.segments = segments_mod.SegmentTracker(data_dir, label, site_state)
        self.segment_monitor = segments_mod.SegmentMonitor(
            config, site_state, self.segments, on_split=self._restart
        )
        self.write_failure = write_failure_mod.WriteFailureMonitor(site_state)
        self.site_handle = ad_alerts.SiteHandle(label=label, config=config, site_state=site_state)
        self.ad_dispatcher = ad_alerts.AdAlertDispatcher(self.site_handle, plugin)
        self.checker = Checker(config, site_state, on_live=self._on_live)

        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._generation: Dict[str, int] = {}
        self._started_at: Dict[str, float] = {}
        self._lq_disabled: Dict[str, bool] = {}
        self._gen_lock = threading.Lock()

    def start(self) -> None:
        """Start this site's checker/stall/quality/segment background loops."""
        loops = [self.checker.run_loop, self.stall.run_loop, self.quality.run_loop, self.segment_monitor.run_loop]
        self._threads = [
            threading.Thread(target=loop, args=(self._stop_event,), daemon=True, name=f"{loop.__self__.__class__.__name__}-{self.label}")
            for loop in loops
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Signal every background loop to stop and wait briefly for them to exit."""
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def _make_on_line(self, streamer: str):
        """Return a downloader on_line callback feeding pipe capture, quality, and ad alerts."""
        pipe_feed = pipe_capture.feed(self.label, streamer)

        def _on_line(stream_name: str, line: str) -> None:
            pipe_feed(stream_name, line)
            if not self._lq_disabled.get(streamer):
                self.quality.on_downloader_line(streamer, line)
            self.ad_dispatcher.on_downloader_line(streamer, line)

        return _on_line

    def _register_launch(self, streamer: str, process: Any) -> None:
        """Record a fresh launch's generation and start its exit-watching thread."""
        with self._gen_lock:
            generation = self._generation.get(streamer, 0) + 1
            self._generation[streamer] = generation
        self._started_at[streamer] = time.time()
        threading.Thread(
            target=self._watch, args=(streamer, process, generation), daemon=True,
            name=f"watch-{self.label}-{streamer}",
        ).start()

    def _watch(self, streamer: str, process: Any, generation: int) -> None:
        """Block until process exits, then finalize unless a newer attempt has already replaced it."""
        while process.poll() is None and not self._stop_event.is_set():
            time.sleep(1)
        with self._gen_lock:
            if self._generation.get(streamer) != generation:
                return  # a stall/quality/split restart already relaunched this streamer
        output_path = self.site_state.get_in_progress_path(streamer)
        self.write_failure.on_process_exit(streamer, output_path, self._started_at.get(streamer, time.time()))
        self.coordinator.on_process_exit(self.label, streamer, self.site_state)
        self.segments.clear(streamer)
        activity_log.log_activity(self.label, f"{streamer} stopped recording")

    def _restart(self, streamer: str, path: str) -> None:
        """Shared restart callback for the stall/quality/segment monitors: relaunch at path."""
        effective = resolve_effective_config(self.data_dir, self.label, streamer, self.config)
        self._lq_disabled[streamer] = effective.lq_fallback_disabled
        lq = False if effective.lq_fallback_disabled else self.quality.is_lq_active(streamer)
        process = self.coordinator.restart_session(
            effective.config, streamer, self.site_state, path, lq=lq, on_line=self._make_on_line(streamer)
        )
        self._register_launch(streamer, process)

    def _on_live(self, streamer: str) -> None:
        """Checker callback: hand off to a worker thread so cooldown/delay never blocks polling."""
        threading.Thread(
            target=self._start_streamer, args=(streamer,), daemon=True, name=f"start-{self.label}-{streamer}"
        ).start()

    def _start_streamer(self, streamer: str) -> None:
        """Apply admission/schedule/intro-delay gating, then start a fresh recording session."""
        if self.site_state.is_recording(streamer):
            return
        self.site_state.mark_live(streamer)

        effective = resolve_effective_config(self.data_dir, self.label, streamer, self.config)

        if not within_schedule(effective.schedule):
            return
        if effective.intro_delay:
            time.sleep(effective.intro_delay)
        if self.site_state.is_recording(streamer):
            return
        if not may_start_recording(self.data_dir, self.app_state, self.app_cfg, self.label, streamer):
            return

        self._lq_disabled[streamer] = effective.lq_fallback_disabled
        lq = False if effective.lq_fallback_disabled else self.quality.is_lq_active(streamer)
        process = self.coordinator.start_session(
            self.label, effective.config, streamer, self.site_state, lq=lq, on_line=self._make_on_line(streamer)
        )
        self._register_launch(streamer, process)

        path = self.site_state.get_in_progress_path(streamer)
        split_minutes = effective.config.get("timing", {}).get("split_after_minutes", 0)
        if split_minutes and path:
            self.segments.start(streamer, path)
        self.stall.reset(streamer)
        self.quality.reset(streamer)
        self.site_state.reset_recording_flags(streamer)

        notif_cfg = effective.config.get("notifications", {})
        desktop.notify_recording_started(
            self.label, streamer, effective.popup_enabled,
            notif_cfg.get("popup_timeout_sec", 15), notif_cfg.get("popup_cooldown_sec", 240),
        )
        ntfy.notify_recording_started(
            self.label, streamer, effective.ntfy_enabled, self.app_cfg.notifications.ntfy_topic
        )
        activity_log.log_activity(self.label, f"{streamer} started recording")
