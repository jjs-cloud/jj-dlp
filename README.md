## Quick Start Guide:

Step 1:  Download jj-dlp.py and one of the conf files (right click > save link as)

Step 2:  Put the names of the streamers you want to record in the [Streamers] section of the conf file.

Step 3:  Run "py jj-dlp.py --config twitch.conf" (or whatever conf file you chose) in the command prompt

  Note:  If "py" doesn't work, try "python" or "python3"

---

# jj-dlp

jj-dlp is a [yt-dlp](https://github.com/yt-dlp/yt-dlp) wrapper that allows you to automatically record live streams. 

---

## Features

- **Multi-streamer monitoring** — watches/records any number of channels concurrently
- **Stall detection** — restarts yt-dlp if the download stalls
- **Hot config file** — start and stop recordings via the config file while the script is still running
- **Verbosity modes** — Hide yt-dlp and/or ffmpeg output with the press of a button. (v and o)
- **Cross-platform** — works on Linux, Windows, and Mac (probably)

---

## Requirements

- [Python](https://www.python.org/downloads/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) Must be in your system `PATH`, or you can specify the path in the jj-dlp config file

---

## Configuration

The script is driven by a `.conf` file. By default it looks for `jj-dlp.conf` in the current directory.  You can also use the `--config` option to specify a `.conf` file.

### Example `jj-dlp.conf`

```ini
[General]
# URL template for the streaming site. {username} is replaced with each streamer's name.
SITE_TMPL           = https://www.twitch.tv/{username}

# Directory where recordings are saved (absolute or relative)
OUTPUT_DIR          = recordings

# yt-dlp output filename template
OUTPUT_TMPL         = %(title)s [%(id)s].%(ext)s

# How often (seconds) to check if streamers are live
CHECK_INTERVAL      = 60

# Seconds to wait after a recording ends before the next check
COOLDOWN_AFTER_RECORDING = 5

# How often (seconds) to check the output file size for stall detection
STALL_CHECK_INTERVAL = 30

# Seconds of no file growth before a stall is declared and yt-dlp is restarted
STALL_TIMEOUT       = 120

# How often (seconds) the config file is polled for changes
CONFIG_CHECK_INTERVAL = 3

# Verbosity: 1=normal, 2=debug, 3=both
VERBOSITY           = 1

# Enable logging to file (true/false)
LOGGING             = false
LOG_PATH            = recordings/jj-dlp.log

# Split stdout and stderr into separate log files (true/false)
SPLIT_LOGS          = false

# Path to yt-dlp binary (leave blank to use system PATH)
YT_DLP_PATH         =

[Streamers]
# One streamer username per line
streamer1
streamer2

[Block]
# Streamers listed here are skipped / stopped immediately
streamer3

[Checker]
# yt-dlp arguments used when checking live status
--dump-json

[Downloader]
# yt-dlp arguments used when downloading
--fixup never
--no-part
```

---

## Usage

```bash
# Use the default jj-dlp.conf in the current directory
python jj-dlp.py

# Specify a custom config file
python jj-dlp.py --config C:\\path\\to\\my.conf
```

Press **Ctrl+C** to stop. Active recordings are given up to 15 seconds to finish gracefully before the process exits.

---

## How It Works

1. **Main loop** — loads the config, queries yt-dlp for each streamer's live status, and starts a recording thread for any streamer that is live.
2. **Recording thread** — runs yt-dlp as a subprocess, monitors the output file size, and restarts yt-dlp automatically if a stall is detected.
3. **Config watcher thread** — polls the config file every `CONFIG_CHECK_INTERVAL` seconds and triggers an immediate live check when new streamers are added.
4. **Block list** — checked on every config reload during an active recording; a blocked streamer's process is killed immediately.

---

## Verbosity Levels

| Value | Output |
|-------|--------|
| `1`   | Normal messages only |
| `2`   | Debug messages only  |
| `3`   | Normal + debug messages |

---

## License

MIT
