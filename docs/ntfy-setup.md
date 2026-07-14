# Push Notifications Setup via ntfy.sh

`jj-dlp` supports pushing instant alerts to your iPhone or Android when a recording starts, using the free and open-source notification service **ntfy.sh**.

No accounts, sign-ups, or subscriptions are required!

## Setup Steps

### Step 1 — Install the App on your iPhone/Android
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

Open jj-dlp and switch to the Config tab. Tab over to the Global Settings section and update the NTFY_TOPIC key with the same topic name you chose in step 2.

Restart `jj-dlp` to apply the settings.

---

## Customizing Notifications

### Enabling/Disabling Notifications per Site
To enable/disable notifications for a specific site, open jj-dlp and switch to the Config tab. Tab over to the Site Settings section, switch to the desired site, and update the NTFY_NOTIFICATIONS key to True/False. 

### Enabling/Disabling Notifications per Streamer
To enable/disable notifications for a specific streamer, open jj-dlp and switch to the Config tab. Tab over to the Streamer Settings section, switch to the desired streamer, press enter to open the streamer settings popup, and update "NTFY NOTIFICATIONS" to ON or OFF.
