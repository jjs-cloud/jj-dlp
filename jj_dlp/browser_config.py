import re

_SUPPORTED_BROWSERS = [
    "firefox", "opera", "safari", "disabled",
]


def _read_browser_from_config(config_path: str) -> str:
    """
    Return the BROWSER value from the [General] section of *config_path*,
    or 'firefox' if not set. BROWSER is a single file-global value shared by
    [Downloader] and [Checker] — whichever sections opt in do so via their
    own COOKIES_FROM_BROWSER = true/false key.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return "firefox"

    in_general = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_general = stripped.lower() == "[general]"
            continue
        if in_general and "=" in stripped:
            key, _, value = stripped.partition("=")
            if key.strip().upper() == "BROWSER":
                value = value.strip().lower()
                if value:
                    return value

    return "firefox"


def _write_key_to_section(lines: list, section_name: str, key_name: str, value: str) -> None:
    """
    Set `key_name = value` inside [section_name], updating it in place if the
    key already exists (anywhere in the section) or inserting it as the
    section's first line otherwise. Creates the section at the end of the
    file if it doesn't exist yet. Mutates *lines* in place.
    """
    in_section = False
    section_line_idx = None
    key_line_idx = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == f"[{section_name.lower()}]":
            in_section = True
            section_line_idx = i
            continue
        if in_section:
            if stripped.startswith("["):          # entered a new section
                break
            if "=" in stripped:
                existing_key = stripped.split("=", 1)[0].strip()
                if existing_key.lower() == key_name.lower():
                    key_line_idx = i
                    break

    if key_line_idx is not None:
        indent = lines[key_line_idx][: len(lines[key_line_idx]) - len(lines[key_line_idx].lstrip())]
        lines[key_line_idx] = f"{indent}{key_name} = {value}\n"
    elif section_line_idx is not None:
        lines.insert(section_line_idx + 1, f"{key_name} = {value}\n")
    else:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n[{section_name}]\n")
        lines.append(f"{key_name} = {value}\n")


def _write_browser_to_config(config_path: str, browser: str, write_downloader: bool = True, write_checker: bool = False) -> None:
    """
    Record *browser* as the file-global BROWSER value in [General], and set
    COOKIES_FROM_BROWSER = true/false in [Downloader] and/or [Checker]
    according to write_downloader/write_checker. When browser == 'disabled',
    COOKIES_FROM_BROWSER is set to false in the selected sections (BROWSER
    itself is still recorded, so the picker can pre-select "disabled" again
    next time). Uses raw text manipulation to preserve the rest of the file
    exactly.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return

    enabled = "true" if browser != "disabled" else "false"

    _write_key_to_section(lines, "General", "BROWSER", browser)

    if write_downloader:
        _write_key_to_section(lines, "Downloader", "COOKIES_FROM_BROWSER", enabled)

    if write_checker:
        _write_key_to_section(lines, "Checker", "COOKIES_FROM_BROWSER", enabled)

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
