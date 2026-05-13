## Quick Start Guide:
  
(Assumes you already have [python](https://www.python.org/downloads/) installed)

Step 1:  Download jj-dlp by clicking the green "Code" button, then "Download ZIP"

Step 2:  Extract the zip, and double click on jj-dlp.py to run it. 

Step 3:  When you see the jj-dlp dashboard, you can add your favorite streamers by pressing the A key.

That's it!  Your streamers will be recorded automatically.

---

# jj-dlp

jj-dlp is a app that allows you to automatically record live streams, powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp). 

---

## Technical Features

- **Dashboard** — Manage your streamer monitoring and recording from a nice dashboard.
- **Stall detection** — restarts yt-dlp if the download stalls (common issue with yt-dlp)
- **Error detection** — restarts yt-dlp if ffmpeg recieves certain errors (common issue with ffmpeg)
- **Small footprint** — only needs to run 1 yt-dlp process when checking for live streams
- **Twitch API** — Optionally connect your Twitch API credentials for faster triggering. 
- **Dependency Resolution** — You will be prompted to install ffmpeg and windows-curses if needed.
  
---

## Requirements

- [python](https://www.python.org/downloads/)

---

## Advanced Configuration

The script uses a `.conf` file for each streaming website.  Most settings can be configured from within the app in the "Config" tab, and some must be manually configured by editing the `.conf` file in a text editor.

At startup, the script will scan the current folder for `.conf` files and prompt you to choose the ones you want to use.

If you want to skip this step, you can specify the config file(s) with  `--config` option.  

Example: `python jj-dlp.py --config twitch.conf tiktok-live.conf`

---

## Usage

```bash
# Normal usage
python jj-dlp.py

# Specify a config file
python jj-dlp.py --config C:\path\to\my.conf
```


---

