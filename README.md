## Quick Start Guide:
  
(Assumes you already have [python](https://www.python.org/downloads/) and [yt-dlp](https://github.com/yt-dlp/yt-dlp))

Step 1:  Download jj-dlp by clicking the green "Code" button, then "Download ZIP"

Step 2:  Extract the zip, and double click on jj-dlp.py to run it.  

Note: You might need to run it with one of the following commands:
```
py jj-dlp.py
python jj-dlp.py
python3 jj-dlp.py
```

---

# jj-dlp

jj-dlp is a [yt-dlp](https://github.com/yt-dlp/yt-dlp) wrapper that allows you to automatically record live streams. 

---

## Features

- **Dashboard controls** — Add and remove streamers straight from the dashboard.
- **Stall detection** — restarts yt-dlp if the download stalls
- **Error detection** — restarts yt-dlp if ffmpeg recieves certain errors
- **Small footprint** — only needs to run 1 yt-dlp process when checking for live streams
- **Twitch API** — Optionally connect your Twitch API credentials for faster triggering
- 
---

## Requirements

- [python](https://www.python.org/downloads/)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) Must be in your system `PATH`, or you can specify the path in the `.conf` file

---

## Configuration

The script is driven by a `.conf` file.  At startup, it will scan the folder for `.conf` files and prompt you to choose one.
You can also use the `--config` option to specify a `.conf` file.

### Example `twitch.conf`

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
# Normal usage
python jj-dlp.py

# Specify a config file
python jj-dlp.py --config C:\path\to\my.conf
```

Press **Ctrl+C** to stop. Active recordings are given up to 15 seconds to finish gracefully before the process exits.

---

## How It Works

1. **Main loop** — loads the config, queries yt-dlp for each streamer's live status, and starts a recording thread for any streamer that is live.
2. **Recording thread** — runs yt-dlp as a subprocess.
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
