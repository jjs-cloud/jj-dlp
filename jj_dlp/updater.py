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
# NOTE: load_config is still imported for external callers; _load/_save_global_json
# are intentionally NOT imported from main — see the local helpers below.
try:
    from . import logger
    from .main import load_config
except ImportError:
    from jj_dlp import logger
    from jj_dlp.main import load_config

REPO_ZIP_URL = "https://github.com/jjs-cloud/jj-dlp/archive/refs/heads/testing.zip"
API_COMMITS_URL = "https://api.github.com/repos/jjs-cloud/jj-dlp/commits/testing"

PRESERVED_SECTIONS = ["Streamers", "Block"]
PRESERVED_KEYS = [
    "SITE_LABEL", "SITE_ORDER", "PANEL_RESIZE", "SPLIT_AFTER", "OUTPUT_DIR", 
    "OUTPUT_TMPL", "LAST_LIVE_HIGHLIGHT", "DISK_DRIVES", "POPUP_NOTIFICATIONS", 
    "POPUP_TIMEOUT", "POPUP_COOLDOWN", "PROGRESS_BAR_MAX_HOURS", "PROGRESS_BAR_WIDTH"
]

# ── Local global.json helpers ─────────────────────────────────────────────────
# These deliberately do NOT delegate to main._load_global_json / _save_global_json.
# main.py sets its _GLOBAL_JSON_PATH at module-import time.  When updater.py runs
# as a standalone stage-2 script, importing main.py executes module-level code
# (ffmpeg checks, curses init, etc.) that can raise exceptions and abort the
# import — leaving mark_update_completed() unreachable and global.json stale.
# Using __file__ here keeps path resolution self-contained and always correct.

def _global_json_path() -> str:
    """Return the absolute path to global.json.

    Behavior:
    - If the environment variable `JJ_DLP_GLOBAL_JSON_PATH` is set, return that
      value (useful for telling a stage-2 updater where the real global.json
      lives when the script is being executed from a temporary folder).
    - If `JJ_DLP_GLOBAL_DIR` is set, return `<dir>/global.json`.
    - Otherwise, return the path anchored to this file's package dir.
    """
    # Highest priority: explicit full path
    env_path = os.environ.get('JJ_DLP_GLOBAL_JSON_PATH')
    if env_path:
        return env_path
    # Directory override
    env_dir = os.environ.get('JJ_DLP_GLOBAL_DIR')
    if env_dir:
        return os.path.join(env_dir, 'global.json')
    # Default: package-local global.json
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "global.json")


def dbg(msg: str, exc: Exception | None = None) -> None:
    """Append a timestamped debug message to debug.log next to this package.

    Designed to be safe to call when updater.py is executed as a standalone
    stage-2 script.  Dumps optional exception tracebacks and keeps a simple
    append-only log for post-mortem inspection.
    """
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
        ts = datetime.datetime.utcnow().isoformat() + "Z"
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"[{ts}] {msg}\n")
            if exc is not None:
                lf.write("""Exception traceback:\n""")
                lf.write(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
                lf.write("\n")
    except Exception:
        # Never raise from the debugger helper
        pass


def _load_global_json() -> dict:
    path = _global_json_path()
    dbg(f"_load_global_json: attempting to read global.json from: {path}")
    try:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read()
                dbg(f"_load_global_json: on-disk contents before load:\n{raw}")
            except Exception as e:
                dbg(f"_load_global_json: failed to read raw contents", e)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        dbg(f"_load_global_json: parsed data: {repr(data)}")
        if isinstance(data, dict):
            return data
    except Exception as e:
        dbg("_load_global_json: exception while loading JSON", e)
    return {}


def _save_global_json(data: dict) -> None:
    path = _global_json_path()
    dbg(f"_save_global_json: attempting to save global.json to: {path}")
    try:
        # Dump targeted fields that are commonly relevant for update troubleshooting
        try:
            update_info = data.get('update_info', {}) if isinstance(data, dict) else None
            latest_sha = update_info.get('latest_sha') if update_info else None
            current_sha = update_info.get('current_sha') if update_info else None
            update_available = update_info.get('update_available') if update_info else None
            dbg(f"_save_global_json: writing update_info latest_sha={latest_sha} current_sha={current_sha} update_available={update_available}")
        except Exception as e:
            dbg("_save_global_json: failed to introspect update_info", e)

        # Capture before-state
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    before = f.read()
                dbg(f"_save_global_json: on-disk BEFORE write:\n{before}")
            except Exception as e:
                dbg("_save_global_json: failed to read before-state", e)

        # Write the file
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            dbg("_save_global_json: write completed without exception")
        except Exception as e:
            dbg("_save_global_json: exception during write", e)
            return

        # Verify after-state
        try:
            with open(path, "r", encoding="utf-8") as f:
                after = f.read()
            dbg(f"_save_global_json: on-disk AFTER write:\n{after}")
            # Try to parse and compare to detect whether the save was successful
            try:
                after_obj = json.loads(after)
                equal = after_obj == data
                dbg(f"_save_global_json: verification compare result: {equal}")
            except Exception as e:
                dbg("_save_global_json: failed to parse after-state JSON for verification", e)
        except Exception as e:
            dbg("_save_global_json: failed to read after-state", e)
    except Exception as e:
        dbg("_save_global_json: unexpected exception", e)

# ─────────────────────────────────────────────────────────────────────────────

def check_for_updates_background():
    """Checks for updates in the background and saves the status to global.json."""
    try:
        req = urllib.request.Request(API_COMMITS_URL, headers={'User-Agent': 'jj-dlp-updater'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            latest_sha = data['sha']
            dbg(f"check_for_updates_background: fetched latest_sha={latest_sha}")

            global_data = _load_global_json()
            current_sha = global_data.get('update_info', {}).get('current_sha')
            dbg(f"check_for_updates_background: current_sha from disk={current_sha}")

            update_info = global_data.setdefault('update_info', {})
            if current_sha:
                update_info['update_available'] = current_sha != latest_sha
            else:
                # First-run baseline: record the current latest SHA so the next check
                # can correctly detect a newer upstream commit.
                update_info['current_sha'] = latest_sha
                update_info['update_available'] = False

            update_info['latest_sha'] = latest_sha
            dbg(f"check_for_updates_background: prepared update_info latest={update_info.get('latest_sha')} current={update_info.get('current_sha')} update_available={update_info.get('update_available')}")
            _save_global_json(global_data)
            dbg("check_for_updates_background: _save_global_json() returned (see debug.log for details)")
    except Exception:
        pass  # Silently fail in background


def mark_update_completed():
    global_data = _load_global_json()
    update_info = global_data.setdefault('update_info', {})
    latest_sha = update_info.get('latest_sha')
    if latest_sha:
        update_info['current_sha'] = latest_sha
    update_info['update_available'] = False
    dbg(f"mark_update_completed: updating update_info to current_sha={update_info.get('current_sha')} latest_sha={update_info.get('latest_sha')} update_available={update_info.get('update_available')}")
    _save_global_json(global_data)
    dbg("mark_update_completed: _save_global_json() returned (see debug.log for details)")


def is_update_available():
    global_data = _load_global_json()
    return global_data.get('update_info', {}).get('update_available', False)


def get_base_dir():
    # Return the directory containing jj-dlp.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def perform_update():
    print("\n--- jj-dlp Updater ---")
    
    temp_dir = tempfile.mkdtemp(prefix="jj_dlp_update_")
    print(f"Temporary files will be saved to: {temp_dir}")
    
    print("Downloading latest version from GitHub...")
    zip_path = os.path.join(temp_dir, "testing.zip")
    try:
        urllib.request.urlretrieve(REPO_ZIP_URL, zip_path)
    except Exception as e:
        print(f"Error downloading update: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    print("Extracting files...")
    extract_dir = os.path.join(temp_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
    except Exception as e:
        print(f"Error extracting update: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    # The zip usually contains a single folder like 'jj-dlp-testing'
    extracted_items = os.listdir(extract_dir)
    if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_items[0])):
        source_dir = os.path.join(extract_dir, extracted_items[0])
    else:
        source_dir = extract_dir

    base_dir = get_base_dir()

    # Run stage 2 from the *downloaded* copy of updater.py, not the live one.
    # This avoids any file-locking issues on Windows (you can't overwrite a
    # .py file that Python currently has open), and ensures stage 2 uses the
    # latest updater logic from the release being installed.  The old
    # updater.py will be cleanly overwritten by copy_and_diff inside stage 2.
    new_updater_path = os.path.join(source_dir, "jj_dlp", "updater.py")
    curr_updater_path = os.path.join(base_dir, "jj_dlp", "updater.py")
    stage2_script = new_updater_path if os.path.exists(new_updater_path) else curr_updater_path

    print("Launching stage 2 of the updater...")
    # Pass PYTHONIOENCODING=utf-8 so that Unicode characters printed by
    # main.py at import time (e.g. the ✔ in plain_ffmpeg_check) don't cause a
    # UnicodeEncodeError when the subprocess inherits a cp1252 console.
    stage2_env = os.environ.copy()
    stage2_env["PYTHONIOENCODING"] = "utf-8"
    dbg(f"perform_update: launching stage2 script: {stage2_script} with source_dir={source_dir} base_dir={base_dir} temp_dir={temp_dir}")
    # If the downloaded copy of updater.py exists, overwrite it with our
    # instrumented copy so that stage 2 (which runs the downloaded script)
    # will emit the same debug logs. Also set JJ_DLP_DEBUG_LOG_DIR so the
    # copied script writes logs into the live package directory instead of
    # leaving them in a temporary folder that gets removed.
    try:
        if os.path.exists(new_updater_path):
            try:
                shutil.copy2(curr_updater_path, new_updater_path)
                dbg(f"perform_update: copied instrumented updater to {new_updater_path}")
            except Exception as e:
                dbg(f"perform_update: failed to copy instrumented updater to {new_updater_path}", e)
        # Ensure stage2 writes debug.log into the real package dir
        real_pkg_dir = os.path.dirname(os.path.abspath(__file__))
        stage2_env['JJ_DLP_DEBUG_LOG_DIR'] = real_pkg_dir
        # Tell stage2 explicitly where the real global.json lives so it updates
        # the live package state instead of a temp copy.
        stage2_env['JJ_DLP_GLOBAL_JSON_PATH'] = os.path.join(real_pkg_dir, 'global.json')
        stage2_env['JJ_DLP_GLOBAL_DIR'] = real_pkg_dir
        dbg(f"perform_update: set JJ_DLP_DEBUG_LOG_DIR={stage2_env['JJ_DLP_DEBUG_LOG_DIR']} and JJ_DLP_GLOBAL_JSON_PATH={stage2_env['JJ_DLP_GLOBAL_JSON_PATH']}")
    except Exception as e:
        dbg("perform_update: unexpected error while preparing stage2 updater", e)
    try:
        subprocess.run(
            [sys.executable, stage2_script, "--stage2", source_dir, base_dir, temp_dir],
            check=True,
            env=stage2_env,
        )
    except subprocess.CalledProcessError as e:
        print(f"Update failed during stage 2: {e}")
    except Exception as e:
        print(f"Error launching stage 2: {e}")
        
    # temp_dir is cleaned up in stage 2 or we can clean it up here if stage 2 failed


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
                os.makedirs(dst, exist_ok=True)
                for item in os.listdir(src):
                    copy_and_diff(os.path.join(src, item), os.path.join(dst, item))
            else:
                # Never overwrite global.json — it contains live runtime state.
                if os.path.basename(dst) == "global.json":
                    return
                if os.path.exists(dst):
                    with open(dst, 'r', encoding='utf-8', errors='ignore') as f:
                        old_content = f.read()
                    with open(src, 'r', encoding='utf-8', errors='ignore') as f:
                        new_content = f.read()
                    if old_content != new_content and not dst.endswith(".conf"):
                        create_diff(old_content, new_content, dst, actual_diff_dir)
                shutil.copy2(src, dst)
                
        copy_and_diff(source_dir, base_dir)

        # Ensure bin/ scripts are executable on Linux/macOS
        print("Setting executable permissions on bin/ files...")
        _mark_bin_executable(base_dir)
        
        mark_update_completed()
        print("Update completed successfully! Diff files are available in the 'diff' directory.")
        
    except Exception as e:
        print(f"Error during stage 2: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print(f"Cleaning up temporary directory: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 4 and sys.argv[1] == "--stage2":
        _stage2(sys.argv[2], sys.argv[3], sys.argv[4])
