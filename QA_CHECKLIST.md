# jj-dlp Manual QA Checklist

There is no automated test suite (Doc 1 §22) — this checklist is meant to
be run by hand after major milestones, across all three target platforms
where noted. Check off each item; note the platform(s) tested next to any
item marked Win/macOS/Linux.

## 0. Environments to cover

- [ ] Windows 10/11
- [ ] macOS (recent version)
- [ ] Linux (a common distro — Ubuntu/Debian/Fedora/Arch)

Run the full checklist on at least one platform, and re-run the
platform-specific sections (deps install, file ops, updater relaunch) on
the other two.

## 1. Fresh install, no dependencies present

- [ ] Remove/rename any existing data directory so this is a true fresh
      start.
- [ ] Launch the app with `ffmpeg` not on PATH. Confirm the dependency
      check detects it missing and offers a Y/N install prompt.
- [ ] Decline the install; confirm the app exits (or clearly warns) rather
      than proceeding silently with a broken engine.
- [ ] Re-launch, accept the install; confirm installer output streams
      live and `check()` passes afterward.
- [ ] **Windows only**: remove `windows-curses`, confirm the curses
      dependency is detected missing and `pip install windows-curses`
      succeeds via the same flow.
- [ ] **Linux**: confirm the correct package manager
      (apt-get/dnf/yum/pacman/zypper/apk) is auto-detected and `sudo` is
      prefixed only when not already root.
- [ ] **macOS**: confirm a clear failure message with instructions if
      Homebrew isn't installed, and a successful `brew install ffmpeg`
      when it is.
- [ ] On first successful launch, confirm `config/`, `schema/`, `state/`,
      and `logs/` are created under the chosen data directory with the
      expected shipped defaults (`config/app.json`, `config/theme.json`,
      `config/priority.json`, `schema/fields.json`).

## 2. First-run site creation (all three plugins)

For each plugin (`chatsite`, `twitch`, `tiktok-live`):

- [ ] Open the Config tab's create-new-site flow, pick the plugin, enter
      a unique label, confirm creation.
- [ ] Confirm the new `config/sites/<label>.json` matches that plugin's
      documented default overrides (Doc 1 §4.1/§4.2/§4.3) — spot-check a
      few fields (e.g. `url_template`, `lq_downloader.format`,
      `plugin_settings`).
- [ ] Confirm the per-site Config screen shows `site_fields` plus, for
      twitch, the extra `ad_alert_patterns`/`eventsub.*` fields — and
      that chatsite/tiktok-live do **not** show those extra fields.
- [ ] Add at least one streamer to each new site via the management
      overlay's Add flow.
- [ ] Confirm each site appears as its own panel on the Dashboard tab.

## 3. Full record → stop → File Manager cycle

- [ ] With a streamer that is actually live (or a stand-in URL that
      yt-dlp reports as live), confirm the checker detects it within one
      `check_interval` and recording starts automatically.
- [ ] Confirm the dashboard shows the recording glyph, a growing
      live-duration bar, and a status string.
- [ ] Confirm a desktop popup notification fires (if
      `notifications.popup_enabled`) and, if `ntfy_enabled`, that a push
      arrives.
- [ ] Let the stream end (or kill the source) and confirm the app marks
      the streamer not-recording and records the end time.
- [ ] Open the File Manager tab; confirm the finished file appears with
      correct size/modified time and IDLE state (WRITING while still
      recording, if checked mid-recording).
- [ ] Exercise each File Options action on that file: open, open
      containing folder, Fixup (with and without convert-to-mp4 /
      delete-original), Move (including adding a new destination on the
      fly and adjusting the filename in the Move-Filename popup), Trim,
      and Split — confirm each produces the expected output file(s) and
      background progress indication.
- [ ] Confirm collision-safe renaming: trigger a move/name collision and
      confirm `_2`, `_3`, etc. suffixes are applied instead of
      overwriting.
- [ ] Delete a test file via both trash mode and permanent-delete mode;
      confirm the correct one is used per the toggled mode.

## 4. Priority reordering and bypass

- [ ] Open the Priority tab; confirm every streamer across all loaded
      sites appears in one ordered list.
- [ ] Reorder a few entries up/down; confirm the change persists across
      a restart.
- [ ] Toggle bypass on one entry; confirm it sorts to the top and is
      shown with a distinct marker.
- [ ] Set `recording.max_concurrent` to a small nonzero number with more
      than that many streamers live at once; confirm bypass streamers all
      record regardless of the limit, and non-bypass streamers are
      admitted in priority order.

## 5. Each of the 7 streamer override popups

For a single test streamer, open each settings popup from the Priority
tab and confirm: the field shows "inherits: `<effective value>`" when
unset, editing and saving writes only that field as a non-null override
via `set_override`, the `*` marker appears next to the streamer, and
"Reset to default" (via the confirm-reset popup) clears it back to null.

- [ ] **Quality** — override `format`, and independently toggle
      `lq_fallback_disabled`.
- [ ] **Notifications** — independently override `popup_enabled` and
      `ntfy_enabled` (tri-state — confirm one can be set while the other
      still inherits).
- [ ] **Auto-Suffix** — toggle the boolean override.
- [ ] **Output Directory** — set a path override and confirm that
      streamer's next recording lands there instead of the site default.
- [ ] **Split** — override `enabled` and `split_after_minutes`.
- [ ] **Intro Delay** — set a nonzero seconds value and confirm recording
      start is delayed by roughly that long after going live.
- [ ] **Schedule** — restrict to specific days/hours and confirm the
      streamer is *not* picked up outside that window even if live.

## 6. Theme switching and custom role creation

- [ ] Open the Theme Manager; switch the active theme between `dark`,
      `light`, and `high_contrast`; confirm the whole UI recolors
      immediately and the choice persists across a restart.
- [ ] Create a new role with distinct fg/bg/bold via role_edit.py.
- [ ] Reassign one element (e.g.
      `dashboard.streamer.recording_dot`) to the new role via
      element_edit.py; confirm only that element's color changes,
      nothing else in the theme.
- [ ] Give a different element a direct per-element color override
      (bypassing roles entirely) and confirm it renders correctly and
      independently of any role.
- [ ] Attempt to delete a role still in use by an element; confirm it's
      refused rather than silently orphaning the element.
- [ ] Confirm the startup random-scheme popup (if enabled) offers a
      themed switch for the session without altering the persisted
      `active_theme`.
- [ ] With `ui.rgb_mode` on and a true-color-capable terminal, confirm
      OSC 4 palette colors are applied and restored cleanly on exit;
      with it off (or in a non-true-color terminal), confirm the
      portable curses color-pair fallback still looks correct.

## 7. Web UI (if enabled)

- [ ] Set `web_ui.enabled: true` with a user/pass, restart, and confirm
      the reachable URL is logged on startup.
- [ ] Confirm `GET /api/status` requires Basic Auth and returns a 401
      without it.
- [ ] Authenticate once; confirm a session cookie is issued and
      subsequent requests don't need to resend credentials until it
      expires.
- [ ] Confirm `GET /api/status` reflects the same state as the curses
      dashboard for every loaded site/streamer.
- [ ] Exercise `POST /api/streamers/add`, `/remove`, and `/disable`;
      confirm each has the identical effect as the corresponding curses
      management-overlay action, and that the curses UI picks up the
      change.
- [ ] Confirm a malformed JSON body returns 400, an unknown route
      returns 404, and disabling `web_ui.enabled` and restarting stops
      the server entirely.

## 8. EventSub for a Twitch site

- [ ] Configure `plugin_settings.eventsub.client_id`/`client_secret` and
      a reachable `callback_url`/`webhook_port` for a twitch-plugin site.
- [ ] Confirm an OAuth app token is fetched and cached, and refreshed
      automatically near expiry.
- [ ] Confirm subscriptions are created for each streamer's numeric user
      id, and the EventSub tab shows webhook server status, current
      subscriptions, and their state.
- [ ] Trigger a real (or simulated) `stream.online` webhook call; confirm
      the HMAC signature is verified, the challenge handshake responds
      correctly, and recording starts immediately (no poll wait).
- [ ] Restart the app with a streamer already live before its
      subscription existed; confirm `backfill.py` catches it via the
      Helix streams endpoint on startup.
- [ ] Stop/remove the twitch site and confirm
      `stop_background_service` tears down the webhook server and
      subscriptions cleanly.
- [ ] Confirm a non-twitch site's EventSub tab shows "Not applicable for
      this site."

## 9. Update flow (against a test release)

- [ ] Point `updates.update_branch` at a test branch/release with a
      known newer tag/commit; confirm `is_update_available()` reports
      true and `latest_sha_seen` is stored.
- [ ] Trigger the install flow; confirm the release zip downloads and
      extracts to a temp directory, the app relaunches with
      `--finish-update <path>`, and the fresh process copies files over
      the install directory without errors.
- [ ] Confirm `state/update_check.json`'s `installed_sha` updates and
      the app re-executes its normal entry point afterward.
- [ ] Confirm the changelog popup appears once (comparing
      `changelog_shown_for_version` against the new `installed_sha`) and
      does not reappear on a subsequent launch.
- [ ] Before updating, hand-edit a `preserve: true` field (e.g. a site's
      `timing.stall_timeout`) to a non-default value; after the update,
      confirm that value survived the merge while a non-preserved field
      reset to its new shipped default.
- [ ] Confirm `schema/fields.json` itself was fully replaced by the new
      shipped copy, not merged.

## 10. Cross-cutting robustness checks

- [ ] Corrupt a `config/*` file by hand (truncate or inject invalid
      JSON) and restart; confirm a clear warning is logged, the newest
      backup is loaded instead, and the app keeps running.
- [ ] With no valid backup available, confirm the app falls back to
      plugin/schema defaults for that one file and warns loudly instead
      of crashing.
- [ ] Corrupt or delete a `state/*` file and restart; confirm it's
      silently rebuilt as empty/default with no warning needed.
- [ ] Rename a site; confirm every reference in `config/priority.json`
      and `state/*.json` updates to the new label.
- [ ] Delete a site; confirm its `priority.json` entries and state
      entries are cleaned up (or offered for cleanup) rather than left
      orphaned.
- [ ] Trigger a stall (freeze a recording's file growth) and confirm
      `stall_since` is set and the recording restarts after
      `stall_timeout`.
- [ ] Force enough ffmpeg errors to cross `ff_err_threshold`; confirm the
      streamer falls back to `lq_downloader`, and — with
      `upgrade_quality` true — later attempts a restart back to full
      quality.
- [ ] Simulate a write failure (e.g. point `output.dir` at a
      non-writable path); confirm the persistent write-failure banner
      appears and stays set until dismissed, even after a later
      successful attempt.
- [ ] With a streamer recording, request quit; confirm the exit
      confirmation popup states how many recordings will stop and
      requires explicit confirmation before killing them.
- [ ] Kill the app forcibly (SIGKILL-equivalent) mid-recording and
      restart; confirm no corrupted config files result (atomic writes
      held), and confirm the single-instance lock correctly refuses a
      second concurrent instance while the first is still running.
- [ ] **Windows**: confirm closing the console window terminates all
      child yt-dlp/ffmpeg processes (Job Object behavior) rather than
      orphaning them.
- [ ] On December 25 (or with the system clock set to that date),
      confirm the dashboard's festive easter-egg decoration appears.
- [ ] Resize the terminal during normal operation; confirm every tab's
      layout recomputes cleanly with no drawing artifacts.
