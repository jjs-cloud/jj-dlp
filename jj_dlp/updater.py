import os
import sys
import tempfile
import urllib.request
import zipfile
import shutil
import difflib
import configparser
import re
import json
import traceback
import datetime

from . import logger
from .main import load_config, _load_global_json, _save_global_json

_REPO_BASE = "https://github.com/jjs-cloud/jj-dlp"
_API_BASE   = "https://api.github.com/repos/jjs-cloud/jj-dlp"
_VALID_BRANCHES = {"main", "testing", "experimental"}

# ── Updater version ───────────────────────────────────────────────────────────
# Incremented independently of the main jj-dlp version so we can tell which
# updater logic is actually running during an update.
UPDATER_VERSION = "2.0.0"


class UpdateError(Exception):
    """Custom exception raised during updating."""
    pass


def _get_update_branch() -> str:
    """Return the configured update branch (falls back to 'main' if unset or invalid)."""
    try:
        from jj_dlp.main import load_global_config
        branch = load_global_config().get("update_branch", "main")
    except Exception:
        branch = "main"
    return branch if branch in _VALID_BRANCHES else "main"


def _repo_zip_url(branch: str) -> str:
    return f"{_REPO_BASE}/archive/refs/heads/{branch}.zip"


def _api_commits_url(branch: str) -> str:
    return f"{_API_BASE}/commits/{branch}"


PRESERVED_SECTIONS = ["Streamers", "Block"]

# ── Preserved keys: imported from the single source of truth in config_editor ─
from .config_editor import PRESERVED_KEYS


# ─────────────────────────────────────────────────────────────────────────────

def check_for_updates_background():
    """Checks for updates in the background and saves the status to global.json."""
    try:
        branch = _get_update_branch()
        api_commits_url = _api_commits_url(branch)
        logger.dbg(f"[UPDATER] check_for_updates_background: branch={branch} url={api_commits_url}")
        req = urllib.request.Request(api_commits_url, headers={'User-Agent': 'jj-dlp-updater'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
        latest_sha = data.get('sha')
        logger.dbg(f"[UPDATER] check_for_updates_background: fetched latest_sha={latest_sha}")

        if not latest_sha:
            logger.dbg("[UPDATER] check_for_updates_background: API response missing sha")
            return

        global_data = _load_global_json()
        current_sha = global_data.get('update_info', {}).get('current_sha')
        logger.dbg(f"[UPDATER] check_for_updates_background: current_sha={current_sha}")

        update_info = global_data.setdefault('update_info', {})
        if current_sha:
            update_info['update_available'] = current_sha != latest_sha
        else:
            update_info['current_sha'] = latest_sha
            update_info['update_available'] = False

        update_info['latest_sha'] = latest_sha
        logger.dbg(f"[UPDATER] check_for_updates_background: update_info current_sha={update_info.get('current_sha')} latest_sha={latest_sha} update_available={update_info.get('update_available')}")
        _save_global_json(global_data)
    except Exception as e:
        logger.dbg(f"[UPDATER] check_for_updates_background: failed during update check: {e}")


def mark_update_completed():
    global_data = _load_global_json()
    update_info = global_data.setdefault('update_info', {})
    latest_sha = update_info.get('latest_sha')
    if latest_sha:
        update_info['current_sha'] = latest_sha
    update_info['update_available'] = False
    logger.dbg(f"[UPDATER] mark_update_completed: updating update_info to current_sha={update_info.get('current_sha')} latest_sha={update_info.get('latest_sha')} update_available={update_info.get('update_available')}")
    _save_global_json(global_data)
    logger.dbg("[UPDATER] mark_update_completed: _save_global_json() returned")


def is_update_available():
    global_data = _load_global_json()
    return global_data.get('update_info', {}).get('update_available', False)


def get_base_dir():
    # Return the directory containing jj-dlp.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def perform_update():
    logger.dbg(f"[UPDATER] perform_update: starting — updater version {UPDATER_VERSION}")
    print(f"\n--- jj-dlp Updater (v{UPDATER_VERSION}) ---")

    branch = _get_update_branch()
    repo_zip_url = _repo_zip_url(branch)
    logger.dbg(f"[UPDATER] perform_update: branch={branch} url={repo_zip_url}")

    base_dir = get_base_dir()
    temp_dir = tempfile.mkdtemp(prefix="jj_dlp_update_")
    logger.dbg(f"[UPDATER] perform_update: base_dir={base_dir} temp_dir={temp_dir}")
    print(f"Temporary files will be saved to: {temp_dir}")

    # ── Step 1: Download ──────────────────────────────────────────────────────
    print(f"Downloading latest version from GitHub (branch: {branch})...")
    zip_path = os.path.join(temp_dir, "main.zip")
    try:
        req = urllib.request.Request(repo_zip_url, headers={'User-Agent': 'jj-dlp-updater'})
        with urllib.request.urlopen(req, timeout=30) as response:
            with open(zip_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
        logger.dbg(f"[UPDATER] perform_update: downloaded zip to {zip_path}")
    except Exception as e:
        logger.dbg(f"[UPDATER] perform_update: download failed: {e}")
        print(f"Error downloading update: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    # ── Step 2: Extract ───────────────────────────────────────────────────────
    print("Extracting files...")
    extract_dir = os.path.join(temp_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        logger.dbg(f"[UPDATER] perform_update: extracted zip to {extract_dir}")
    except Exception as e:
        logger.dbg(f"[UPDATER] perform_update: extraction failed: {e}")
        print(f"Error extracting update: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    # The zip usually contains a single folder like 'jj-dlp-main'
    extracted_items = os.listdir(extract_dir)
    if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_items[0])):
        source_dir = os.path.join(extract_dir, extracted_items[0])
    else:
        source_dir = extract_dir
    logger.dbg(f"[UPDATER] perform_update: source_dir resolved to {source_dir}")

    try:
        # ── Step 3: Merge configs ─────────────────────────────────────────────
        actual_diff_dir = os.path.join(base_dir, "diff")
        if os.path.exists(actual_diff_dir):
            shutil.rmtree(actual_diff_dir, ignore_errors=True)
        os.makedirs(actual_diff_dir, exist_ok=True)
        logger.dbg(f"[UPDATER] perform_update: diff dir cleared and recreated at {actual_diff_dir}")
        print(f"Diffs will be saved to: {actual_diff_dir}")

        # Collect user config files (root + configs/)
        config_files = []
        configs_dir = os.path.join(base_dir, "configs")
        root_configs = [
            f for f in os.listdir(base_dir)
            if f.endswith(".conf") and os.path.isfile(os.path.join(base_dir, f))
        ] if os.path.exists(base_dir) else []
        sub_configs = [
            f for f in os.listdir(configs_dir)
            if f.endswith(".conf") and os.path.isfile(os.path.join(configs_dir, f))
        ] if os.path.exists(configs_dir) else []

        all_config_names: set = set()
        duplicates: set = set()
        for f in root_configs:
            all_config_names.add(f)
            config_files.append(os.path.join(base_dir, f))
        for f in sub_configs:
            if f in all_config_names:
                duplicates.add(f)
            else:
                all_config_names.add(f)
            config_files.append(os.path.join(configs_dir, f))

        if duplicates:
            print(f"\nWARNING: Found duplicate config files in root and configs/ directory: {', '.join(duplicates)}")
            print("The updater may overwrite or merge them unpredictably. Please consolidate them later.\n")
            logger.dbg(f"[UPDATER] perform_update: duplicate configs found: {duplicates}")

        logger.dbg(f"[UPDATER] perform_update: found {len(config_files)} user config file(s) to merge")

        # Collect new configs from source_dir
        new_configs = []
        src_configs_dir = os.path.join(source_dir, "configs")
        if os.path.exists(src_configs_dir):
            new_configs.extend([
                os.path.join(src_configs_dir, f)
                for f in os.listdir(src_configs_dir) if f.endswith(".conf")
            ])
        if os.path.exists(os.path.join(source_dir, "jj-dlp.conf")):
            new_configs.append(os.path.join(source_dir, "jj-dlp.conf"))

        new_config_map = {os.path.basename(p): p for p in new_configs}
        logger.dbg(f"[UPDATER] perform_update: new_config_map keys={list(new_config_map.keys())}")

        for user_cfg in config_files:
            fname = os.path.basename(user_cfg)
            if fname not in new_config_map:
                logger.dbg(f"[UPDATER] perform_update: no matching new config for {fname}, skipping merge")
                continue
            new_cfg_path = new_config_map[fname]
            logger.dbg(f"[UPDATER] perform_update: merging config {user_cfg} -> {new_cfg_path}")

            with open(user_cfg, 'r', encoding='utf-8') as f:
                old_content = f.read()
            with open(new_cfg_path, 'r', encoding='utf-8') as f:
                new_content = f.read()

            streamers = get_old_config_section(user_cfg, "Streamers")
            blocked   = get_old_config_section(user_cfg, "Block")
            logger.dbg(f"[UPDATER] perform_update: preserved sections for {fname}: streamers={bool(streamers)} block={bool(blocked)}")

            merged_content = inject_preserved_keys(new_content, user_cfg)
            merged_content = replace_section(merged_content, "Streamers", streamers)
            merged_content = replace_section(merged_content, "Block", blocked)

            create_diff(old_content, merged_content, user_cfg, actual_diff_dir)

            with open(new_cfg_path, 'w', encoding='utf-8') as f:
                f.write(merged_content)
            logger.dbg(f"[UPDATER] perform_update: merged config written to {new_cfg_path}")

        # ── Step 4: Copy files source_dir → base_dir ─────────────────────────
        print("Installing new files...")
        logger.dbg(f"[UPDATER] perform_update: copying files {source_dir} -> {base_dir}")

        def copy_and_diff(src, dst):
            if os.path.isdir(src):
                if os.path.basename(src) == "__pycache__":
                    return
                os.makedirs(dst, exist_ok=True)
                for item in os.listdir(src):
                    copy_and_diff(os.path.join(src, item), os.path.join(dst, item))
            else:
                if os.path.basename(dst).endswith(".pyc"):
                    return
                if os.path.basename(dst) == "global.json":
                    logger.dbg(f"[UPDATER] perform_update: skipping global.json at {dst}")
                    return
                if os.path.exists(dst):
                    with open(dst, 'r', encoding='utf-8', errors='ignore') as f:
                        old_content = f.read()
                    with open(src, 'r', encoding='utf-8', errors='ignore') as f:
                        new_content = f.read()
                    if old_content != new_content and not dst.endswith(".conf"):
                        create_diff(old_content, new_content, dst, actual_diff_dir)
                        if os.path.basename(dst) == "updater.py":
                            logger.dbg(f"[UPDATER] perform_update: updater.py changed, copying {src} -> {dst}")
                else:
                    if os.path.basename(dst) == "updater.py":
                        logger.dbg(f"[UPDATER] perform_update: installing new updater.py {src} -> {dst}")
                try:
                    shutil.copy2(src, dst)
                except OSError as e:
                    import errno
                    if getattr(e, 'errno', None) == errno.ETXTBSY:
                        print(f"\nERROR: The file '{dst}' is currently in use (Text file busy).")
                        print("Please ensure that jj-dlp, yt-dlp, and ffmpeg are fully closed and not running in the background.")
                        print("You may need to manually kill any stuck 'yt-dlp' or 'ffmpeg' processes and try again.\n")
                        raise UpdateError(f"The file '{dst}' is currently in use (Text file busy).")
                    raise

        copy_and_diff(source_dir, base_dir)
        logger.dbg("[UPDATER] perform_update: all files copied from source to base")

        # ── Step 5: Set executable bits on bin/ ───────────────────────────────
        print("Setting executable permissions on bin/ files...")
        _mark_bin_executable(base_dir)
        logger.dbg("[UPDATER] perform_update: bin/ permissions set")

        # ── Step 6: Mark update completed in global.json ──────────────────────
        mark_update_completed()
        logger.dbg("[UPDATER] perform_update: marked update completed")

        print("\n" + "="*60)
        print("✅ Update completed successfully!")
        print(f"   Diff files are available in the 'diff' directory.")
        print("="*60)
        print("\nℹ️  Please restart jj-dlp for the new version to take effect.")

    except UpdateError as e:
        logger.dbg(f"[UPDATER] perform_update: clean abort: {e}")
    except Exception as e:
        logger.dbg(f"[UPDATER] perform_update: exception during update: {e}")
        print(f"Error during update: {e}")
        traceback.print_exc()
    finally:
        print(f"Cleaning up temporary directory: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.dbg(f"[UPDATER] perform_update: temp_dir cleaned up")


def get_old_config_section(config_path, section_name):
    try:
        parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
        parser.read(config_path, encoding='utf-8')
        if parser.has_section(section_name):
            return "\n".join([f"{k}" for k, v in parser.items(section_name)])
    except Exception:
        pass
    return ""


def inject_preserved_keys(new_text, old_config_path):
    parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
    try:
        parser.read(old_config_path, encoding='utf-8')
    except Exception:
        return new_text

    for key in PRESERVED_KEYS:
        old_val = None
        for sec in parser.sections():
            if parser.has_option(sec, key):
                old_val = parser.get(sec, key)
                break
        if old_val is not None:
            pattern = re.compile(rf"^([ \t]*{key}[ \t]*=).*$", re.IGNORECASE | re.MULTILINE)
            if pattern.search(new_text):
                new_text = pattern.sub(lambda m, val=old_val: f"{m.group(1)} {val}", new_text)
    return new_text


def replace_section(text, sec_name, new_content):
    lines = text.splitlines()
    out = []
    in_sec = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped.lower() == f"[{sec_name.lower()}]":
                in_sec = True
                out.append(line)
                if new_content and new_content.strip():
                    out.append(new_content.strip())
                out.append("")
                replaced = True
                continue
            else:
                in_sec = False

        if not in_sec:
            out.append(line)

    if not replaced:
        out.append(f"\n[{sec_name}]")
        if new_content and new_content.strip():
            out.append(new_content.strip())
        out.append("")

    return "\n".join(out)


def _mark_bin_executable(base_dir):
    """Mark all files in <base_dir>/bin/ as executable on Linux and macOS."""
    if sys.platform == "win32":
        return
    bin_dir = os.path.join(base_dir, "bin")
    if not os.path.isdir(bin_dir):
        return
    for fname in os.listdir(bin_dir):
        fpath = os.path.join(bin_dir, fname)
        if not os.path.isfile(fpath):
            continue
        current = os.stat(fpath).st_mode
        executable_mode = current | 0o111
        if current != executable_mode:
            os.chmod(fpath, executable_mode)
            print(f"  Marked executable: bin/{fname}")


def create_diff(old_content, new_content, file_path, diff_dir):
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"old/{os.path.basename(file_path)}",
        tofile=f"new/{os.path.basename(file_path)}"
    ))

    if diff:
        rel_path = os.path.basename(file_path)
        diff_file = os.path.join(diff_dir, f"{rel_path}.diff")
        with open(diff_file, 'w', encoding='utf-8') as f:
            f.writelines(diff)
