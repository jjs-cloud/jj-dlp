import re

_SUPPORTED_BROWSERS = [
    "firefox", "opera", "safari", "disabled",
]

def _read_browser_from_section(lines: list, section_name: str) -> str:
    in_section = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == f"[{section_name.lower()}]":
            in_section = True
            continue
        if in_section:
            if stripped.startswith("["):          # entered a new section
                break
            if stripped.lower() == "--cookies-from-browser":
                # The browser name should be on the very next non-blank line
                for j in range(i + 1, len(lines)):
                    candidate = lines[j].strip()
                    if candidate == "":
                        continue
                    if candidate.startswith("[") or candidate.startswith("-"):
                        break
                    return candidate.lower()
    return ""

def _read_browser_from_config(config_path: str) -> str:
    """
    Return the browser name currently set after --cookies-from-browser in the
    [Downloader] or [Checker] section, or 'firefox' if not found.
    We use raw text scanning because configparser treats each line as a
    separate no-value key when the value sits on its own continuation line.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return "firefox"

    browser = _read_browser_from_section(lines, "Downloader")
    if browser:
        return browser
    
    browser = _read_browser_from_section(lines, "Checker")
    if browser:
        return browser

    return "firefox"


def _write_browser_to_section(lines: list, browser: str, section_name: str) -> None:
    in_section = False
    cookies_idx   = None
    browser_idx   = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == f"[{section_name.lower()}]":
            in_section = True
            continue
        if in_section:
            if stripped.startswith("["):
                break
            if stripped.lower() == "--cookies-from-browser":
                cookies_idx = i
                for j in range(i + 1, len(lines)):
                    candidate = lines[j].strip()
                    if candidate == "":
                        continue
                    if candidate.startswith("[") or candidate.startswith("-"):
                        break
                    browser_idx = j
                    break
                break

    if browser == "disabled":
        to_remove = sorted(
            [x for x in [cookies_idx, browser_idx] if x is not None],
            reverse=True,
        )
        for idx in to_remove:
            lines.pop(idx)
    else:
        if cookies_idx is not None and browser_idx is not None:
            indent = lines[browser_idx][: len(lines[browser_idx]) - len(lines[browser_idx].lstrip())]
            lines[browser_idx] = f"{indent}{browser}\n"
        elif cookies_idx is not None:
            lines.insert(cookies_idx + 1, f"{browser}\n")
        else:
            for i, line in enumerate(lines):
                if line.strip().lower() == f"[{section_name.lower()}]":
                    lines.insert(i + 1, f"{browser}\n")
                    lines.insert(i + 1, "--cookies-from-browser\n")
                    break

def _write_browser_to_config(config_path: str, browser: str, write_downloader: bool = True, write_checker: bool = False) -> None:
    """
    Update the configured sections so that --cookies-from-browser is followed
    by *browser* on the next line.  If browser == 'disabled', both the
    --cookies-from-browser line and the browser-name line are removed.
    Uses raw text manipulation to preserve the rest of the file exactly.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return

    if write_downloader:
        _write_browser_to_section(lines, browser, "Downloader")
    
    if write_checker:
        _write_browser_to_section(lines, browser, "Checker")

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass


def _write_ask_for_browser_to_config(config_path: str, value: bool) -> None:
    """
    Set ASK_FOR_BROWSER = True/False in the [General] section of *config_path*.
    If the key already exists it is updated in-place; otherwise it is appended
    to the end of the [General] section.  The rest of the file is preserved.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return

    val_str = "True" if value else "False"
    key_name = "ASK_FOR_BROWSER"

    # Try to update an existing ASK_FOR_BROWSER line anywhere in the file
    for i, line in enumerate(lines):
        if re.match(r"^\s*ASK_FOR_BROWSER\s*=", line, re.IGNORECASE):
            lines[i] = f"ASK_FOR_BROWSER = {val_str}\n"
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
            except Exception:
                pass
            return

    # Key not found — insert after the [General] header
    general_idx = None
    next_sec_idx = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == "[general]":
            general_idx = i
            continue
        if general_idx is not None and stripped.startswith("["):
            next_sec_idx = i
            break

    if general_idx is None:
        # No [General] section — append one
        lines.append("\n[General]\n")
        lines.append(f"{key_name} = {val_str}\n")
    else:
        # Insert before the next section (or end of file), skipping trailing blanks
        insert_at = next_sec_idx
        while insert_at > general_idx + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines.insert(insert_at, f"{key_name} = {val_str}\n")

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass
