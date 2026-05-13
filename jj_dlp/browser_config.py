import re

_SUPPORTED_BROWSERS = [
    "brave", "chrome", "chromium", "edge",
    "firefox", "opera", "safari", "vivaldi", "whale",
    "disabled",
]

def _read_browser_from_config(config_path: str) -> str:
    """
    Return the browser name currently set after --cookies-from-browser in the
    [Downloader] section, or 'firefox' if not found.
    We use raw text scanning because configparser treats each line as a
    separate no-value key when the value sits on its own continuation line.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return "firefox"

    in_downloader = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == "[downloader]":
            in_downloader = True
            continue
        if in_downloader:
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
    return "firefox"


def _write_browser_to_config(config_path: str, browser: str) -> None:
    """
    Update the [Downloader] section so that --cookies-from-browser is followed
    by *browser* on the next line.  If browser == 'disabled', both the
    --cookies-from-browser line and the browser-name line are removed.
    Uses raw text manipulation to preserve the rest of the file exactly.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return

    in_downloader = False
    cookies_idx   = None   # index of the --cookies-from-browser line
    browser_idx   = None   # index of the browser-name line that follows it

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == "[downloader]":
            in_downloader = True
            continue
        if in_downloader:
            if stripped.startswith("["):
                break
            if stripped.lower() == "--cookies-from-browser":
                cookies_idx = i
                # Look for the browser name on the next non-blank line
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
        # Remove both lines (reverse order so indices stay valid)
        to_remove = sorted(
            [x for x in [cookies_idx, browser_idx] if x is not None],
            reverse=True,
        )
        for idx in to_remove:
            lines.pop(idx)
    else:
        if cookies_idx is not None and browser_idx is not None:
            # Replace the existing browser name in-place
            indent = lines[browser_idx][: len(lines[browser_idx]) - len(lines[browser_idx].lstrip())]
            lines[browser_idx] = f"{indent}{browser}\n"
        elif cookies_idx is not None:
            # No browser line existed — insert one right after --cookies-from-browser
            lines.insert(cookies_idx + 1, f"{browser}\n")
        else:
            # --cookies-from-browser not present at all — find the [Downloader]
            # section and insert both lines after it.
            for i, line in enumerate(lines):
                if line.strip().lower() == "[downloader]":
                    lines.insert(i + 1, f"{browser}\n")
                    lines.insert(i + 1, "--cookies-from-browser\n")
                    break

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
    import re as _re

    # Try to update an existing ASK_FOR_BROWSER line anywhere in the file
    for i, line in enumerate(lines):
        if _re.match(r"^\s*ASK_FOR_BROWSER\s*=", line, _re.IGNORECASE):
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
