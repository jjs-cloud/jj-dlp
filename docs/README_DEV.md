# jj-dlp — Multi-Site Stream Recorder

**Version:** 1.11.1

A powerful, multi-site stream recorder with a MenuWorks-style curses dashboard, Twitch EventSub integration, and automatic GitHub-based updates. Built on yt-dlp.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Core Features](#core-features)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Usage](#usage)
7. [Dashboard UI](#dashboard-ui)
8. [Advanced Features](#advanced-features)
9. [Troubleshooting](#troubleshooting)
10. [Development & Contributing](#development--contributing)
11. [License](#license)

---

## Overview

### Purpose & Problem Domain

**jj-dlp** is a multi-site stream recorder application designed for users who monitor and record streams from multiple platforms simultaneously. It solves the key problems of:

1. **Multi-site coordination** — Monitor and record multiple streamers/sites in parallel without manual intervention
2. **Real-time notifications** — Detect when streamers go live via both polling and Twitch EventSub webhooks (instant push notifications)
3. **Graceful process management** — Handle recording interruptions, splits, stalls, and resource constraints
4. **Interactive control** — Provide a MenuWorks-style curses dashboard for real-time monitoring and control
5. **Configuration flexibility** — Support multiple independent configurations (one per streamer/site) with global and local settings
6. **Automatic updates** — Self-update from GitHub without manual intervention

### Core Architecture

jj-dlp is built as a **multi-threaded, event-driven application** with the following high-level structure:

```
┌─────────────────────────────────────────────────────────────┐
│                    jj-dlp Main Process                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Per-Site State (SiteState) — One per config file    │  │
│  ├──────────────────────────────────────────────────────┤  │
│  │  • Monitor Thread (liveness polling)                │  │
│  │  • Config Watcher Thread (file change detection)    │  │
│  │  • Recording Threads (one per active stream)        │  │
│  │  • Pipe Drain Threads (stdout/stderr from yt-dlp)  │  │
│  │  • Twitch EventSub Server (webhook listener)        │  │
│  │  • Thread-safe state (dash_lock, lock, etc.)       │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Curses Dashboard (Main Thread)                       │  │
│  ├──────────────────────────────────────────────────────┤  │
│  │  • Event loop reading from terminal input            │  │
│  │  • Renders panels by reading thread-safe state      │  │
│  │  • Updates once per frame (~100ms)                  │  │
│  │  • Can switch between curses/terminal modes         │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Shared Services                                      │  │
│  ├──────────────────────────────────────────────────────┤  │
│  │  • Global logging infrastructure (logger.py)        │  │
│  │  • Configuration parsing (config_editor.py)         │  │
│  │  • Browser cookie auth (browser_config.py)          │  │
│  │  • GitHub auto-updater (updater.py)                 │  │
│  │  • Dependency manager (deps.py)                     │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Thread & Concurrency Model

**Threads spawned by jj-dlp:**

1. **Dashboard Event Loop** (main thread)
   - Runs the curses UI
   - Reads keyboard input and renders panels
   - Does NOT perform blocking operations; relies on thread-safe reads
   - Can spawn modal dialogs (config editor, browser chooser, config chooser)

2. **Per-Site Monitor Thread** (1 per config file)
   - Polls the liveness check command (e.g., yt-dlp query API) at `check_interval` seconds
   - Detects when streamers go live
   - Spawns recording threads for live streamers
   - Waits for next check or responds to `trigger_event` signal
   - Also initializes Twitch EventSub server if enabled

3. **Per-Site Config Watcher Thread** (1 per config file)
   - Watches the config file for changes (mtime polling)
   - Signals the monitor thread to re-check when config is updated
   - Invalidates cached config so monitor picks up new settings

4. **Recording Threads** (1 per active streamer)
   - Spawns a yt-dlp subprocess to download the stream
   - Launches 2 pipe drain threads (stdout/stderr) to read subprocess output
   - Manages recording lifecycle (split, stall detection, blocking, etc.)
   - Detects when stream ends (yt-dlp process exit) and cleans up

5. **Pipe Drain Threads** (2 per recording = 2 * active recordings)
   - Read and process stdout/stderr from yt-dlp subprocess
   - Detect ffmpeg errors and stalls
   - Log output to per-site log files
   - Feed debug lines to the dashboard log panel

6. **Twitch EventSub Server Thread** (0 or 1 per config)
   - Runs a minimal HTTP server on configurable port (default 8888)
   - Listens for webhook POST requests from Twitch
   - Verifies HMAC signature on incoming events
   - Calls `on_stream_online` callback when streamer goes live
   - Manages EventSub subscriptions (create/delete via Twitch API)

7. **Periodic Update Checker Thread** (daemon, 1 global)
   - Wakes up every `update_interval` minutes
   - Checks GitHub for new commits
   - Sets `UPDATE_AVAILABLE` flag if new version exists

**Synchronization primitives used:**

- `threading.Lock` — Protects mutable shared state (dash_lock, lock, procs_lock, config_cache_lock)
- `threading.Event` — Used for shutdown signaling (_stop_event) and inter-thread communication (trigger_event)
- Atomic reads/writes — Global variables like `OUTPUT_MODE` and `UPDATE_AVAILABLE` protected with locks

**Race condition prevention:**

- All dashboard-visible state (dash_*) is protected by `site.dash_lock`
- All recording-related state (currently_recording, evicted_streamers) is protected by `site.lock`
- yt-dlp process references (_active_procs) are protected by `_procs_lock`
- Config cache is read-only after load with TTL; protected by _cfg_cache_lock

### Core Data Structures

#### SiteState (lines 504–650 in main.py)

Encapsulates all mutable runtime state for a single monitored configuration:

```python
class SiteState:
    # Identity
    config_path: str                        # Path to .conf file
    label: str                              # Display name in dashboard
    site_order: int                         # Sort order (0=leftmost)
    
    # Concurrency control
    lock: threading.Lock                    # Protects recording state
    dash_lock: threading.Lock               # Protects dashboard-visible state
    trigger_event: threading.Event          # Signal monitor to check now
    _stop_event: threading.Event            # Signal all threads to shut down
    
    # Recording management
    currently_recording: Set[str]           # Streamers being recorded right now
    evicted_streamers: Set[str]             # Streamers to stop ASAP
    recording_threads: List[threading.Thread]  # Active recording threads
    known_streamers: Set[str]               # All streamers in config
    _active_procs: Dict[str, subprocess.Popen]  # Running yt-dlp processes
    
    # Dashboard display state (written by monitor/recording threads, read by dashboard)
    dash_live_since: Dict[str, float]       # Streamer → time when went live
    dash_last_live: Dict[str, float]        # Streamer → last time recording ended
    dash_all_streamers: List[str]           # All configured streamers
    dash_blocked: Set[str]                  # Currently blocked streamers
    dash_next_check_in: float               # Seconds until next liveness check
    dash_log_lines: List[str]               # Recent activity log (last 200 lines)
    dash_stdout_lines: List[str]            # yt-dlp stdout output
    dash_stderr_lines: List[str]            # yt-dlp stderr output
    
    # Monitoring & config
    monitor_thread: Optional[threading.Thread]
    watcher_thread: Optional[threading.Thread]
    eventsub: Optional[TwitchEventSub]      # Webhook server instance
    eventsub_state: Optional[EventSubState] # Dashboard-visible EventSub status
    _cfg_cache: Optional[dict]              # Cached config (TTL 2s)
    
    # Error tracking
    ffmpeg_error_counts: Dict[str, int]     # Streamer → cumulative error count
    stall_since: Dict[str, float]           # Streamer → when stall detected
    
    # UI state
    show_checker_stdout: bool               # Show JSON output from checker
    show_checker_stderr: bool
    show_debug_log: bool                    # Show debug lines in Log panel
    popup_last_shown: Dict[str, float]      # Streamer → time of last popup
```

#### Configuration Structure

Configuration is hierarchical and loaded from INI files:

1. **global.conf** — App-wide settings (loaded once)
   - Global keys like DISK_DRIVES, DEBUG_LOGS, CHECK_FOR_UPDATES, UPDATE_BRANCH, MAX_CONCURRENT_REC
   - Applies to all sites uniformly

2. **Per-site .conf files** — One per streamer/platform
   - Site-specific keys like CHECK_INTERVAL, OUTPUT_DIR, SITE_TMPL, STALL_TIMEOUT
   - [General] section with per-site overrides
   - [Downloader] section with yt-dlp command arguments
   - [Checker] section with liveness check command arguments
   - Streamer-specific sections for per-streamer overrides

3. **global.json** — Persistent state across runs
   - update_info: {current_sha, latest_sha, update_available} for versioning
   - startup_configs: Saved list of last-used config files
   - dbg_filters_state: Saved debug filter tags (on/off per tag)

Configuration is loaded on-demand and cached (TTL 2 seconds) to avoid excessive file I/O during dashboard rendering.

### Core Workflows

#### Startup Sequence

1. Dependency checks (curses, FFmpeg)
2. Config discovery/selection (auto, prompt, or --config args)
3. Browser selection (if ASK_FOR_BROWSER is true)
4. Load global.conf
5. Check for available updates; prompt user if update pending
6. Spawn per-site state and threads:
   - Create SiteState for each config
   - Start monitor thread (liveness polling + EventSub server)
   - Start config watcher thread
7. Configure logger (inject dashboard callbacks)
8. Enter curses dashboard event loop

#### Monitoring & Recording Workflow

**Monitor Thread Main Loop (lines 1856–1905):**

```
while not stop:
    load config
    get list of live streamers (via liveness check command OR EventSub callback)
    update dashboard display state
    if live streamers exist:
        call start_recording_if_needed()
    
    wait check_interval seconds (or until trigger_event fires)
    if config changed:
        trigger_event fires → loop again immediately
```

**Recording Workflow:**

1. `start_recording_if_needed()` checks:
   - Is streamer already recording? Skip.
   - Is streamer blocked? Skip.
   - Have we hit MAX_CONCURRENT_REC limit? Skip.
   - Otherwise: spawn recording thread.

2. Recording thread (`record_stream()`) does:
   - Build yt-dlp command with cookies, format, output path
   - Spawn subprocess with Popen
   - Launch 2 pipe drain threads for stdout/stderr
   - Enter inner loop:
     - Check for stop signals (site._stop_event, evicted_streamers, blocked list)
     - Check for ffmpeg errors/stalls
     - If split_after is set: split file every N seconds
     - Wait for process to exit

3. When stream ends:
   - yt-dlp subprocess exits (detected via `proc.poll()`)
   - Pipe drain threads finish reading output
   - Recording thread cleans up logs and state
   - Wait cooldown_after_recording seconds before allowing another record

#### Dashboard Rendering

The dashboard is a **MenuWorks-style TUI** (terminal user interface):

- **Panels:** Site columns, system stats sidebar, log tabs
- **Multi-tab interface:** Dashboard, Stdout, Stderr, Log, Config Editor
- **Update frequency:** ~10 FPS (100ms frame time)
- **Input handling:** Keyboard shortcuts for pause, resume, stop, mode switch

The dashboard thread **never blocks** — it only reads thread-safe state via locks. All actual work (checking streams, recording, etc.) happens in background threads.

### Configuration System

jj-dlp uses an **INI-based configuration** with a strict schema defined in `config_editor.py:CONFIG_KEYS`:

- ~40 recognized keys (global + per-site)
- Each key has a scope (global/site), default value, and help comment
- Unknown keys are silently ignored for forward compatibility
- Config can be edited live via dashboard (ConfigEditor modal)
- Changes trigger config watcher to wake up monitor thread

**Key categories:**

- **Paths & Output:** output_dir, output_tmpl, log_path
- **Timing & Intervals:** check_interval, cooldown_after_recording, stall_check_interval, stall_timeout, config_check_interval
- **Recording Quality:** (yt-dlp command arguments in [Downloader] section)
- **Liveness Detection:** site_tmpl, checker_cookies, downloader_cookies
- **UI & Notifications:** popup_notifications, popup_timeout, popup_cooldown, panel_resize, progress_bar_*
- **Advanced:** split_after, twitch_enabled, max_concurrent_rec

### External Integrations

#### yt-dlp Integration

- jj-dlp spawns yt-dlp as a subprocess for both liveness checking and recording
- Liveness check: `yt-dlp --simulate <url>` (queries API without downloading)
- Recording: `yt-dlp --output <path> <url>` (downloads video stream)
- Browser cookies can be passed: `--cookies-from-browser firefox`

#### Twitch EventSub Integration

- Alternative to polling: instead of checking every 60 seconds, receive instant webhook notifications when a streamer goes live
- Implementation:
  - HTTP server listens on WEBHOOK_PORT (default 8888)
  - Register webhooks with Twitch API for each monitored streamer
  - Twitch POSTs HMAC-signed JSON to CALLBACK_URL when streamer.online
  - Verify signature and spawn recording immediately
- Requires:
  - Twitch OAuth credentials (client_id, client_secret)
  - Public CALLBACK_URL (e.g., via ngrok or reverse proxy)
  - Network accessibility from Twitch (port forwarding if behind NAT)

#### GitHub Auto-Update System

- Periodically fetches latest commit SHA from GitHub API
- Stores current/latest SHA in global.json
- If new commit found, sets UPDATE_AVAILABLE flag
- On user request (or at startup), downloads repo ZIP, extracts, and replaces files
- Preserves config files and certain global settings during update
- Supports multiple branches (main, testing, experimental)

#### Browser Cookie Authentication

- Extracts cookies from browser local storage (Firefox, Opera, Safari)
- Used to access age-restricted or account-required content
- Passed to yt-dlp via `--cookies-from-browser` flag
- Can be configured per-site for both downloader and checker

### Error Handling & Recovery

**Graceful degradation:**

- If FFmpeg has errors: restart recording automatically (up to error limit)
- If yt-dlp crashes: detect process exit and retry with cooldown
- If config file is corrupted: use cached config or defaults
- If Twitch EventSub fails: fall back to polling

**Stall detection:**

- Monitor output file size growth
- If file size hasn't changed for `stall_timeout` seconds: restart recording
- Prevents infinite hangs when stream stops mid-record

**Blocking mechanism:**

- Users can block/unblock streamers via dashboard
- Blocking immediately kills active recording for that streamer
- Config change detection wakes monitor thread to refresh

### Performance Considerations

1. **Config caching** — Config files loaded at most every 2 seconds to avoid repeated disk I/O
2. **Dashboard frame rate** — 10 FPS update limit to avoid excessive CPU and lock contention
3. **Polling efficiency** — Monitor thread sleeps between checks; can be woken early via trigger_event
4. **Thread-safe data structures** — Locks are held for minimal time; state is updated atomically
5. **Log buffer limits** — Dashboard log keeps only last 200 lines to avoid memory bloat
6. **Subprocess management** — Each recording is a separate process; yt-dlp scales with download bandwidth, not jj-dlp CPU

### Design Patterns Used

1. **Thread-per-site model** — Each config file gets its own monitor/watcher threads for independence
2. **Producer-consumer pattern** — Monitor thread produces live notifications; recording threads consume and spawn recordings
3. **Observer pattern** — Config watcher observes file changes and notifies monitor thread
4. **Singleton pattern** — Global logger, updater, config parser instances
5. **Event loop pattern** — Dashboard runs a frame-by-frame event loop (input → render → sleep)
6. **Cache-aside pattern** — Config cache with TTL validation

### Key Implementation Details

- **No spawned GUI frameworks** — Uses curses (standard library) only, works over SSH
- **No external dependencies** — ffmpeg and yt-dlp are called as subprocesses, not linked libraries
- **Cross-platform** — Uses `subprocess.CREATE_NO_WINDOW` on Windows; `start_new_session` on Unix for proper subprocess group cleanup
- **Resilient subprocess management** — Tracks all spawned yt-dlp processes and kills them gracefully on shutdown
- **Timestamp-based liveness** — Tracks when streamers were first detected live and last seen recording to highlight recent activity

---

## Quick Start

### Prerequisites

*[Section to be filled: System requirements, Python version, OS compatibility]*

- Python 3.8+
- FFmpeg
- [Additional dependencies to be documented]

### Installation Steps

*[Section to be filled: Step-by-step installation instructions]*

1. Clone/download the repository
2. Install dependencies
3. Run initial setup
4. [Additional setup steps]

### First Run

*[Section to be filled: How to get a user up and running in 5 minutes]*

```bash
python -m jj_dlp [config_file]
```

*[More details and examples to follow]*

---

## Core Features

### 1. Multi-Site Stream Recording

*[Section to be filled: How jj-dlp handles multiple streaming sites and configurations]*

- **Supported Sites:** [List of supported platforms]
- **Per-Site Configuration:** [Explain how different sites can have different settings]
- **Site Management:** [How to add, remove, enable/disable sites]
- **Recording Quality:** [Resolution, bitrate, codec options]
- **Output Organization:** [File naming, directory structure]

### 2. Twitch EventSub Integration

*[Section to be filled: Twitch-specific features and instant notifications]*

- **Setup & Authentication:** [How to configure Twitch OAuth]
- **Instant Notifications:** [How EventSub provides real-time alerts]
- **Webhook Configuration:** [CALLBACK_URL, port, and reverse proxy setup]
- **Automatic Subscription Management:** [How subscriptions are created/cleaned up]
- **Fallback Polling:** [What happens if EventSub isn't available]

### 3. Configuration Management

*[Section to be filled: Config file format, options, and editing]*

- **Config File Format:** [INI/TOML/other format specification]
- **[General] Section:** [Global settings, paths, logging]
- **[Downloader] Section:** [Recording quality and format options]
- **[Checker] Section:** [Liveness checking and polling behavior]
- **Per-Site Sections:** [Site-specific overrides and settings]
- **Browser Cookie Auth:** [Supported browsers, setup process]
- **Config Validation:** [How invalid configs are handled]

### 4. Browser Cookie Authentication

*[Section to be filled: Using browser cookies for authentication]*

- **Supported Browsers:** Firefox, Opera, Safari, disabled
- **Why Use Cookies:** [Age-restricted content, account-specific settings]
- **Setting Up:** [Step-by-step for each browser]
- **Cookie Safety:** [Security considerations]
- **Troubleshooting:** [Common browser auth issues]

### 5. Automatic Updates

*[Section to be filled: GitHub-based auto-update system]*

- **Update Channels:** [main branch, dev branch, or custom]
- **Checking for Updates:** [How updates are detected]
- **Installing Updates:** [The update process and rollback]
- **Update Notifications:** [Where updates appear in the dashboard]
- **Manual Updates:** [How to trigger updates manually]

### 6. Recording Control

*[Section to be filled: Interactive recording management during runtime]*

- **Pause/Resume:** [How to pause individual recordings]
- **Stop Recording:** [Graceful shutdown of active downloads]
- **Priority Recording:** [If multiple sites go live simultaneously]
- **Recording Status:** [Live view of recording progress]
- **File Splitting:** [How recordings are split across multiple files]

### 7. Comprehensive Logging

*[Section to be filled: Debug logs, crash logs, and log filtering]*

- **Debug Log:** [What gets logged, log levels, filtering by tag]
- **Activity Log:** [Recording events, errors, status changes]
- **Crash Log:** [Unhandled exception capturing]
- **Log Configuration:** [File paths, rotation, retention]
- **Debug Filters:** [Per-tag debug output control]

### 8. Output Modes

*[Section to be filled: Different ways jj-dlp can be run]*

- **Curses Dashboard Mode:** [Interactive TUI with live monitoring]
- **Terminal Mode:** [Simple text output without dashboard]
- **Silent/Daemon Mode:** [Background operation]
- **Switching Modes:** [Command-line options and hotkeys]

---

## Installation

### System Requirements

*[Section to be filled: Detailed OS/platform support, dependencies list]*

### Python & Package Installation

*[Section to be filled: Virtual environments, pip install, dependency resolution]*

### FFmpeg Setup

*[Section to be filled: Installing FFmpeg on different platforms]*

- **Windows:** [Installation instructions]
- **macOS:** [Installation instructions]
- **Linux:** [Installation instructions]

### First-Time Setup

*[Section to be filled: Initial configuration wizard or manual setup]*

- Creating a config file
- Testing the yt-dlp integration
- Setting up logging
- Configuring browser auth (optional)

---

## Configuration

### Config File Locations

*[Section to be filled: Where jj-dlp looks for config files, precedence order]*

### [General] Section Reference

*[Section to be filled: Complete list of all global config options]*

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| [To be filled] | | | |

### [Downloader] Section Reference

*[Section to be filled: yt-dlp and recording quality options]*

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| [To be filled] | | | |

### [Checker] Section Reference

*[Section to be filled: Liveness checking and polling options]*

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| [To be filled] | | | |

### Per-Site Configuration

*[Section to be filled: How to define individual site sections and their options]*

```ini
[SiteName]
; Site-specific configuration overrides
```

### Browser Cookie Configuration

*[Section to be filled: --cookies-from-browser setup and syntax]*

```ini
[Downloader]
--cookies-from-browser
firefox
```

### Config Validation & Errors

*[Section to be filled: Common config mistakes and how to fix them]*

---

## Usage

### Basic Command Line

*[Section to be filled: Command-line arguments and options]*

```bash
python -m jj_dlp [options] config_file1 [config_file2 ...]
```

| Argument | Purpose |
|----------|---------|
| [To be filled] | |

### Running a Single Config

*[Section to be filled: Simple example with one config file]*

```bash
python -m jj_dlp twitch.ini
```

### Running Multiple Configs

*[Section to be filled: Managing multiple independent configurations]*

```bash
python -m jj_dlp config1.ini config2.ini config3.ini
```

### Interactive Dashboard Navigation

*[Section to be filled: Keyboard shortcuts and controls in curses mode]*

| Key | Action |
|-----|--------|
| [To be filled] | |

### Dashboard Panels

*[Section to be filled: Explanation of each dashboard panel and what it shows]*

- **Site List Panel:** [What info is displayed, color coding]
- **Recording Status Panel:** [Current downloads, progress]
- **System Stats Panel:** [CPU, memory, disk, network]
- **Log/Message Panel:** [Recent events and notifications]
- **Debug Log Panel:** [Live debug output with filtering]

### Recording a Specific Streamer

*[Section to be filled: Triggering a manual recording]*

- Manual start via dashboard
- Automatic start when live
- Force-start even if not detected

### Stopping Recordings

*[Section to be filled: Different ways to stop an active recording]*

- Normal stop (wait for file close)
- Force stop (immediate termination)
- Soft stop (pause before cleanup)

---

## Advanced Features

### Twitch EventSub Setup (Deep Dive)

*[Section to be filled: Detailed Twitch EventSub configuration]*

- **Creating a Twitch Developer App:** [Step-by-step]
- **OAuth Configuration:** [Client ID, client secret setup]
- **Webhook URL & Reverse Proxy:** [ngrok, nginx, cloud tunneling]
- **Event Subscription Management:** [Subscription IDs, renewal]
- **Network & Firewall:** [Port forwarding, security]

### File Splitting & Recording Organization

*[Section to be filled: How recordings are split and organized on disk]*

- **Split Criteria:** [File size, duration, quality changes]
- **Naming Convention:** [How files are named and timestamped]
- **Directory Structure:** [Output directory layout]
- **Resumable Uploads:** [Partial upload recovery]

### Custom yt-dlp Arguments

*[Section to be filled: Passing advanced yt-dlp options through config]*

- **Recognized Flags:** [Which yt-dlp flags are supported]
- **Custom Postprocessors:** [Using ffmpeg postprocessing]
- **Format Selection:** [Format codes and quality ladders]

### Debug Logging & Troubleshooting

*[Section to be filled: Using debug logs to diagnose issues]*

- **Enabling Debug Logs:** [Setting DEBUG=true, changing log path]
- **Debug Filter Tags:** [DRAIN, CHECKER, SPLIT, POPEN, PERF, DISK, UPDATER, TWITCH, KILL, CONFIG, POPUP, STALL]
- **Reading Debug Output:** [Timestamp format, understanding tag categories]
- **Performance Profiling:** [PERF tag for timing data]

### Dependency Management

*[Section to be filled: Internal dependency resolution and fallback]*

- **Runtime Dependency Checks:** [How jj-dlp validates FFmpeg, yt-dlp]
- **Fallback Handling:** [What happens if a dependency is missing]
- **Curses Availability:** [Terminal detection and graceful degradation]

### Updating jj-dlp

*[Section to be filled: The auto-update process and manual updates]*

- **Automatic Background Updates:** [When checks happen, how to enable]
- **Manual Update Check:** [Command to trigger update check]
- **Applying Updates:** [Restart requirements, update persistence]
- **Choosing a Branch:** [main, dev, or custom GitHub branches]
- **Rollback:** [How to revert to a previous version]

### Multi-Config Management

*[Section to be filled: Running and managing multiple jj-dlp instances]*

- **Independent Instances:** [Config file isolation]
- **Shared vs. Separate Logging:** [Log file organization]
- **Resource Management:** [CPU/memory impact of parallel instances]

---

## Troubleshooting

### Common Issues & Solutions

*[Section to be filled: FAQ-style troubleshooting guide]*

#### Recording Not Starting
- *Symptoms:* [What the user observes]
- *Causes:* [Possible root causes]
- *Solutions:* [Step-by-step fixes]

#### Dashboard Not Rendering
- *Symptoms:*
- *Causes:*
- *Solutions:*

#### Authentication Failures
- *Symptoms:*
- *Causes:*
- *Solutions:*

#### EventSub Not Receiving Notifications
- *Symptoms:*
- *Causes:*
- *Solutions:*

#### Update Failures
- *Symptoms:*
- *Causes:*
- *Solutions:*

#### High CPU/Memory Usage
- *Symptoms:*
- *Causes:*
- *Solutions:*

### Getting Help

*[Section to be filled: Where users can report issues, ask questions]*

- GitHub Issues
- Discussion threads
- Debug log submission tips

### Collecting Debug Information

*[Section to be filled: How to gather diagnostic info for bug reports]*

- Relevant config files (redacted)
- Debug log output (time range, relevant tags)
- System information (OS, Python version, etc.)
- Steps to reproduce

---

## Development & Contributing

### Project Structure

*[Section to be filled: Overview of the codebase organization]*

```
jj_dlp/
├── main.py              [Core app, dashboard, recording loop]
├── config_editor.py     [Configuration parsing and validation]
├── browser_config.py    [Browser cookie authentication]
├── logger.py            [Logging infrastructure]
├── twitch_eventsub.py   [Twitch EventSub client and server]
├── updater.py           [GitHub-based auto-update]
├── deps.py              [Dependency management and checks]
└── [Other modules]
```

### Setting Up Development Environment

*[Section to be filled: How contributors should set up their local environment]*

### Code Style & Standards

*[Section to be filled: Coding conventions, linting, testing requirements]*

### Key Modules Explained

#### main.py
*[Section to be filled: Architecture of the main application loop]*

#### config_editor.py
*[Section to be filled: How config parsing and validation works]*

#### twitch_eventsub.py
*[Section to be filled: EventSub server architecture and webhook handling]*

#### logger.py
*[Section to be filled: Logging subsystem design and thread-safety]*

#### updater.py
*[Section to be filled: Update checking and installation process]*

### Testing

*[Section to be filled: How to run tests, what's covered]*

### Building & Distribution

*[Section to be filled: How to create releases, distribution packages]*

---

## License

*[Section to be filled: License type and text]*

---

## Appendix

### Command-Line Reference

*[Section to be filled: Complete CLI argument documentation]*

### Config File Reference (Full)

*[Section to be filled: Complete config key reference with examples]*

### Keyboard Shortcut Reference

*[Section to be filled: Complete list of all hotkeys in dashboard mode]*

### EventSub Webhook Examples

*[Section to be filled: Example webhook payloads and responses]*

### FAQ

*[Section to be filled: Frequently asked questions]*

- How do I record from multiple sites simultaneously?
- Can I use jj-dlp with a VPN?
- How much storage do I need?
- Can I customize the output filename?
- Is there a GUI version?
- Can I run jj-dlp on headless/server environments?

### Glossary

*[Section to be filled: Technical terms and abbreviations]*

- **EventSub:** Twitch's webhook notification system
- **yt-dlp:** The underlying video downloader
- **TUI:** Text User Interface (curses dashboard)
- **HMAC:** Hash-based Message Authentication Code
- **[Other terms]*

---

**End of Framework**

*This README framework is ready for progressive filling. Each major section is marked with a placeholder for its detailed content, allowing incremental development across multiple sessions.*
