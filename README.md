# jj-dlp

A multi-site stream recorder built on [yt-dlp](https://github.com/yt-dlp/yt-dlp),
with a curses terminal UI, an optional read-only/write-lite web UI, priority
and override control over which streamers record when, and per-site plugins
for chatsite, Twitch, and TikTok Live.

## Installing

### Requirements

- Python 3.9 or newer.
- `ffmpeg` on your `PATH` (jj-dlp will offer to install it for you on first
  run if it's missing — see "First run" below).
- Windows only: the `windows-curses` package (jj-dlp offers to install this
  too on first run).

### From a release

1. Download the latest release zip from the Releases page.
2. Unzip it anywhere.
3. Install it into a Python environment:

   ```
   pip install ./jj-dlp
   ```

4. Run it:

   ```
   jj-dlp
   ```

### From source (development)

```
git clone <repo-url>
cd jj-dlp
pip install -e .
jj-dlp
```

### First run

On first launch, jj-dlp checks for `ffmpeg` and (on Windows) `curses`
support. If either is missing, it will ask permission before installing
anything — nothing is installed silently. It then creates its data
directory (see below) with a fresh `config/app.json`, `schema/fields.json`,
and the three built-in themes, and walks you through picking which sites to
load for the session.

## Data directory

jj-dlp keeps everything it writes — `config/`, `schema/`, `state/`,
`logs/`, and `recordings/` — inside a single `userdata/` folder created
next to the installed `jj_dlp/` package (i.e. alongside `bin/` and
`pyproject.toml`), not in any platform-specific home/AppData location.
Nothing is ever written outside this folder (or wherever `--data-dir`
points instead). Override it with `--data-dir <path>` on any platform.

## Platform-specific notes

- **Windows** — `ffmpeg` is installed via `winget`; you may need to
  restart your terminal (or log out/in) afterward for `PATH` to pick it
  up. Curses support comes from the `windows-curses` package, installed
  automatically if missing.
- **macOS** — `ffmpeg` is installed via Homebrew; if Homebrew itself
  isn't installed, jj-dlp will tell you and stop rather than trying to
  install Homebrew for you.
- **Linux** — `ffmpeg` is installed via whichever of
  `apt-get`/`dnf`/`yum`/`pacman`/`zypper`/`apk` is detected, `sudo`-prefixed
  if you're not already root.

## How updates work

jj-dlp checks GitHub Releases in the background (governed by
`config/app.json`'s `updates.check_for_updates`, `update_interval_min`, and
`update_branch`). When an update is available and you accept it, jj-dlp:

1. Downloads and extracts the release zip to a temp directory.
2. Relaunches itself with `--finish-update <path>`, since the running
   process can't safely overwrite its own open files.
3. The freshly-spawned process copies the extracted package files over the
   install directory, replaces `schema/fields.json` with the newly shipped
   version, merges your existing settings back onto it (any field marked
   `preserve: true` keeps your value; everything else takes the new
   default), and records the new `installed_sha`.
4. The app then restarts normally, showing a changelog popup once per
   version if the release notes changed.

Updating never touches your recordings, and never merges/preserves
anything in `schema/fields.json` itself — that file is always fully
replaced, since it describes the app's own config surface, not your data.

## Release packaging (for maintainers)

`core/updater/install.py` doesn't install a wheel — it copies files
straight over the existing install directory, so a release zip's layout
must match what it expects:

- The zip must contain a `jj_dlp/` package directory either at its root,
  or exactly one level deep (this also makes GitHub's auto-generated
  `zipball_url` source archives, which nest everything under a
  `<repo>-<sha>/` folder, work without any special-casing).
- Everything under that `jj_dlp/` directory is copied over the running
  install's `jj_dlp/` directory, overwriting existing files — so the zip
  should contain the full package (including `core/config/
  shipped_fields.json`), not just changed files.
- `pyproject.toml` and anything outside `jj_dlp/` is not copied by the
  updater and doesn't need to be in the zip at all; it only matters for a
  fresh `pip install`.
- The GitHub release's tag name is what `updates.update_branch` and the
  updater's tag lookup compare against — tag releases consistently (e.g.
  `v0.2.0`) and keep `jj_dlp/__init__.py`'s `__version__` in sync with it.

`scripts/build_release.py` builds a zip in this shape from a checkout:

```
python scripts/build_release.py
```

This writes `dist/jj-dlp-<version>.zip` (version read from
`jj_dlp.__version__`), containing just the `jj_dlp/` package directory at
the zip's root. Attach that file as a release asset when cutting a
release; the updater looks for the first `.zip` asset on the release,
falling back to GitHub's automatic zipball if none is attached.

## Where your data lives

Everything jj-dlp reads or writes lives under one data directory (see
"First run" above for the per-platform default, or pass `--data-dir`):

```
config/            precious, user-facing settings — backed up automatically
  app.json           global app settings
  theme.json          active theme + all theme definitions
  priority.json         cross-site streamer order, bypass, and overrides
  sites/<label>.json      one file per site; the filename is that site's identity
  .backups/                 last 5 timestamped copies of each config/* file
schema/
  fields.json        app-owned description of every config field; replaced
                      wholesale on every update, never hand-preserved
state/             disposable runtime cache — safe to delete, gets rebuilt
  live_status.json    last-live times, currently-live flags, segment tracking
  disk_history.json     rolling disk-usage/write-rate samples for the graph
  update_check.json       last update check result
  eventsub.json             Twitch EventSub subscription/status cache
logs/              debug.log and app.log, plus crash logs
recordings/        default output location (configurable per site)
```

**Safe to hand-edit:** anything under `config/` — it's validated on load,
and a bad edit falls back to the newest backup (or defaults) with a logged
warning rather than crashing. jj-dlp only reads config at startup and after
its own writes, so a hand edit while the app is running won't be picked up
until you restart or use the Config tab's "Reload from disk" action.

**Not safe to hand-edit:** `schema/fields.json` — it's overwritten on every
update and isn't part of the preserve/merge logic; any local changes to it
will be lost.

**Safe to delete anytime:** everything under `state/`. jj-dlp treats a
missing or corrupt state file as empty and rebuilds it silently.
