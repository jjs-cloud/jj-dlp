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

_REPO_BASE = "https://github.com/jjs-cloud/jj-dlp"
_API_BASE   = "https://api.github.com/repos/jjs-cloud/jj-dlp"
_VALID_BRANCHES = {"main", "testing", "experimental"}

# ── Updater version ───────────────────────────────────────────────────────────
# Incremented independently of the main jj-dlp version so we can tell which
# updater logic is actually running during an update.
UPDATER_VERSION = "2.1.0"

# ── Lazy package imports ──────────────────────────────────────────────────────
# Relative imports are deferred to call time so this file is also safe to
# execute as a standalone script (the old stage-2 subprocess path).  When run
# as a script __name__ == "__main__" and relative imports would crash at parse
# time if they were at module scope.

# All compatibility code (stage-2, lazy imports) is set to be removed in version 3.0.0

def _logger():
    from . import logger as _l
    return _l

def _load_global_json() -> dict:
    from .main import _load_global_json as _f
    return _f()

def _save_global_json(data: dict) -> None:
    from .main import _save_global_json as _f
    _f(data)

def _get_preserved_keys() -> list:
    try:
        from .config_editor import PRESERVED_KEYS as _pk
        return _pk
    except ImportError:
        return []


class UpdateError(Exception):
    """Custom exception raised during updating."""
    pass


def _get_update_branch() -> str:
    """Return the configured update branch (falls back to 'main' if unset or invalid)."""
    try:
        from .main import load_global_config
        branch = load_global_config().get("update_branch", "main")
    except Exception:
        branch = "main"
    return branch if branch in _VALID_BRANCHES else "main"


def _repo_zip_url(branch: str) -> str:
    return f"{_REPO_BASE}/archive/refs/heads/{branch}.zip"


def _api_commits_url(branch: str) -> str:
    return f"{_API_BASE}/commits/{branch}"


def _fetch_latest_sha(branch: str) -> str | None:
    """Fetch the current HEAD SHA for *branch* from the GitHub API.

    Returns the SHA string, or ``None`` if the request fails.
    """
    try:
        req = urllib.request.Request(
            _api_commits_url(branch),
            headers={'User-Agent': 'jj-dlp-updater'},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
        sha = data.get('sha')
        _logger().dbg(f"[UPDATER] _fetch_latest_sha: branch={branch} sha={sha}")
        return sha or None
    except Exception as e:
        _logger().dbg(f"[UPDATER] _fetch_latest_sha: failed: {e}")
        return None


PRESERVED_SECTIONS = ["Streamers", "Block"]


# ─────────────────────────────────────────────────────────────────────────────

def check_for_updates_background():
    """Checks for updates in the background and saves the status to global.json."""
    try:
        branch = _get_update_branch()
        api_commits_url = _api_commits_url(branch)
        _logger().dbg(f"[UPDATER] check_for_updates_background: branch={branch} url={api_commits_url}")
        req = urllib.request.Request(api_commits_url, headers={'User-Agent': 'jj-dlp-updater'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
        latest_sha = data.get('sha')
        _logger().dbg(f"[UPDATER] check_for_updates_background: fetched latest_sha={latest_sha}")

        if not latest_sha:
            _logger().dbg("[UPDATER] check_for_updates_background: API response missing sha")
            return

        global_data = _load_global_json()
        current_sha = global_data.get('update_info', {}).get('current_sha')
        _logger().dbg(f"[UPDATER] check_for_updates_background: current_sha={current_sha}")

        update_info = global_data.setdefault('update_info', {})
        if current_sha:
            update_info['update_available'] = current_sha != latest_sha
        else:
            update_info['current_sha'] = latest_sha
            update_info['update_available'] = False

        update_info['latest_sha'] = latest_sha
        _logger().dbg(f"[UPDATER] check_for_updates_background: update_info current_sha={update_info.get('current_sha')} latest_sha={latest_sha} update_available={update_info.get('update_available')}")
        _save_global_json(global_data)
    except Exception as e:
        _logger().dbg(f"[UPDATER] check_for_updates_background: failed during update check: {e}")


def mark_update_completed(installed_sha: str | None = None):
    global_data = _load_global_json()
    update_info = global_data.setdefault('update_info', {})
    # Prefer the freshly-fetched SHA passed in by perform_update() so that
    # rapid back-to-back commits don't leave current_sha pointing at a stale
    # value and cause a spurious "Update Available" on the next launch.
    sha_to_record = installed_sha or update_info.get('latest_sha')
    if sha_to_record:
        update_info['current_sha'] = sha_to_record
        update_info['latest_sha'] = sha_to_record
    update_info['update_available'] = False
    _logger().dbg(f"[UPDATER] mark_update_completed: current_sha={update_info.get('current_sha')} latest_sha={update_info.get('latest_sha')} update_available=False")
    _save_global_json(global_data)
    _logger().dbg("[UPDATER] mark_update_completed: _save_global_json() returned")


def is_update_available():
    global_data = _load_global_json()
    return global_data.get('update_info', {}).get('update_available', False)


def get_base_dir():
    # Return the directory containing jj-dlp.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def perform_update():
    _logger().dbg(f"[UPDATER] perform_update: starting — updater version {UPDATER_VERSION}")
    print(f"\n--- jj-dlp Updater (v{UPDATER_VERSION}) ---")

    branch = _get_update_branch()
    repo_zip_url = _repo_zip_url(branch)
    _logger().dbg(f"[UPDATER] perform_update: branch={branch} url={repo_zip_url}")

    base_dir = get_base_dir()
    temp_dir = tempfile.mkdtemp(prefix="jj_dlp_update_")
    _logger().dbg(f"[UPDATER] perform_update: base_dir={base_dir} temp_dir={temp_dir}")
    print(f"Temporary files will be saved to: {temp_dir}")

    # ── Step 1: Download ──────────────────────────────────────────────────────
    print(f"Downloading latest version from GitHub (branch: {branch})...")
    zip_path = os.path.join(temp_dir, "main.zip")
    try:
        req = urllib.request.Request(repo_zip_url, headers={'User-Agent': 'jj-dlp-updater'})
        with urllib.request.urlopen(req, timeout=30) as response:
            with open(zip_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
        _logger().dbg(f"[UPDATER] perform_update: downloaded zip to {zip_path}")
    except Exception as e:
        _logger().dbg(f"[UPDATER] perform_update: download failed: {e}")
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
        _logger().dbg(f"[UPDATER] perform_update: extracted zip to {extract_dir}")
    except Exception as e:
        _logger().dbg(f"[UPDATER] perform_update: extraction failed: {e}")
        print(f"Error extracting update: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    # The zip usually contains a single folder like 'jj-dlp-main'
    extracted_items = os.listdir(extract_dir)
    if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_items[0])):
        source_dir = os.path.join(extract_dir, extracted_items[0])
    else:
        source_dir = extract_dir
    _logger().dbg(f"[UPDATER] perform_update: source_dir resolved to {source_dir}")

    try:
        # ── Step 3: Merge configs ─────────────────────────────────────────────
        actual_diff_dir = os.path.join(base_dir, "diff")
        if os.path.exists(actual_diff_dir):
            shutil.rmtree(actual_diff_dir, ignore_errors=True)
        os.makedirs(actual_diff_dir, exist_ok=True)
        _logger().dbg(f"[UPDATER] perform_update: diff dir cleared and recreated at {actual_diff_dir}")
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
            _logger().dbg(f"[UPDATER] perform_update: duplicate configs found: {duplicates}")

        _logger().dbg(f"[UPDATER] perform_update: found {len(config_files)} user config file(s) to merge")

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
        _logger().dbg(f"[UPDATER] perform_update: new_config_map keys={list(new_config_map.keys())}")

        for user_cfg in config_files:
            fname = os.path.basename(user_cfg)
            if fname not in new_config_map:
                _logger().dbg(f"[UPDATER] perform_update: no matching new config for {fname}, skipping merge")
                continue
            new_cfg_path = new_config_map[fname]
            _logger().dbg(f"[UPDATER] perform_update: merging config {user_cfg} -> {new_cfg_path}")

            with open(user_cfg, 'r', encoding='utf-8') as f:
                old_content = f.read()
            with open(new_cfg_path, 'r', encoding='utf-8') as f:
                new_content = f.read()

            streamers = get_old_config_section(user_cfg, "Streamers")
            blocked   = get_old_config_section(user_cfg, "Block")
            _logger().dbg(f"[UPDATER] perform_update: preserved sections for {fname}: streamers={bool(streamers)} block={bool(blocked)}")

            merged_content = inject_preserved_keys(new_content, user_cfg)
            merged_content = update_config_comments(merged_content)
            merged_content = replace_section(merged_content, "Streamers", streamers)
            merged_content = replace_section(merged_content, "Block", blocked)

            create_diff(old_content, merged_content, user_cfg, actual_diff_dir)

            with open(new_cfg_path, 'w', encoding='utf-8') as f:
                f.write(merged_content)
            _logger().dbg(f"[UPDATER] perform_update: merged config written to {new_cfg_path}")

        # ── Step 4: Copy files source_dir → base_dir ─────────────────────────
        print("Installing new files...")
        _logger().dbg(f"[UPDATER] perform_update: copying files {source_dir} -> {base_dir}")

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
                    _logger().dbg(f"[UPDATER] perform_update: skipping global.json at {dst}")
                    return
                if os.path.exists(dst):
                    if not _is_binary(dst) and not dst.endswith(".conf"):
                        with open(dst, 'r', encoding='utf-8', errors='ignore') as f:
                            old_content = f.read()
                        with open(src, 'r', encoding='utf-8', errors='ignore') as f:
                            new_content = f.read()
                        if old_content != new_content:
                            create_diff(old_content, new_content, dst, actual_diff_dir)
                    if os.path.basename(dst) == "updater.py":
                        _logger().dbg(f"[UPDATER] perform_update: updater.py changed, copying {src} -> {dst}")
                else:
                    if os.path.basename(dst) == "updater.py":
                        _logger().dbg(f"[UPDATER] perform_update: installing new updater.py {src} -> {dst}")
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
        _logger().dbg("[UPDATER] perform_update: all files copied from source to base")

        # ── Step 5: Set executable bits on bin/ ───────────────────────────────
        print("Setting executable permissions on bin/ files...")
        _mark_bin_executable(base_dir)
        _logger().dbg("[UPDATER] perform_update: bin/ permissions set")

        # ── Step 6: Mark update completed in global.json ──────────────────────
        # Re-fetch the latest SHA now that the install is done.  If additional
        # commits landed on the branch between when we started the download and
        # now, this ensures current_sha always matches whatever HEAD is at this
        # moment, preventing a spurious "Update Available" on the next launch.
        post_install_sha = _fetch_latest_sha(branch)
        _logger().dbg(f"[UPDATER] perform_update: post-install SHA fetch: {post_install_sha}")
        mark_update_completed(installed_sha=post_install_sha)
        _logger().dbg("[UPDATER] perform_update: marked update completed")

        print("\n" + "="*60)
        print("✅ Update completed successfully!")
        print(f"   Diff files are available in the 'diff' directory.")
        print("="*60)
        print("\nℹ️  Please restart jj-dlp for the new version to take effect.")

    except UpdateError as e:
        _logger().dbg(f"[UPDATER] perform_update: clean abort: {e}")
    except Exception as e:
        _logger().dbg(f"[UPDATER] perform_update: exception during update: {e}")
        print(f"Error during update: {e}")
        traceback.print_exc()
    finally:
        print(f"Cleaning up temporary directory: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        _logger().dbg(f"[UPDATER] perform_update: temp_dir cleaned up")
        input("Press any key to exit...")


def get_old_config_section(config_path, section_name):
    try:
        parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
        parser.read(config_path, encoding='utf-8')
        if parser.has_section(section_name):
            return "\n".join([f"{k}" for k, v in parser.items(section_name)])
    except Exception as e:
        message = (
            f"WARNING: Failed to preserve [{section_name}] from '{config_path}'. "
            "The config file may be corrupted or invalid. "
            "The updater will continue, but this section may not be preserved."
        )
        print(message)
        print("Press Enter to continue or Ctrl+C to abort.")
        try:
            input()
        except KeyboardInterrupt:
            raise
        _logger().dbg(f"[UPDATER] get_old_config_section: {message} exception={e}")
    return ""


def inject_preserved_keys(new_text, old_config_path):
    parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
    try:
        parser.read(old_config_path, encoding='utf-8')
    except Exception:
        return new_text

    for key in _get_preserved_keys():
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


def update_config_comments(text):
    """Replace or insert the canonical comment line immediately above each
    CONFIG_KEYS entry found in the [General] section.

    Rules:
    - Only touches lines inside [General]; other sections are left unchanged.
    - If the line immediately preceding a key assignment starts with '#', it is
      replaced with the comment from CONFIG_KEYS.
    - If there is no preceding comment line, one is inserted.
    - Multi-line comment blocks are not collapsed; only the single line
      immediately above the key is considered.
    - Keys not present in CONFIG_KEYS are left untouched.
    """
    # Resolve CONFIG_KEYS whether called as a package or as __main__.
    try:
        from .config_editor import CONFIG_KEYS as _ck
    except ImportError:
        try:
            _pkg_dir = os.path.dirname(os.path.abspath(__file__))
            _proj_root = os.path.dirname(_pkg_dir)
            if _proj_root not in sys.path:
                sys.path.insert(0, _proj_root)
            from jj_dlp.config_editor import CONFIG_KEYS as _ck
        except Exception:
            return text

    comment_map = {kdef.name.upper(): kdef.comment for kdef in _ck}

    lines = text.splitlines(keepends=True)
    in_general = False
    result = []

    for line in lines:
        stripped = line.strip()

        # Track section changes.
        if stripped.startswith('[') and stripped.endswith(']'):
            in_general = (stripped[1:-1].lower() == 'general')
            result.append(line)
            continue

        # Only process key assignments inside [General].
        if in_general and '=' in stripped and not stripped.startswith('#'):
            key_part = stripped.split('=', 1)[0].strip().upper()
            if key_part in comment_map:
                new_comment = f"# {comment_map[key_part]}\n"
                # Replace the immediately preceding comment, or insert one.
                if result and result[-1].strip().startswith('#'):
                    result[-1] = new_comment
                else:
                    result.append(new_comment)
                result.append(line)
                continue

        result.append(line)

    return ''.join(result)


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


def _is_binary(path: str) -> bool:
    """Return True if *path* looks like a binary file (contains a null byte in the first 8 KB)."""
    try:
        with open(path, 'rb') as f:
            return b'\x00' in f.read(8192)
    except Exception:
        return False


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


# ── Standalone stage-2 entry point (transitional compatibility shim) ──────────
#
# The OLD installed updater.py (pre-v2) downloads this file and runs it as:
#
#   python /tmp/.../jj_dlp/updater.py --stage2 <source_dir> <base_dir> <temp_dir>
#
# Because it is executed as a plain script (not a package), relative imports
# fail.  This block catches that invocation and performs the copy/install work
# using only stdlib + the helper functions defined above (which are already in
# module scope by the time __main__ runs).
#
# Once this version is installed, the old subprocess launch code is gone and
# this block will never be reached again.  It is dead code from v3 onward.
#
if __name__ == "__main__":
    import errno as _errno

    if len(sys.argv) == 5 and sys.argv[1] == "--stage2":
        _source_dir = sys.argv[2]
        _base_dir   = sys.argv[3]
        _temp_dir   = sys.argv[4]

        # ── Minimal standalone logger: write to the same debug.log the old
        #    stage-2 would have used (JJ_DLP_DEBUG_LOG_DIR env var, or next
        #    to this file as a fallback).
        def _sdbg(msg: str) -> None:
            try:
                _forced_dir = os.environ.get("JJ_DLP_DEBUG_LOG_DIR")
                _log_dir = _forced_dir if _forced_dir else os.path.dirname(os.path.abspath(__file__))
                os.makedirs(_log_dir, exist_ok=True)
                _ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                with open(os.path.join(_log_dir, "debug.log"), "a", encoding="utf-8") as _lf:
                    _lf.write(f"[{_ts}] [UPDATER][STAGE2-COMPAT] {msg}\n")
            except Exception:
                pass

        # ── Standalone global.json helpers (env var path from old launcher) ──
        def _json_path() -> str:
            p = os.environ.get("JJ_DLP_GLOBAL_JSON_PATH")
            if p:
                return p
            d = os.environ.get("JJ_DLP_GLOBAL_DIR")
            if d:
                return os.path.join(d, "global.json")
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), "global.json")

        def _load_json() -> dict:
            try:
                with open(_json_path(), "r", encoding="utf-8") as _f:
                    _d = json.load(_f)
                return _d if isinstance(_d, dict) else {}
            except Exception:
                return {}

        def _save_json(data: dict) -> None:
            try:
                with open(_json_path(), "w", encoding="utf-8") as _f:
                    json.dump(data, _f, indent=2)
            except Exception:
                pass

        def _mark_done() -> None:
            _gd = _load_json()
            _ui = _gd.setdefault("update_info", {})
            _ls = _ui.get("latest_sha")
            if _ls:
                _ui["current_sha"] = _ls
            _ui["update_available"] = False
            _sdbg(f"mark_done: current_sha={_ui.get('current_sha')} update_available=False")
            _save_json(_gd)

        # ── PRESERVED_KEYS: read from config_editor if importable, else [] ───
        try:
            _pkg_dir = os.path.dirname(os.path.abspath(__file__))
            _proj_root = os.path.dirname(_pkg_dir)
            if _proj_root not in sys.path:
                sys.path.insert(0, _proj_root)
            from jj_dlp.config_editor import PRESERVED_KEYS as _PKEYS
        except Exception:
            _PKEYS = []

        _sdbg(f"stage2-compat starting: source={_source_dir} base={_base_dir} temp={_temp_dir}")
        print("Running stage 2 of update (compat mode)...")

        try:
            _diff_dir = os.path.join(_base_dir, "diff")
            if os.path.exists(_diff_dir):
                shutil.rmtree(_diff_dir, ignore_errors=True)
            os.makedirs(_diff_dir, exist_ok=True)
            print(f"Diffs will be saved to: {_diff_dir}")

            # ── Collect user configs ──────────────────────────────────────────
            _cfg_files = []
            _cfgs_dir = os.path.join(_base_dir, "configs")
            for _root_f in (os.listdir(_base_dir) if os.path.exists(_base_dir) else []):
                if _root_f.endswith(".conf") and os.path.isfile(os.path.join(_base_dir, _root_f)):
                    _cfg_files.append(os.path.join(_base_dir, _root_f))
            for _sub_f in (os.listdir(_cfgs_dir) if os.path.exists(_cfgs_dir) else []):
                if _sub_f.endswith(".conf") and os.path.isfile(os.path.join(_cfgs_dir, _sub_f)):
                    _cfg_files.append(os.path.join(_cfgs_dir, _sub_f))

            # ── Collect new configs from source ───────────────────────────────
            _new_cfgs = []
            _src_cfgs = os.path.join(_source_dir, "configs")
            if os.path.exists(_src_cfgs):
                _new_cfgs += [os.path.join(_src_cfgs, f) for f in os.listdir(_src_cfgs) if f.endswith(".conf")]
            if os.path.exists(os.path.join(_source_dir, "jj-dlp.conf")):
                _new_cfgs.append(os.path.join(_source_dir, "jj-dlp.conf"))
            _new_cfg_map = {os.path.basename(p): p for p in _new_cfgs}

            # ── Merge each user config ────────────────────────────────────────
            for _ucfg in _cfg_files:
                _fn = os.path.basename(_ucfg)
                if _fn not in _new_cfg_map:
                    continue
                _ncfg = _new_cfg_map[_fn]
                with open(_ucfg, "r", encoding="utf-8") as _f:
                    _old_txt = _f.read()
                with open(_ncfg, "r", encoding="utf-8") as _f:
                    _new_txt = _f.read()
                _streamers = get_old_config_section(_ucfg, "Streamers")
                _blocked   = get_old_config_section(_ucfg, "Block")

                # inline inject_preserved_keys using _PKEYS
                _parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
                try:
                    _parser.read(_ucfg, encoding="utf-8")
                except Exception:
                    pass
                for _key in _PKEYS:
                    _oval = None
                    for _sec in _parser.sections():
                        if _parser.has_option(_sec, _key):
                            _oval = _parser.get(_sec, _key)
                            break
                    if _oval is not None:
                        _pat = re.compile(rf"^([ \t]*{_key}[ \t]*=).*$", re.IGNORECASE | re.MULTILINE)
                        if _pat.search(_new_txt):
                            _new_txt = _pat.sub(lambda m, v=_oval: f"{m.group(1)} {v}", _new_txt)

                _new_txt = update_config_comments(_new_txt)
                _new_txt = replace_section(_new_txt, "Streamers", _streamers)
                _new_txt = replace_section(_new_txt, "Block", _blocked)
                create_diff(_old_txt, _new_txt, _ucfg, _diff_dir)
                with open(_ncfg, "w", encoding="utf-8") as _f:
                    _f.write(_new_txt)
                _sdbg(f"merged config {_fn}")

            # ── Copy files source → base ──────────────────────────────────────
            print("Installing new files...")

            def _copy(src, dst):
                if os.path.isdir(src):
                    if os.path.basename(src) == "__pycache__":
                        return
                    os.makedirs(dst, exist_ok=True)
                    for _item in os.listdir(src):
                        _copy(os.path.join(src, _item), os.path.join(dst, _item))
                else:
                    if dst.endswith(".pyc") or os.path.basename(dst) == "global.json":
                        return
                    if os.path.exists(dst) and not _is_binary(dst) and not dst.endswith(".conf"):
                        with open(dst, "r", encoding="utf-8", errors="ignore") as _f:
                            _oc = _f.read()
                        with open(src, "r", encoding="utf-8", errors="ignore") as _f:
                            _nc = _f.read()
                        if _oc != _nc:
                            create_diff(_oc, _nc, dst, _diff_dir)
                    try:
                        shutil.copy2(src, dst)
                    except OSError as _e:
                        if getattr(_e, "errno", None) == _errno.ETXTBSY:
                            print(f"\nERROR: '{dst}' is in use (Text file busy). Close yt-dlp/ffmpeg and retry.\n")
                            raise UpdateError(f"'{dst}' is in use.")
                        raise

            _copy(_source_dir, _base_dir)
            _sdbg("files copied")

            print("Setting executable permissions on bin/ files...")
            _mark_bin_executable(_base_dir)

            _mark_done()
            _sdbg("update marked complete")

            print("\n" + "=" * 60)
            print("✅ Update completed successfully!")
            print("   Diff files are available in the 'diff' directory.")
            print("=" * 60)
            print("\nℹ️  Please restart jj-dlp for the new version to take effect.")

        except UpdateError:
            pass
        except Exception as _e:
            _sdbg(f"exception: {_e}")
            print(f"Error during stage 2: {_e}")
            traceback.print_exc()
        finally:
            print(f"Cleaning up temporary directory: {_temp_dir}")
            shutil.rmtree(_temp_dir, ignore_errors=True)
            input("\nPress Enter to exit...")