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
- **Priority System** — For slow connections, and/or to conserve disk space, you can limit the amount of simultaneous recordings and record only your highest priority streamers.
- **Stall detection** — restarts yt-dlp if the download stalls (common issue with yt-dlp)
- **Error detection** — restarts yt-dlp if ffmpeg recieves certain errors (common issue with ffmpeg)
- **Small footprint** — only needs to run 1 yt-dlp process when checking for live streams
- **Twitch API** — Optionally connect your Twitch API credentials for faster triggering. 
- **Dependency Resolution** — You will be prompted to install ffmpeg and windows-curses if needed.
  
---

## Requirements

- [python](https://www.python.org/downloads/)
- [windows-curses](https://pypi.org/project/windows-curses/) (or curses on linux/mac)

---


