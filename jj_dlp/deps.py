"""
deps.py  —  dependency detection & installation for jj-dlp

Covers:
  • curses  (windows-curses on Windows, OS package on Linux/macOS)
  • ffmpeg  (winget on Windows, brew on macOS, distro PM on Linux)

Public API
----------
# curses
check_curses_available()          -> bool
install_curses_auto(progress_cb)  -> (bool, str)
ensure_curses()                   -> None   (prompts & exits/continues)

# ffmpeg
check_ffmpeg()                    -> (bool, str)
install_ffmpeg_auto(progress_cb)  -> (bool, str)
plain_ffmpeg_check()              -> bool   (prompts & returns continue flag)
"""

import os
import sys
import subprocess
import shutil


def _is_root() -> bool:
    """Return True if the current process is running as root/superuser.

    ``os.geteuid`` does not exist on Windows; this helper centralises the
    platform guard so callers never reach the attribute directly.
    """
    # os.geteuid is a POSIX-only API and raises AttributeError on Windows.
    # We only need sudo-elevation on POSIX systems anyway, so returning False
    # on Windows is both safe and correct.
    return getattr(os, "geteuid", lambda: -1)() == 0


# ══════════════════════════════════════════════════════════════════════════════
# curses detection & installation
# ══════════════════════════════════════════════════════════════════════════════

def check_curses_available() -> bool:
    """Return True if the curses module can be imported."""
    try:
        import curses as _c  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


def install_curses_auto(progress_cb=None) -> tuple:
    """
    Attempt to install the platform-appropriate curses package.

    On Windows: installs 'windows-curses' via pip.
    On macOS/Linux: curses ships with Python; if missing, advise the user to
    install the OS ncurses dev package and rebuild Python (or use their
    distro's python3-curses package).

    progress_cb(line: str) is called for each line of installer output.

    Returns (success: bool, message: str).
    """
    def _emit(line):
        if progress_cb:
            progress_cb(line)

    if sys.platform == "win32":
        _emit("Attempting: pip install windows-curses ...")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "windows-curses"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                _emit(line.rstrip())
            proc.wait()
            if proc.returncode == 0:
                if check_curses_available():
                    return True, "windows-curses installed successfully."
                return False, ("pip reported success but curses still "
                               "cannot be imported. Try restarting the script.")
            return False, f"pip exited with code {proc.returncode}."
        except Exception as exc:
            return False, f"Installation error: {exc}"

    elif sys.platform == "darwin":
        msg = (
            "curses is part of Python's standard library on macOS.\n"
            "If it is missing, your Python installation may be incomplete.\n"
            "Recommended fix:\n"
            "  brew install python   # reinstall via Homebrew\n"
            "or download the official installer from https://www.python.org/downloads/"
        )
        _emit(msg)
        return False, msg

    else:  # Linux / other POSIX
        candidates = [
            ("apt-get",  ["apt-get", "install", "-y", "python3-curses"]),
            ("apt",      ["apt",     "install", "-y", "python3-curses"]),
            ("dnf",      ["dnf",     "install", "-y", "python3-curses"]),
            ("yum",      ["yum",     "install", "-y", "python3-curses"]),
            ("pacman",   ["pacman",  "-S",      "--noconfirm", "python-curses"]),
            ("zypper",   ["zypper",  "install", "-y", "python3-curses"]),
            ("apk",      ["apk",     "add",     "py3-curses"]),
        ]
        pm_name, cmd = "", []
        for name, c in candidates:
            if shutil.which(name):
                pm_name, cmd = name, c
                break

        if not cmd:
            msg = ("No supported package manager found.\n"
                   "Install the python3-curses package for your distribution manually.")
            _emit(msg)
            return False, msg

        if not _is_root():
            cmd = ["sudo"] + cmd

        _emit(f"Attempting: {' '.join(cmd)} ...")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                _emit(line.rstrip())
            proc.wait()
            if proc.returncode == 0:
                if check_curses_available():
                    return True, "python3-curses installed successfully."
                return False, ("Package installed but curses still cannot be imported. "
                               "Try restarting the script.")
            return False, f"Package manager exited with code {proc.returncode}."
        except Exception as exc:
            return False, f"Installation error: {exc}"


def ensure_curses() -> None:
    """
    Plain-terminal (no curses) prompt shown when curses is not available.
    Offers to install it; exits or continues based on the user's choice.
    Call once at startup, before any curses.wrapper() call.
    """
    if check_curses_available():
        return

    BOLD  = "\033[1m"
    RED   = "\033[91m"
    YEL   = "\033[93m"
    GRN   = "\033[92m"
    RESET = "\033[0m"

    print()
    print(f"{BOLD}{RED}┌─ MISSING DEPENDENCY ───────────────────────────────┐{RESET}")
    print(f"{BOLD}{RED}│  curses  is not available in this Python install.   │{RESET}")
    print(f"{BOLD}{RED}└─────────────────────────────────────────────────────┘{RESET}")
    print()

    if sys.platform == "win32":
        print(f"{YEL}On Windows, curses requires the 'windows-curses' package.{RESET}")
        print(f"{YEL}It can be installed automatically via pip.{RESET}")
    elif sys.platform == "darwin":
        print(f"{YEL}On macOS, curses ships with Python.{RESET}")
        print(f"{YEL}Your Python installation may be incomplete or broken.{RESET}")
    else:
        print(f"{YEL}On Linux, curses may require the 'python3-curses' OS package.{RESET}")

    print()

    try:
        answer = input(f"{BOLD}Attempt automatic installation now? [Y/n]: {RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)

    if answer in ("", "y", "yes"):
        print()
        success, message = install_curses_auto(progress_cb=lambda l: print(f"  {l}"))
        print()
        if success:
            print(f"{GRN}✓  {message}{RESET}")
            print(f"{GRN}Curses has been successfully installed.  Please restart the script.{RESET}\n")
            try:
                input("Press Enter to exit...")
            except (EOFError, KeyboardInterrupt):
                pass
            sys.exit(0)
        else:
            print(f"{RED}✗  Installation failed:{RESET}")
            print(f"   {message}")
            print()
            try:
                cont = input("Continue anyway (the UI will not work)? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                cont = "n"
            if cont not in ("y", "yes"):
                sys.exit(1)
    else:
        print()
        print(f"{RED}curses is required for the dashboard UI.{RESET}")
        print("Install it manually and re-run, or press Enter to try continuing anyway.")
        try:
            input("Press Enter to continue, Ctrl+C to quit: ")
        except (EOFError, KeyboardInterrupt):
            sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# ffmpeg detection & installation
# ══════════════════════════════════════════════════════════════════════════════

def check_ffmpeg() -> tuple:
    """
    Returns (found: bool, path: str).
    Checks PATH via shutil.which, then a few common hard-coded locations.
    """
    p = shutil.which("ffmpeg")
    if p:
        return True, p

    candidates = []
    if sys.platform == "win32":
        candidates = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/usr/local/bin/ffmpeg",
            "/opt/homebrew/bin/ffmpeg",
            "/opt/local/bin/ffmpeg",
        ]
    else:
        candidates = [
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/snap/bin/ffmpeg",
        ]

    for c in candidates:
        if os.path.isfile(c):
            return True, c

    return False, ""


def _detect_linux_package_manager() -> tuple:
    """
    Returns (pm_name: str, install_cmd: list) for the first package manager found,
    or ("", []) if none detected.
    """
    candidates = [
        ("apt-get",  ["apt-get", "install", "-y", "ffmpeg"]),
        ("apt",      ["apt",     "install", "-y", "ffmpeg"]),
        ("dnf",      ["dnf",     "install", "-y", "ffmpeg"]),
        ("yum",      ["yum",     "install", "-y", "ffmpeg"]),
        ("pacman",   ["pacman",  "-S",      "--noconfirm", "ffmpeg"]),
        ("zypper",   ["zypper",  "install", "-y", "ffmpeg"]),
        ("apk",      ["apk",     "add",     "ffmpeg"]),
    ]
    for name, cmd in candidates:
        if shutil.which(name):
            return name, cmd
    return "", []


def install_ffmpeg_auto(progress_cb=None) -> tuple:
    """
    Attempt a platform-appropriate ffmpeg installation.

    progress_cb(line: str) is called for each line of installer output.

    Returns (success: bool, message: str).
    """
    if sys.platform == "win32":
        winget_available = False
        try:
            result = subprocess.run(
                ["winget", "--version"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            winget_available = result.returncode == 0
        except FileNotFoundError:
            winget_available = False

        if not winget_available:
            msg = ("winget not found. To install ffmpeg manually, run:\n"
                   "    winget install --id Gyan.FFmpeg -e\n"
                   "or download from https://www.gyan.dev/ffmpeg/builds/ "
                   "and add it to your PATH.")
            if progress_cb:
                progress_cb(msg)
            return False, msg

        cmd = ["winget", "install", "--id", "Gyan.FFmpeg", "-e",
               "--accept-source-agreements", "--accept-package-agreements"]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                if progress_cb:
                    progress_cb(line.rstrip())
            proc.wait()
            if proc.returncode == 0:
                return True, ("ffmpeg installed successfully via winget!\n"
                              "NOTE: You must restart your computer or log out and back in "
                              "for the system PATH to update. jj-dlp will not work properly until then.")
            return False, f"winget exited with code {proc.returncode}."
        except Exception as e:
            return False, f"winget failed to launch: {e}"

    if sys.platform == "darwin":
        brew = shutil.which("brew")
        if not brew:
            msg = ("Homebrew not found. Install Homebrew first:\n"
                   "  /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/"
                   "Homebrew/install/HEAD/install.sh)\"\n"
                   "Then run:  brew install ffmpeg")
            if progress_cb:
                progress_cb(msg)
            return False, msg
        cmd = [brew, "install", "ffmpeg"]
    else:
        pm_name, cmd = _detect_linux_package_manager()
        if not cmd:
            msg = "No supported package manager found (tried apt, dnf, yum, pacman, zypper, apk)."
            if progress_cb:
                progress_cb(msg)
            return False, msg
        if not _is_root():
            cmd = ["sudo"] + cmd

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            if progress_cb:
                progress_cb(line.rstrip())
        proc.wait()
        if proc.returncode == 0:
            found, path = check_ffmpeg()
            if found:
                return True, f"ffmpeg installed successfully at {path}"
            return True, "Installer reported success (restart may be needed for PATH)."
        return False, f"Installer exited with code {proc.returncode}."
    except FileNotFoundError as e:
        return False, f"Could not launch installer: {e}"
    except Exception as e:
        return False, f"Installation error: {e}"


def plain_ffmpeg_check() -> bool:
    """
    Plain-terminal ffmpeg presence check run before the main curses dashboard.

    • If ffmpeg is found  → brief confirmation, auto-continue after 1 s.
    • If ffmpeg is missing → prompt Y/N to attempt auto-install.
      - Y: run installer, stream output line-by-line to the terminal.
      - N: warn and continue (yt-dlp may still work without ffmpeg for
           some formats, but the user is informed).

    Returns True to continue startup, False to abort.
    """
    import time

    BOLD  = "\033[1m"
    RED   = "\033[91m"
    YEL   = "\033[93m"
    GRN   = "\033[92m"
    CYN   = "\033[96m"
    RESET = "\033[0m"

    # ── Phase 1: Detection ────────────────────────────────────────────────────
    found, ffmpeg_path = check_ffmpeg()

    if found:
        print()
        print(f"{GRN}{BOLD}✔  ffmpeg found:{RESET}")
        print(f"   {ffmpeg_path}")
        print(f"{YEL}Continuing in 0.3 seconds…{RESET}")
        time.sleep(0.3)
        return True

    # ── Phase 2: Missing — prompt ─────────────────────────────────────────────
    if sys.platform == "win32":
        winget_available = False
        try:
            result = subprocess.run(
                ["winget", "--version"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            winget_available = result.returncode == 0
        except FileNotFoundError:
            winget_available = False
        install_hint = ("Will run: winget install Gyan.FFmpeg" if winget_available
                        else "winget not found — manual install required.")
    elif sys.platform == "darwin":
        brew = shutil.which("brew")
        install_hint = ("Will run: brew install ffmpeg" if brew
                        else "Homebrew not found — manual install required.")
    else:
        pm_name, _ = _detect_linux_package_manager()
        install_hint = (f"Will run: sudo {pm_name} install ffmpeg" if pm_name
                        else "No supported package manager found.")

    if sys.platform == "win32":
        can_auto = winget_available
    elif sys.platform == "darwin":
        can_auto = bool(shutil.which("brew"))
    else:
        can_auto = bool(_detect_linux_package_manager()[0])

    print()
    print(f"{BOLD}{RED}┌─ DEPENDENCY CHECK — ffmpeg ─────────────────────────┐{RESET}")
    print(f"{BOLD}{RED}│  ffmpeg not found in PATH or common locations.       │{RESET}")
    print(f"{BOLD}{RED}└─────────────────────────────────────────────────────-┘{RESET}")
    print()
    print(f"{YEL}{install_hint}{RESET}")
    print()

    if can_auto:
        prompt = f"{BOLD}Install ffmpeg now? [Y/n]: {RESET}"
    else:
        prompt = f"{BOLD}Continue without ffmpeg? [N/q]: {RESET}"

    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return False

    if answer in ("q", "quit"):
        return False

    if answer in ("n", "no") or (not can_auto and answer not in ("y", "yes", "")):
        print()
        print(f"{YEL}⚠  Continuing without ffmpeg.{RESET}")
        print(f"{YEL}   Some formats / remuxing may fail.{RESET}")
        print()
        return True

    if not can_auto:
        print()
        print(f"{YEL}⚠  Continuing without ffmpeg.{RESET}")
        print(f"{YEL}   Some formats / remuxing may fail.{RESET}")
        print()
        return True

    # ── Phase 3: Install ──────────────────────────────────────────────────────
    print()
    print(f"{CYN}Installing ffmpeg — please wait…{RESET}")
    print()

    success, msg = install_ffmpeg_auto(progress_cb=lambda line: print(f"  {line}"))

    print()
    if success:
        print(f"{GRN}{BOLD}✔  Installation complete.{RESET}")
        print(f"   {msg}")
        if sys.platform == "win32":
            print()
            print(f"{YEL}{BOLD}NOTE: You must restart your computer (or log out and back in){RESET}")
            print(f"{YEL}      for the system PATH to update before jj-dlp will work properly.{RESET}")
            print()
            input("Press Enter to exit...")
            sys.exit(0)
        print()
        return True
    else:
        print(f"{RED}{BOLD}✘  Installation failed.{RESET}")
        print(f"   {msg}")
        print()
        try:
            cont = input(f"{BOLD}Continue without ffmpeg? [n/Q]: {RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            cont = "q"
        if cont in ("y", "yes", "n", "no"):
            print()
            print(f"{YEL}⚠  Continuing without ffmpeg.{RESET}")
            print(f"{YEL}   Some formats / remuxing may fail.{RESET}")
            print()
            return True
        return False
