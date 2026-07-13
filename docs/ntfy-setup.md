# Push Notifications Setup via ntfy.sh

`jj-dlp` supports pushing instant alerts to your iPhone or Android when a recording starts, using the free and open-source notification service **ntfy.sh**.

No accounts, sign-ups, or subscriptions are required!

---

## How it works

```
  jj-dlp (Starts Recording)
            │
            │  POST https://ntfy.sh/<your-topic>
            ▼
       ntfy.sh Servers
            │
            │  Apple APNs Push
            ▼
    Your iPhone (ntfy App)
```

1. You install the free **ntfy** app on your phone and subscribe to a unique topic name of your choosing.
2. In `configs/global.conf`, you configure `NTFY_TOPIC` with your unique topic name.
3. When `jj-dlp` starts recording a streamer, it sends an HTTP POST request to `ntfy.sh` with the notification details.
4. You receive a native iOS push notification on your iPhone instantly.

---

## Setup Steps

### Step 1 — Install the App on your iPhone
1. Search for and download **ntfy** from the App Store or Google Play.
2. Open the app and allow notifications when prompted.

### Step 2 — Choose a Unique Topic
Since ntfy.sh is a public service, anybody who knows your topic name can see your notifications or send notifications to you. 
1. Choose a unique, random topic name that is hard to guess (e.g. `jj-dlp-alerts-z9k2-p5q`).
2. Do **not** use simple names like `jj-dlp` or `alerts`.

### Step 3 — Subscribe to the Topic in the mobile app
1. In the ntfy app, tap **+ (Subscribe to topic)**.
2. Enter your chosen unique topic name.
3. Tap **Subscribe**.

### Step 4 — Configure jj-dlp

Open jj-dlp and switch to the Config tab. Tab over to the Global Settings section and update the NTFY_TOPIC key with your own unique topic name. 

Restart `jj-dlp` to apply the settings.

---

## Customizing Notifications

### Disabling per Site
By default, notifications are enabled for all sites. To disable notifications for a specific site (e.g. Twitch), open jj-dlp and switch to the Config tab. Tab over to the Site Settings section, switch to Twitch, and update the NTFY_NOTIFICATIONS key to False. 


### Enabling Notifications per Streamer
You can configure notifications on a per-streamer basis, which will override the site-level setting.   First, make sure the NTFY_NOTIFICATIONS is set to false in the SITE SETTINGS panel, and then enable it per streamer using the following steps:

1. Go to the **Config** tab.
2. Tab over to the STREAMER SETTINGS panel.
3. Highlight the streamer you want to edit and press **Enter** to open the **SETTINGS** popup.
4. Select **Notifications** and press **Enter**.
5. Press **Space** to toggle **Notifications Enabled** (`[x]` or `[ ]`), then press **Enter** to save.
