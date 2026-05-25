import os
import sys
import tempfile
import urllib.request
import zipfile
import shutil
import difflib
import subprocess
import configparser
import re
import json
import traceback
import datetime

# When run directly as a script (e.g. stage-2 update), ensure the project root
# is on sys.path so that jj_dlp is importable as a proper package.
_pkg_dir = os.path.dirname(os.path.abspath(__file__))   # …/jj_dlp
_project_root = os.path.dirname(_pkg_dir)               # …/jj-dlp
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Adjust imports to work whether run as module or script.
try:
    from . import logger
    from .main import load_config, _load_global_json, _save_global_json
except ImportError:
    from jj_dlp import logger
    from jj_dlp.main import load_config, _load_global_json, _save_global_json

_REPO_BASE = "https://github.com/jjs-cloud/jj-dlp"
_API_BASE   = "https://api.github.com/repos/jjs-cloud/jj-dlp"
_VALID_BRANCHES = {"main", "testing", "experimental"}


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
# The authoritative definition lives in config_editor.CONFIG_KEYS (preserve=True).
# Importing here avoids maintaining a separate list in this file.
try:
    from .config_editor import PRESERVED_KEYS
except ImportError:
    from jj_dlp.config_editor import PRESERVED_KEYS

# ── Per-tag debug filter ───────────────────────────────────────────────────────
# Controls which [TAG] groups appear in updater's debug.log.
# updater.py has its own copy because it can run as a standalone stage-2 script
# (no access to logger.py's DBG_FILTERS).  Tags used here:
#
#   UPDATE_CHECK — check_for_updates_background, is_update_available
#   MARK_DONE    — mark_update_completed
#   PERFORM      — perform_update download / extract / stage2 launch
#   STAGE2       — _stage2 copy and install logic
#
UPDATER_DBG_FILTERS: dict[str, bool] = {
    "UPDATE_CHECK": True,
    "MARK_DONE":    True,
    "PERFORM":      True,
    "STAGE2":       True,
}


def dbg(msg: str, exc: Exception | None = None) -> None:
    """Append a timestamped debug message to debug.log next to this package.

    The message is dropped if its leading [TAG] appears in UPDATER_DBG_FILTERS
    with a value of False.  Messages with no recognised [TAG] always pass through.

    Designed to be safe to call when updater.py is executed as a standalone
    stage-2 script.  Dumps optional exception tracebacks and keeps a simple
    append-only log for post-mortem inspection.
    """
    # ── Tag-based filter ──────────────────────────────────────────────────────
    if msg.startswith("["):
        end = msg.find("]")
        if end > 1:
            tag = msg[1:end]
            if not UPDATER_DBG_FILTERS.get(tag, True):
                return

    try:
        # Allow tests or stage2 to redirect logs to a specific directory.
        forced_dir = os.environ.get('JJ_DLP_DEBUG_LOG_DIR')
        if forced_dir:
            try:
                os.makedirs(forced_dir, exist_ok=True)
            except Exception:
                pass
            log_path = os.path.join(forced_dir, "debug.log")
        else:
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"[{ts}] {msg}\n")
            if exc is not None:
                lf.write("""Exception traceback:\n""")
                lf.write(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
                lf.write("\n")
    except Exception:
        # Never raise from the debugger helper
        pass


# ─────────────────────────────────────────────────────────────────────────────

def check_for_updates_background():
    """Checks for updates in the background and saves the status to global.json."""
    try:
        branch = _get_update_branch()
        api_commits_url = _api_commits_url(branch)
        dbg(f"[UPDATE_CHECK] check_for_updates_background: branch={branch} url={api_commits_url}")
        req = urllib.request.Request(api_commits_url, headers={'User-Agent': 'jj-dlp-updater'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
        latest_sha = data.get('sha')
        dbg(f"[UPDATE_CHECK] check_for_updates_background: fetched latest_sha={latest_sha}")

        if not latest_sha:
            dbg("[UPDATE_CHECK] check_for_updates_background: API response missing sha")
            return

        global_data = _load_global_json()
        current_sha = global_data.get('update_info', {}).get('current_sha')
        dbg(f"[UPDATE_CHECK] check_for_updates_background: current_sha={current_sha}")

        update_info = global_data.setdefault('update_info', {})
        if current_sha:
            update_info['update_available'] = current_sha != latest_sha
        else:
            update_info['current_sha'] = latest_sha
            update_info['update_available'] = False

        update_info['latest_sha'] = latest_sha
        dbg(f"[UPDATE_CHECK] check_for_updates_background: update_info current_sha={update_info.get('current_sha')} latest_sha={latest_sha} update_available={update_info.get('update_available')}")
        _save_global_json(global_data)
    except Exception as e:
        dbg("[UPDATE_CHECK] check_for_updates_background: failed during update check", e)


def mark_update_completed():
    global_data = _load_global_json()
    update_info = global_data.setdefault('update_info', {})
    latest_sha = update_info.get('latest_sha')
    if latest_sha:
        update_info['current_sha'] = latest_sha
    update_info['update_available'] = False
    dbg(f"[MARK_DONE] mark_update_completed: updating update_info to current_sha={update_info.get('current_sha')} latest_sha={update_info.get('latest_sha')} update_available={update_info.get('update_available')}")
    _save_global_json(global_data)
    dbg("[MARK_DONE] mark_update_completed: _save_global_json() returned (see debug.log for details)")


def is_update_available():
    global_data = _load_global_json()
    return global_data.get('update_info', {}).get('update_available', False)


def get_base_dir():
    # Return the directory containing jj-dlp.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def perform_update():
    dbg("[PERFORM] perform_update: starting update")
    print("\n--- jj-dlp Updater ---")
    
    branch = _get_update_branch()
    repo_zip_url = _repo_zip_url(branch)
    dbg(f"[PERFORM] perform_update: branch={branch} url={repo_zip_url}")

    temp_dir = tempfile.mkdtemp(prefix="jj_dlp_update_")
    print(f"Temporary files will be saved to: {temp_dir}")
    
    print(f"Downloading latest version from GitHub (branch: {branch})...")
    zip_path = os.path.join(temp_dir, "main.zip")
    try:
        req = urllib.request.Request(repo_zip_url, headers={'User-Agent': 'jj-dlp-updater'})
        with urllib.request.urlopen(req, timeout=30) as response:
            with open(zip_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
        dbg(f"[PERFORM] perform_update: downloaded zip to {zip_path}")
    except Exception as e:
        dbg("[PERFORM] perform_update: download failed", e)
        print(f"Error downloading update: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    print("Extracting files...")
    extract_dir = os.path.join(temp_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        dbg(f"[PERFORM] perform_update: extracted zip to {extract_dir}")
    except Exception as e:
        dbg("[PERFORM] perform_update: extraction failed", e)
        print(f"Error extracting update: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    # The zip usually contains a single folder like 'jj-dlp-main'
    extracted_items = os.listdir(extract_dir)
    if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_items[0])):
        source_dir = os.path.join(extract_dir, extracted_items[0])
    else:
        source_dir = extract_dir

    base_dir = get_base_dir()

    new_updater_path = os.path.join(source_dir, "jj_dlp", "updater.py")
    curr_updater_path = os.path.join(base_dir, "jj_dlp", "updater.py")
    stage2_script = new_updater_path if os.path.exists(new_updater_path) else curr_updater_path

    print("Launching stage 2 of the updater...")
    stage2_env = os.environ.copy()
    stage2_env["PYTHONIOENCODING"] = "utf-8"
    dbg(f"[PERFORM] perform_update: launching stage2 script: {stage2_script} source_dir={source_dir} base_dir={base_dir} temp_dir={temp_dir}")
    try:
        # Use the downloaded updater.py (which has the latest logic from the repo).
        # This ensures it can cleanly overwrite the base_dir updater.py without
        # running into the catch-22 of copying the old version back over itself.
        real_pkg_dir = os.path.dirname(os.path.abspath(__file__))
        stage2_env['JJ_DLP_DEBUG_LOG_DIR'] = real_pkg_dir
        stage2_env['JJ_DLP_GLOBAL_JSON_PATH'] = os.path.join(real_pkg_dir, 'global.json')
        stage2_env['JJ_DLP_GLOBAL_DIR'] = real_pkg_dir
        dbg(f"[PERFORM] perform_update: set JJ_DLP_DEBUG_LOG_DIR={stage2_env['JJ_DLP_DEBUG_LOG_DIR']} JJ_DLP_GLOBAL_JSON_PATH={stage2_env['JJ_DLP_GLOBAL_JSON_PATH']}")
    except Exception as e:
        dbg("[PERFORM] perform_update: unexpected error while preparing stage2 updater", e)
    try:
        subprocess.run(
            [sys.executable, stage2_script, "--stage2", source_dir, base_dir, temp_dir],
            check=True,
            env=stage2_env,
        )
        dbg("[PERFORM] perform_update: stage2 subprocess completed")
    except subprocess.CalledProcessError as e:
        dbg("[PERFORM] perform_update: stage2 subprocess failed", e)
        print(f"Update failed during stage 2: {e}")
    except Exception as e:
        dbg("[PERFORM] perform_update: failed launching stage2", e)
        print(f"Error launching stage 2: {e}")
    # temp_dir cleanup should happen in stage2


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
            # Find the key in the new text and replace its value.
            # Use a callable replacement so literal backslashes in old_val
            # are not interpreted as regex replacement escapes.
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


def _stage2(source_dir, base_dir, temp_dir):
    dbg(f"[STAGE2] _stage2: starting stage2 source_dir={source_dir} base_dir={base_dir} temp_dir={temp_dir}")
    try:
        print("Running Stage 2 of update...")
        
        actual_diff_dir = os.path.join(base_dir, "diff")
        
        # Clear diff dir
        if os.path.exists(actual_diff_dir):
            shutil.rmtree(actual_diff_dir, ignore_errors=True)
        os.makedirs(actual_diff_dir, exist_ok=True)
        print(f"Diffs will be saved to: {actual_diff_dir}")
        
        # Check for duplicate configs
        config_files = []
        configs_dir = os.path.join(base_dir, "configs")
        root_configs = [f for f in os.listdir(base_dir) if f.endswith(".conf") and os.path.isfile(os.path.join(base_dir, f))] if os.path.exists(base_dir) else []
        sub_configs = [f for f in os.listdir(configs_dir) if f.endswith(".conf") and os.path.isfile(os.path.join(configs_dir, f))] if os.path.exists(configs_dir) else []
        
        all_config_names = set()
        duplicates = set()
        
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
            
        # Process configs from source_dir
        new_configs = []
        src_configs_dir = os.path.join(source_dir, "configs")
        if os.path.exists(src_configs_dir):
            new_configs.extend([os.path.join(src_configs_dir, f) for f in os.listdir(src_configs_dir) if f.endswith(".conf")])
        if os.path.exists(os.path.join(source_dir, "jj-dlp.conf")):
            new_configs.append(os.path.join(source_dir, "jj-dlp.conf"))
            
        new_config_map = {os.path.basename(p): p for p in new_configs}
        
        for user_cfg in config_files:
            fname = os.path.basename(user_cfg)
            if fname in new_config_map:
                new_cfg_path = new_config_map[fname]
                
                with open(user_cfg, 'r', encoding='utf-8') as f:
                    old_content = f.read()
                    
                with open(new_cfg_path, 'r', encoding='utf-8') as f:
                    new_content = f.read()
                    
                streamers = get_old_config_section(user_cfg, "Streamers")
                blocked = get_old_config_section(user_cfg, "Block")
                
                merged_content = inject_preserved_keys(new_content, user_cfg)
                merged_content = replace_section(merged_content, "Streamers", streamers)
                merged_content = replace_section(merged_content, "Block", blocked)
                
                create_diff(old_content, merged_content, user_cfg, actual_diff_dir)
                
                with open(new_cfg_path, 'w', encoding='utf-8') as f:
                    f.write(merged_content)
                    
        # Now copy all files from source_dir to base_dir.
        # global.json is excluded: it holds runtime state (update SHAs, last-live
        # timestamps, etc.) that must never be overwritten by a repo copy.
        print("Installing new files...")
        
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
                    return
                if os.path.exists(dst):
                    with open(dst, 'r', encoding='utf-8', errors='ignore') as f:
                        old_content = f.read()
                    with open(src, 'r', encoding='utf-8', errors='ignore') as f:
                        new_content = f.read()
                    if old_content != new_content and not dst.endswith(".conf"):
                        create_diff(old_content, new_content, dst, actual_diff_dir)
                        if os.path.basename(dst) == "updater.py":
                            dbg(f"[STAGE2] _stage2.copy_and_diff: updater.py changed, copying {src} -> {dst}")
                else:
                    if os.path.basename(dst) == "updater.py":
                        dbg(f"[STAGE2] _stage2.copy_and_diff: copying new updater.py {src} -> {dst}")
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

        dbg("[STAGE2] _stage2: files copied from source to base")

        # Ensure bin/ scripts are executable on Linux/macOS
        print("Setting executable permissions on bin/ files...")
        _mark_bin_executable(base_dir)
        
        mark_update_completed()
        dbg("[STAGE2] _stage2: marked update completed")
        
        print("\n" + "="*60)
        print("✅ Update completed successfully!")
        print(f"   Diff files are available in the 'diff' directory.")
        print("="*60)

    except UpdateError as e:
        dbg(f"[STAGE2] _stage2: clean abort: {e}")
    except Exception as e:
        dbg("[STAGE2] _stage2: exception during stage2", e)
        print(f"Error during stage 2: {e}")
        import traceback
        traceback.print_exc()
        input("\nPress Enter to exit...")
    finally:
        print(f"Cleaning up temporary directory: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        input("\nPress Enter to exit...")


if __name__ == "__main__":
    if len(sys.argv) > 4 and sys.argv[1] == "--stage2":
        _stage2(sys.argv[2], sys.argv[3], sys.argv[4])
