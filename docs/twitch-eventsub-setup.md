# Twitch EventSub Setup for jj-dlp

EventSub lets Twitch **push** a notification to your script the instant a streamer goes live,
so recording starts immediately — no more waiting up to 60 seconds for the next poll.

---

## How it works

```
Twitch servers
    │
    │  POST /  (HMAC-signed JSON)
    ▼
jj-dlp webhook server  (port 8888)
    │
    │  start_recording_if_needed()
    ▼
yt-dlp  ← starts within ~1 second of the stream going live
```

1. jj-dlp starts a tiny HTTP server on a port you choose (default **8888**).
2. On startup it calls the Twitch API, resolves every streamer name → user ID,
   and subscribes to `stream.online` for each one.
3. Twitch POSTs a signed notification to your server the moment someone goes live.
4. jj-dlp verifies the HMAC signature and immediately starts yt-dlp.
5. The normal 60-second polling loop still runs as a safety net.

---

## Step 1 — Create a Twitch Developer Application

1. Go to <https://dev.twitch.tv/console/apps> and log in.
2. Click **Register Your Application**.
3. Fill in the form:
   - **Name**: anything (e.g. `jj-dlp`)
   - **OAuth Redirect URLs**: `http://localhost` (not actually used, just required)
   - **Category**: `Application Integration`
4. Click **Create**.
5. Click **Manage** next to your new app.
6. Copy the **Client ID** — you'll need it.
7. Click **New Secret**, copy the **Client Secret** — save it somewhere safe,
   Twitch only shows it once.

---

## Step 2 — Make your webhook reachable from the internet

Twitch must be able to reach your HTTP server. You have two options:

### Option A — Port forward (home server / always-on PC)

1. In your router's admin panel, forward **TCP port 8888** (or your chosen port)
   to your PC's local IP address.
2. Find your public IP at <https://whatismyip.com>.
3. Your `CALLBACK_URL` will be:
   ```
   http://YOUR_PUBLIC_IP:8888/
   ```
   > Twitch accepts plain http for development but **strongly prefers https** for
   > production. If you have a domain + SSL cert, use `https://` instead.

### Option B — ngrok (easiest for testing, works behind NAT)

1. Download ngrok from <https://ngrok.com/download> and create a free account.
2. Run: `ngrok http 8888`
3. ngrok gives you a URL like `https://abc123.ngrok-free.app` — use that as your
   `CALLBACK_URL`.
4. Note: free ngrok URLs change every time you restart ngrok, so you'd need to
   update the config and restart jj-dlp each time.

---

## Step 3 — Add the `[Twitch]` section to your config file

Open your `jj-dlp.conf` and add this section (put it anywhere, e.g. at the bottom):

```ini
[Twitch]
; Your app's Client ID from dev.twitch.tv
CLIENT_ID       = abc123youridhere

; Your app's Client Secret (keep this private!)
CLIENT_SECRET   = xyz789yoursecrethere

; A random string you make up — used to verify Twitch's signatures.
; Use any long random string, e.g. from: python3 -c "import secrets; print(secrets.token_hex(32))"
WEBHOOK_SECRET  = my-super-secret-random-string-change-me

; The public URL Twitch will POST notifications to.
; Must end with a slash. Must be reachable from the internet.
CALLBACK_URL    = http://YOUR_PUBLIC_IP:8888/

; Port the local HTTP server listens on (default: 8888)
WEBHOOK_PORT    = 8888
```

**All three of `CLIENT_ID`, `CLIENT_SECRET`, and `CALLBACK_URL` must be present**
for EventSub to activate. If any are missing the feature is silently skipped and
normal polling continues as before.

---

## Step 4 — Generate a good WEBHOOK_SECRET

Run this in a terminal to get a cryptographically random secret:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output as your `WEBHOOK_SECRET`. This string is used to verify that
notifications are actually from Twitch and not spoofed.

---

## Step 5 — Start jj-dlp

Just run jj-dlp normally. If EventSub is configured correctly you'll see:

```
[Twitch] EventSub: credentials found — starting webhook listener and subscription manager
[Twitch] EventSub webhook server listening on port 8888
[Twitch] EventSub: subscribed to stream.online for streamer1
[Twitch] EventSub: subscribed to stream.online for streamer2
```

The dashboard (output mode 1) also shows:

```
  Twitch EventSub: ● active  (2 subscriptions)
```

When a streamer goes live you'll see:

```
[Twitch] EventSub: streamer1 just went live (type=live) — starting immediately
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No "EventSub" lines at startup | Check that all three config keys are present and non-empty |
| `could not obtain access token` | Client ID or Client Secret is wrong |
| `could not resolve user_id for 'name'` | Streamer name is misspelled or the account doesn't exist |
| `subscription FAILED` | CALLBACK_URL isn't reachable from the internet; check port forwarding or ngrok |
| Signature verification FAILED | `WEBHOOK_SECRET` in config doesn't match what was used when subscribing — restart jj-dlp after changing it |
| Subscription revoked | Twitch revokes subscriptions if your server is unreachable for ~24h; jj-dlp will resubscribe automatically |

---

## Notes

- **No extra Python packages required** — the EventSub engine uses only the standard library.
- Subscriptions are **automatically cleaned up** when you stop jj-dlp (Ctrl+C).
- If a streamer is added or removed from `[Streamers]`, jj-dlp syncs subscriptions
  within ~15 seconds without a restart.
- EventSub subscriptions are also **refreshed every hour** to rotate the access token.
- The normal poll loop still runs as a backup. If EventSub misses a notification
  for any reason, the poll will catch it within `CHECK_INTERVAL` seconds.
