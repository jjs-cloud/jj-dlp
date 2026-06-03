# jj-dlp — Multi-Site Stream Recorder

**Version:** 1.11.1

A powerful, multi-site stream recorder with a DOS style curses dashboard. Built on yt-dlp.

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

*[Section to be filled: High-level description of what jj-dlp does, target use cases, key differentiators]*

- Multi-site stream monitoring and recording
- Real-time curses-based dashboard interface
- Twitch EventSub instant notifications (vs. polling)
- Automatic updates from GitHub
- Browser-based cookie authentication
- Comprehensive logging and debug tools

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
