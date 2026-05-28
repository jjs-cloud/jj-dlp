# jj-dlp Testing Solution

A complete drop-in test harness for jj-dlp.  No real yt-dlp, no real streams,
no internet required.

---

## Directory layout

```
fake_ytdlp/
    fake_ytdlp.py          ← drop-in yt-dlp replacement
    fake_ytdlp.conf        ← active scenario configuration
    use_preset.py          ← one-command preset switcher
    fake_ytdlp_checker.sh  ← optional wrapper (checker role)
    fake_ytdlp_downloader.sh  ← optional wrapper (downloader role)
    presets/
        normal_stream.conf
        stall.conf
        ffmpeg_timestamp_discontinuity.conf
        ffmpeg_packet_corrupt.conf
        crash.conf
        slow_start.conf

tests/
    test_fake_ytdlp.py     ← pytest test suite
```

---

## Quick start

### 1. Point jj-dlp at fake_ytdlp

In your test `.conf` file set:

```ini
[General]
YT_DLP_PATH_LINUX   = /absolute/path/to/fake_ytdlp/fake_ytdlp.py
YT_DLP_PATH_WINDOWS = C:\path\to\fake_ytdlp\fake_ytdlp.py
YT_DLP_PATH_MAC     = /absolute/path/to/fake_ytdlp/fake_ytdlp.py
```

fake_ytdlp ignores every real yt-dlp flag.  It reads `fake_ytdlp.conf`
(in the same directory as `fake_ytdlp.py`) instead.

### 2. Set the mode

`fake_ytdlp.conf` has a `[Mode]` section:

```ini
[Mode]
mode = checker      # emit JSON liveness lines (checker phase)
# mode = downloader # write a fake recording file (downloader phase)
```

jj-dlp calls the same binary for both roles.  The simplest approach is to
**manually flip `mode`** before each test, or use the wrapper scripts
(see below).

### 3. Configure streamers

```ini
[Checker]
streamers_live = alice, bob   # comma-separated — these appear LIVE
```

Any streamer not in this list will appear offline.

---

## Activating a scenario

### Manual edit

Open `fake_ytdlp.conf` and toggle the flags you need.

### Preset switcher

```bash
python fake_ytdlp/use_preset.py stall
# Run jj-dlp...
python fake_ytdlp/use_preset.py normal_stream   # restore baseline
```

Available presets: `normal_stream`, `stall`, `ffmpeg_timestamp_discontinuity`,
`ffmpeg_packet_corrupt`, `crash`, `slow_start`.

---

## Scenarios

### Stall detection

**Goal:** confirm jj-dlp restarts a recording whose output file stops growing.

**How it works:** `fake_ytdlp` writes chunks to the output file, then simply
stops writing after `stall_after_seconds`.  The process **stays alive** —
it doesn't exit.  jj-dlp's stall detector (file-size polling in
`get_streamer_file_size`) must notice the growth has stopped and kill/restart
the process.

```ini
[Stall]
enabled             = True
stall_after_seconds = 15    # stop writing after 15 s
```

**Suggested jj-dlp settings to pair with this:**

```ini
STALL_TIMEOUT        = 20   # seconds without growth → restart
STALL_CHECK_INTERVAL = 5
```

Set `stall_after_seconds` < `STALL_TIMEOUT` so the stall starts before the
stream would end on its own.

---

### ffmpeg error restart

**Goal:** confirm jj-dlp restarts when `FFMPEG_ERROR_PATTERNS` lines accumulate
past `FFMPEG_ERROR_RESTART_THRESHOLD` (default 500).

```ini
[FfmpegErrors]
timestamp_discontinuity                  = True
timestamp_discontinuity_interval_seconds = 0.5   # one line every 0.5 s
timestamp_discontinuity_count            = 600    # emit 600 lines total
```

At 0.5 s/line × 500 lines = ~250 s before restart.  Speed up by lowering the
interval or lowering `FFMPEG_ERROR_RESTART_THRESHOLD` in `main.py`.

Both patterns can be enabled simultaneously.

---

### Crash / unexpected exit

**Goal:** confirm jj-dlp re-enters the check loop after the downloader exits
with a non-zero code.

```ini
[CrashTest]
enabled             = True
crash_after_seconds = 10
exit_code           = 1
```

---

### Slow file appearance

**Goal:** confirm `wait_for_streamer_file()` waits patiently and doesn't
false-positive a stall before the file even exists.

```ini
[SlowStart]
enabled       = True
delay_seconds = 8.0    # file doesn't appear for 8 s after launch
```

Make sure `STALL_TIMEOUT` > `delay_seconds` or the stall detector may fire
before the file appears.

---

## Using wrapper scripts (advanced)

If you want jj-dlp to call a different command for checker vs downloader
without touching `fake_ytdlp.conf` between runs, use the wrapper scripts:

```ini
# In your jj-dlp .conf:
[Checker]
--dump-json
/path/to/fake_ytdlp/fake_ytdlp_checker.sh

[Downloader]
--no-part
/path/to/fake_ytdlp/fake_ytdlp_downloader.sh
```

Each wrapper patches `mode` in `fake_ytdlp.conf`, runs the script, then
restores the original value.

---

## Automated tests

```bash
# Install pytest if needed
pip install pytest

# Run all tests
python -m pytest tests/test_fake_ytdlp.py -v

# Run only checker tests
python -m pytest tests/test_fake_ytdlp.py -v -k "Checker"

# Run only stall tests
python -m pytest tests/test_fake_ytdlp.py -v -k "Stall"

# Run only ffmpeg error tests
python -m pytest tests/test_fake_ytdlp.py -v -k "Ffmpeg"

# Run with live output (useful for timing-sensitive tests)
python -m pytest tests/test_fake_ytdlp.py -v -s
```

### Test groups

| Class | What it tests |
|---|---|
| `TestCheckerMode` | JSON output, live/offline flags, URL parsing |
| `TestDownloaderNormal` | File creation, growth, exit codes, progress lines |
| `TestStallDetection` | File growth stops, process stays alive, stderr message |
| `TestFfmpegErrors` | Error line counts, pattern matching, threshold |
| `TestCrashBehaviour` | Exit code, timing |
| `TestSlowStart` | File absent during delay, present after |
| `TestConfigEditorHelpers` | browser_config.py read/write helpers (in-process) |
| `TestLoggerModule` | logger.py debug log, filters, path helpers (in-process) |
| `TestLoadConfig` | load_config() against minimal .conf files (requires jj_dlp importable) |

---

## Extending fake_ytdlp

Add a new scenario by:

1. Adding config keys to `[YourSection]` in `fake_ytdlp.conf`.
2. Reading them in `_load_cfg()` in `fake_ytdlp.py`.
3. Implementing the behaviour in `_run_downloader()` or `_run_checker()`.
4. Adding a preset file in `presets/`.
5. Adding tests in `tests/test_fake_ytdlp.py`.

**Ideas for future scenarios:**

- `NetworkFlap` — write data, pause for N seconds, resume (simulates brief
  network interruption without a true stall)
- `RateThrottle` — vary write speed over time (tests dashboard progress display)
- `LargeFile` — write very fast to test split-recording logic
- `MultipleStreamers` — emit multiple checker JSON lines with mixed live/offline
  statuses
- `AuthError` — exit immediately with a specific stderr message (e.g.
  "Sign in to confirm you're not a bot")
- `GeoBlock` — exit with a geo-restriction message

---

## Testing non-subprocess functionality

Not everything in jj-dlp goes through yt-dlp.  For the rest:

| Component | Recommended approach |
|---|---|
| `browser_config.py` | `TestConfigEditorHelpers` — write temp conf, call functions directly |
| `logger.py` | `TestLoggerModule` — call `configure_debug_log()` / `dbg()` directly |
| `config_editor.py` / `ConfigEditor` | Instantiate with a mock dashboard; write temp conf files |
| `load_config()` | `TestLoadConfig` — write minimal .conf, assert returned dict |
| `updater.py` | Mock `urllib.request` or use `unittest.mock.patch` on the HTTP calls |
| Dashboard drawing | Use `curses.wrapper` in a pseudo-terminal (pty) or mock `stdscr` |
| `deps.py` | Mock `subprocess.run` to return fake version strings |

Use `unittest.mock.patch` from the standard library to replace any function
that makes real network or filesystem calls.
