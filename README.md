## Quick Start Guide:
  
(Assumes you already have [python](https://www.python.org/downloads/) installed)

Step 1:  Download jj-dlp by clicking the green "Code" button, then "Download ZIP"

Step 2:  Extract the zip, and double click on jj-dlp.py to run it. 

Step 3:  When you see the jj-dlp dashboard, you can add your favorite streamers by pressing the A key.

That's it!  Your streamers will be recorded automatically.

---

# jj-dlp

jj-dlp is a [yt-dlp](https://github.com/yt-dlp/yt-dlp) dashboard that allows you to automatically record live streams. 

---

## Features

- **Dashboard** — Manage your streamer monitoring and recording from a nice dashboard.
- **Small footprint** — only needs to run 1 yt-dlp process when checking for live streams
- **Schedule recordings** — record a stream only on a specific day or a specific time.
- **Split recordings** — automatically split recordings into the duration of your choosing, without the need to wait until the end of the stream.
- **Mobile notifications** — Get notifications on your phone/device when a recording starts.
- **Priority System** — For slow connections, and/or to conserve disk space, you can limit the amount of simultaneous recordings and record only your highest priority streamers.
- **Stall detection** — Restarts yt-dlp if the download stalls (common issue with yt-dlp)
- **Error detection** — Restarts yt-dlp if ffmpeg recieves certain errors (common issue with ffmpeg).  Optionally restart yt-dlp in low quality mode.
- **Adjustable Quality** — Choose to record in full quality or low quality.  
- **Twitch API** — Optionally connect your Twitch API credentials for faster triggering. 
- **Dependency Resolution** — You will be prompted to install ffmpeg and windows-curses if needed.
  
---

## Requirements

- [python](https://www.python.org/downloads/)
- [windows-curses](https://pypi.org/project/windows-curses/) (or curses on linux/mac)

---


