"""
jj-dlp — DEBUG simulation helpers.

These flags and functions let real production code paths (stall detection,
LQ restart, quality-upgrade restart, write failures) be exercised end-to-end
without a genuinely degraded/upgraded/broken stream. Every flag defaults to
off (except where individually noted); flip a flag, exercise the behavior
through the app, then flip it back off when done.
"""
import os
import threading
from typing import TYPE_CHECKING, Dict, Optional

from .logger import dbg

if TYPE_CHECKING:
    from .main import SiteState


# ── DEBUG: write-failure simulation ─────────────────────────────────────────
# Flip to True to make recordings fail to write: a plain FILE is pre-created at
# the output path (OS-level, cross-platform), so yt-dlp can't create anything
# under it. Watch for the marker/alert after ~one stall_timeout. Restore to
# False and delete the sentinel file (named below) when done.
_SIMULATE_WRITE_FAILURE = False
_SIMULATE_WRITE_FAILURE_BLOCKER_NAME = "_simulated_write_failure_do_not_create"

# ── DEBUG: stall simulation ─────────────────────────────────────────────────
# Flip to True to make a recording look stalled (file exists, stops growing):
# real growth proceeds until the stall checker arms (growth_seen), then the size
# is frozen so the stall timer runs down to a restart. Keyed by filename, so a
# SPLIT_AFTER segment change restarts the freeze. Watch for the [STALL] restart
# after ~one stall_timeout.
_SIMULATE_STALL = False
_simulate_stall_sizes: dict = {}

# Makes the stall permanent: after the first stall, reported size is pinned at 0,
# so restarted files can never grow (otherwise each restart regrows once and the
# cycle repeats forever). Whole-process — leave both flags off when done.
_SIMULATE_STALL_PERMANENT = False
# Internal latch: once True, reported size is pinned at 0 for the rest of the process.
_simulate_stall_permanent_lock = False

# ── DEBUG: collapse simulation ──────────────────────────────────────────────
# Flip to True to make a recording look like it got truncated/reopened mid-
# write (e.g. yt-dlp reopening the output file from byte 0 after a live-
# stream reconnect instead of resuming). Real growth proceeds until the file
# has grown past _SIMULATE_COLLAPSE_MIN_BYTES, then exactly once the
# reported size is knocked back down to a small value — a single
# poll-to-poll DECREASE, not a freeze — so the very next stall check in
# record_stream() sees `current_size < last_size` and logs the collapse
# warning (no restart — by the time a shrink is observed the data is already
# lost, so this only surfaces the problem). Keyed by filename, so a
# SPLIT_AFTER segment change gets its own one-shot collapse. Watch for
# "[STALL] COLLAPSE DETECTED" / "Warning: recording file for ... shrank
# from ...". Flip back to False when done.
_SIMULATE_COLLAPSE = False
_SIMULATE_COLLAPSE_MIN_BYTES = 2_000_000   # let it grow a bit first so the drop is obvious
_SIMULATE_COLLAPSE_DROP_TO = 65536         # fake "just reopened" size after the collapse
_simulate_collapse_triggered: dict = {}    # filename -> already fired for this file

# ── DEBUG: LQ-restart simulation ────────────────────────────────────────────
# Flip to True to run the real LQ restart path (ffmpeg_error_event →
# _maybe_trigger_lq → evict + record_stream(use_lq=True)) via injected errors.
# Needs LQ_DOWNLOADER=true and ≥2 live streamers with [LQ_Downloader] sections;
# the first growth-confirmed recording is the "storm" whose errors climb once a
# 2nd recording is armed. One-shot per process. Watch for "Bandwidth save:
# stopping ..." / "Recording started: ... [LQ]". Flip back to False when done.
_SIMULATE_LQ_RESTART = False
_SIMULATE_LQ_ERRORS_PER_TICK = 25   # injected per ~1s loop tick → 200 threshold in ~8s
_SIMULATE_LQ_SEED_ERRORS = 3        # seeds "another recording has ffmpeg errors"
# Internal state (do not edit): storm claim latch/name, armed-streamer set, lock.
_simulate_lq_storm_claimed = False
_simulate_lq_storm_streamer = ""
_simulate_lq_armed: set = set()
_simulate_lq_lock = threading.Lock()

# ── DEBUG: no-sidecar simulation ─────────────────────────────────────────
# Flip to True to reproduce a real incident: yt-dlp's --print-to-file
# filename sidecar silently never gets created (the log shows yt-dlp
# claiming it wrote to it, but the file never appears). The
# --print-to-file args are stripped from the launch command before Popen,
# so the sidecar truly never exists and record_stream() falls through its
# real fallback chain (JSON parsing, then eventually the directory-scan
# recovery). Flip back to False when done.
_SIMULATE_NO_SIDECAR = False


def maybe_strip_sidecar_args(cmd: list, sidecar_path: str, streamer: str) -> list:
    """Strip the --print-to-file args pointing at sidecar_path from cmd.

    See the _SIMULATE_NO_SIDECAR flag above. No-op (returns cmd unchanged)
    unless armed.
    """
    if not _SIMULATE_NO_SIDECAR:
        return cmd
    out = []
    i = 0
    stripped = 0
    while i < len(cmd):
        if (cmd[i] == "--print-to-file" and i + 2 < len(cmd)
                and cmd[i + 2] == sidecar_path):
            i += 3
            stripped += 1
            continue
        out.append(cmd[i])
        i += 1
    if stripped:
        dbg(f"[SIMULATE_NO_SIDECAR] stripped {stripped} --print-to-file "
            f"arg(s) for {streamer!r} — sidecar will never be created",
            site_name=streamer)
    return out


# ── DEBUG: quality-upgrade simulation ──────────────────────────────────────
# Flip to True to run the real UPGRADE_QUALITY path (_check_quality_upgrades
# → evict_and_restart) by injecting a higher resolution into the checker's
# json output once the recording is running at its real baseline. Needs
# UPGRADE_QUALITY=true in the site config; one-shot per live session. Watch
# for "Quality upgrade detected: Xp -> Yp — restarting recording". Flip
# back to False when done.
_SIMULATE_QUALITY_UPGRADE = False
_SIMULATE_QUALITY_UPGRADE_HIGH = 1440   # fake "source upgraded to" height (Twitch 1440p tier)
_SIMULATE_QUALITY_UPGRADE_LOW  = 480    # fake baseline seeded if checker reported no resolution at start
# Internal state (do not edit): claim latch/name, baseline-ready latch, lock.
_simulate_quality_upgrade_claimed = False
_simulate_quality_upgrade_target = ""
_simulate_quality_upgrade_ready = False
_simulate_quality_upgrade_lock = threading.Lock()


def get_write_failure_output_path(output_dir: str, current_output_tmpl: str) -> str:
    """Return the output path record_stream() should use for this segment.

    See the _SIMULATE_WRITE_FAILURE flag above. No-op (returns the normal
    path) unless armed; when armed, ensures the blocker file exists and
    returns a path "under" it so the downloader's write is guaranteed to fail.
    """
    if not _SIMULATE_WRITE_FAILURE:
        return os.path.join(output_dir, current_output_tmpl)

    _blocker_path = os.path.join(output_dir, _SIMULATE_WRITE_FAILURE_BLOCKER_NAME)
    if not os.path.exists(_blocker_path):
        try:
            with open(_blocker_path, "wb"):
                pass
            dbg(f"[SIMULATE_WRITE_FAILURE] created blocker file at {_blocker_path!r}")
        except Exception as _blocker_exc:
            dbg(f"[SIMULATE_WRITE_FAILURE] could not create blocker file: {_blocker_exc!r}")
    return os.path.join(_blocker_path, current_output_tmpl)


def maybe_freeze_stall_size(filename, size, last_growth_time, stall_timeout, streamer) -> int:
    """Apply _SIMULATE_STALL freezing to a just-read file size.

    See the _SIMULATE_STALL flag above. No-op (returns *size* unchanged)
    unless armed.
    """
    global _simulate_stall_permanent_lock
    if not _SIMULATE_STALL:
        return size

    # Permanent mode: once a stall has already been detected (setting the
    # process-wide latch), every subsequent read is pinned to 0 bytes —
    # including the pre-arm init lookup — so a restarted file can never
    # show any growth. Force this before the armed/freeze logic below so
    # nothing can re-arm or regrow afterward.
    if _SIMULATE_STALL_PERMANENT and _simulate_stall_permanent_lock:
        return 0

    # See the _SIMULATE_STALL flag description above. We only freeze
    # the reported size once the caller has *armed* the stall checker,
    # which it does by passing last_growth_time/stall_timeout (it only
    # does that after growth_seen has flipped true). Before that, real
    # sizes pass through so growth can be observed and the checker armed
    # exactly as usual. Once armed, report a fixed byte count forever,
    # so `current_size > last_size` in record_stream() never triggers
    # and the armed stall timer runs down to a restart.
    if last_growth_time is not None and stall_timeout is not None:
        _frozen = _simulate_stall_sizes.get(filename)
        if _frozen is None and size > 0:
            _frozen = size
            _simulate_stall_sizes[filename] = _frozen
            dbg(f"[SIMULATE_STALL] armed — freezing reported size at "
                f"{_frozen} bytes for {filename!r}", site_name=streamer)
        if _frozen is not None:
            size = _frozen
    return size


def maybe_collapse_stall_size(filename, size, growth_seen, streamer) -> int:
    """Apply a one-shot _SIMULATE_COLLAPSE truncation to a just-read file size.

    See the _SIMULATE_COLLAPSE flag above. No-op (returns *size* unchanged)
    unless armed. Unlike maybe_freeze_stall_size (which pins the size flat to
    simulate a stall), this fires exactly once per filename: once real growth
    has pushed the file past _SIMULATE_COLLAPSE_MIN_BYTES, the very next call
    reports a much smaller size instead, so the caller observes a genuine
    poll-to-poll decrease — the same shape as yt-dlp truncating/reopening the
    output file after a reconnect.
    """
    if not _SIMULATE_COLLAPSE or not growth_seen:
        return size

    already_fired = _simulate_collapse_triggered.get(filename, False)
    if already_fired:
        return size

    if size >= _SIMULATE_COLLAPSE_MIN_BYTES:
        _simulate_collapse_triggered[filename] = True
        dbg(f"[SIMULATE_COLLAPSE] triggering simulated collapse for {filename!r}: "
            f"reporting size as {_SIMULATE_COLLAPSE_DROP_TO} bytes instead of "
            f"real {size} bytes (one-shot)", site_name=streamer)
        return _SIMULATE_COLLAPSE_DROP_TO
    return size


def maybe_latch_stall_permanent(streamer: str) -> None:
    """Engage the permanent-stall latch after the first real stall detection.

    See the _SIMULATE_STALL_PERMANENT flag above. No-op unless armed.
    """
    global _simulate_stall_permanent_lock
    if _SIMULATE_STALL and _SIMULATE_STALL_PERMANENT and not _simulate_stall_permanent_lock:
        # First real stall detected — latch permanent mode so every
        # subsequent (restarted) file is reported at a fixed 0 bytes.
        _simulate_stall_permanent_lock = True
        dbg("[SIMULATE_STALL] permanent latch engaged — "
            "restarted files will be pinned at 0 bytes", site_name=streamer)


def _maybe_simulate_lq_errors(streamer: str, site: "SiteState", use_lq: bool,
                              growth_seen: bool, ffmpeg_error_counter: list,
                              ffmpeg_error_event: threading.Event,
                              error_threshold: int) -> None:
    """Inject simulated ffmpeg errors to drive the LQ-restart path.

    See the _SIMULATE_LQ_RESTART flag above. No-op unless armed; one recording
    (the "storm") climbs to *error_threshold* (the caller's live
    FFMPEG_ERROR_RESTART_THRESHOLD value) while the rest get a small error
    seed. Uses site.set_ffmpeg_error_count() so the dashboard's ffmpeg-errors
    section shows the count climbing like a real degraded stream.
    """
    global _simulate_lq_storm_claimed, _simulate_lq_storm_streamer
    if not _SIMULATE_LQ_RESTART:
        return
    if use_lq or not growth_seen or ffmpeg_error_event.is_set():
        return

    with _simulate_lq_lock:
        _simulate_lq_armed.add(streamer)
        if not _simulate_lq_storm_claimed:
            # The first recording to confirm growth is the storm.
            _simulate_lq_storm_claimed = True
            _simulate_lq_storm_streamer = streamer
            dbg(f"[SIMULATE_LQ] storm streamer claimed: {streamer!r} "
                f"(error count will climb once a 2nd recording arms)",
                site_name=streamer)
        is_storm = (streamer == _simulate_lq_storm_streamer)

    if is_storm:
        # Wait until at least one OTHER recording has also armed+seeded so
        # _maybe_trigger_lq's condition 1 can be satisfied.
        if len(_simulate_lq_armed) < 2:
            return
        ffmpeg_error_counter[0] += _SIMULATE_LQ_ERRORS_PER_TICK
        if ffmpeg_error_counter[0] > error_threshold:
            ffmpeg_error_counter[0] = error_threshold
        site.set_ffmpeg_error_count(streamer, ffmpeg_error_counter[0])
        dbg(f"[SIMULATE_LQ] storm {streamer!r} injected errors: "
            f"count={ffmpeg_error_counter[0]}/{error_threshold}",
            site_name=streamer)
        if ffmpeg_error_counter[0] >= error_threshold:
            dbg(f"[SIMULATE_LQ] storm {streamer!r} reached threshold — "
                f"setting ffmpeg_error_event", site_name=streamer)
            ffmpeg_error_event.set()
    else:
        # Seed other growth-confirmed recordings so condition 1 passes. Refresh
        # every tick so the 300s last_ffmpeg_error window stays valid until the
        # storm actually fires.
        site.set_ffmpeg_error_count(streamer, _SIMULATE_LQ_SEED_ERRORS)
        dbg(f"[SIMULATE_LQ] seeded {streamer!r} with "
            f"{_SIMULATE_LQ_SEED_ERRORS} ffmpeg errors (for LQ trigger gate)",
            site_name=streamer)


def _maybe_simulate_quality_upgrade(site: "SiteState",
                                    live_info: Dict[str, Optional[int]]) -> None:
    """Inject a fake higher resolution into the checker's live_info dict so
    the real UPGRADE_QUALITY machinery fires without the source genuinely
    switching.

    See the _SIMULATE_QUALITY_UPGRADE flag above. No-op unless armed; one
    recording (the first not-yet-upgraded active one) is claimed, then once
    it's been recording with an established baseline for one full check cycle
    its checker-reported height is faked above that baseline so
    _check_quality_upgrades restarts it "at the higher quality". One-shot per
    live session — the site's quality_upgraded flag gates repeats.
    """
    global _simulate_quality_upgrade_claimed, _simulate_quality_upgrade_target
    global _simulate_quality_upgrade_ready
    if not _SIMULATE_QUALITY_UPGRADE:
        return
    with site.lock:
        active = set(site.currently_recording) - site.evicted_streamers
        baselines = dict(site.recording_resolution)
    active = {s for s in active if not site.was_quality_upgraded(s)}
    if not active:
        return

    inject_height = None
    with _simulate_quality_upgrade_lock:
        if _simulate_quality_upgrade_claimed:
            # Self-heal: if the claimed target's recording ended before we
            # could fire, release the claim so the next recording is picked up.
            if _simulate_quality_upgrade_target not in active:
                _simulate_quality_upgrade_claimed = False
                _simulate_quality_upgrade_ready = False
        if not _simulate_quality_upgrade_claimed:
            _simulate_quality_upgrade_target = sorted(active)[0]
            _simulate_quality_upgrade_claimed = True
            _simulate_quality_upgrade_ready = False
            dbg(f"[SIMULATE_QUALITY_UPGRADE] target claimed: "
                f"{_simulate_quality_upgrade_target!r}", site_name=_simulate_quality_upgrade_target)
        target = _simulate_quality_upgrade_target

        old_height = baselines.get(target)
        if old_height is None:
            # No baseline yet (checker reported no resolution at start) — seed
            # a low one so the real check establishes it this cycle; the jump
            # fires next cycle.
            inject_height = _SIMULATE_QUALITY_UPGRADE_LOW
        elif not _simulate_quality_upgrade_ready:
            # Baseline exists; hold one full check cycle so the low-res
            # recording is genuinely running before we claim the upgrade.
            _simulate_quality_upgrade_ready = True
            dbg(f"[SIMULATE_QUALITY_UPGRADE] {target!r} recording at "
                f"{old_height}p — will fake upgrade on next check",
                site_name=target)
        else:
            inject_height = max(_SIMULATE_QUALITY_UPGRADE_HIGH, old_height + 360)

    if inject_height is not None:
        live_info[target] = inject_height
        if inject_height != _SIMULATE_QUALITY_UPGRADE_LOW:
            dbg(f"[SIMULATE_QUALITY_UPGRADE] injecting higher resolution for "
                f"{target!r}: json now reports {inject_height}p "
                f"(baseline {old_height}p)", site_name=target)
