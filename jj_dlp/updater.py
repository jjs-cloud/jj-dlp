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

# AAdjust imports to work whether run as module or script
try:
    from . import logger
    from .main import load_config, _load_global_json, _save_global_json
except ImportError:
    pass

REPO_ZIP_URL = "https://github.com/jjs-cloud/jj-dlp/archive/refs/heads/experimental.zip"
API_COMMITS_URL = "https://api.github.com/repos/jjs-cloud/jj-dlp/commits/experimental"

PRESERVED_SECTIONS = ["Streamers", "Block"]
PRESERVED_KEYS = [
    "SITE_LABEL", "SITE_ORDER", "PANEL_RESIZE", "SPLIT_AFTER", "OUTPUT_DIR", 
    "OUTPUT_TMPL", "LAST_LIVE_HIGHLIGHT", "DISK_DRIVES", "POPUP_NOTIFICATIONS", 
    "POPUP_TIMEOUT", "POPUP_COOLDOWN", "PROGRESS_BAR_MAX_HOURS", "PROGRESS_BAR_WIDTH"
]

def check_for_updates_background():
    """Checks for updates in the background and saves the status to global.json."""
    try:
        req = urllib.request.Request(API_COMMITS_URL, headers={'User-Agent': 'jj-dlp-updater'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            latest_sha = data['sha']
            
            global_data = _load_global_json()
            current_sha = global_data.get('update_info', {}).get('current_sha')
            
            if current_sha:
                global_data.setdefault('update_info', {})['update_available'] = current_sha != latest_sha
            else:
                # First-run baseline: record the current latest SHA so the next check
                # can correctly detect a newer upstream commit.
                global_data.setdefault('update_info', {})['current_sha'] = latest_sha
                global_data.setdefault('update_info', {})['update_available'] = False
                
            global_data.setdefault('update_info', {})['latest_sha'] = latest_sha
            _save_global_json(global_data)
    except Exception as e:
        pass # Silently fail in background

def mark_update_completed():
    global_data = _load_global_json()
    latest_sha = global_data.get('update_info', {}).get('latest_sha')
    if latest_sha:
        global_data.setdefault('update_info', {})['current_sha'] = latest_sha
    global_data.setdefault('update_info', {})['update_available'] = False
    _save_global_json(global_data)

def is_update_available():
    global_data = _load_global_json()
    return global_data.get('update_info', {}).get('update_available', False)

def get_base_dir():
    # Return the directory containing jj-dlp.py
    if __name__ == "__main__":
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    else:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def perform_update():
    print("\n--- jj-dlp Updater ---")
    
    temp_dir = tempfile.mkdtemp(prefix="jj_dlp_update_")
    print(f"Temporary files will be saved to: {temp_dir}")
    
    ans = input("Do you want to proceed with the update? (y/n): ").strip().lower()
    if ans != 'y':
        print("Update cancelled.")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    print("Downloading latest version from GitHub...")
    zip_path = os.path.join(temp_dir, "experimental.zip")
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

    # The zip usually contains a single folder like 'jj-dlp-experimental'
    extracted_items = os.listdir(extract_dir)
    if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_items[0])):
        source_dir = os.path.join(extract_dir, extracted_items[0])
    else:
        source_dir = extract_dir

    # User requested: "make sure the updater itself gets updated first when the user runs --update"
    # We copy the new updater.py over the current one, then launch it for stage 2.
    base_dir = get_base_dir()
    new_updater_path = os.path.join(source_dir, "jj_dlp", "updater.py")
    curr_updater_path = os.path.join(base_dir, "jj_dlp", "updater.py")
    
    if os.path.exists(new_updater_path):
        print("Updating the updater script first...")
        shutil.copy2(new_updater_path, curr_updater_path)
    
    print("Launching stage 2 of the updater...")
    # Relaunch the (now updated) updater.py as a script to finish installation
    try:
        subprocess.run([sys.executable, curr_updater_path, "--stage2", source_dir, base_dir, temp_dir], check=True)
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
            # Find the key in the new text and replace its value
            pattern = re.compile(rf"^([ \t]*{key}[ \t]*=).*$", re.IGNORECASE | re.MULTILINE)
            if pattern.search(new_text):
                new_text = pattern.sub(rf"\1 {old_val}", new_text)
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
        
        diff_dir = os.path.join(base_dir, "jj-dlp", "diff")
        diff_dir_legacy = os.path.join(base_dir, "diff") # If jj-dlp is the root
        
        # Decide which diff dir to use. If jj-dlp/jj_dlp exists, base_dir is jj-dlp.
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
        # The new zip has a configs/ directory probably, or a default jj-dlp.conf
        new_configs = []
        src_configs_dir = os.path.join(source_dir, "configs")
        if os.path.exists(src_configs_dir):
            new_configs.extend([os.path.join(src_configs_dir, f) for f in os.listdir(src_configs_dir) if f.endswith(".conf")])
        if os.path.exists(os.path.join(source_dir, "jj-dlp.conf")):
            new_configs.append(os.path.join(source_dir, "jj-dlp.conf"))
            
        # For each existing user config, if there's a matching new config, merge it.
        # If there are multiple new configs, we can just map by filename.
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
                
                # Write the merged content back to the new config file in the temp dir,
                # so that the next step (copytree) will install the merged version.
                with open(new_cfg_path, 'w', encoding='utf-8') as f:
                    f.write(merged_content)
                    
        # Now copy all files from source_dir to base_dir
        print("Installing new files...")
        
        def copy_and_diff(src, dst):
            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                for item in os.listdir(src):
                    copy_and_diff(os.path.join(src, item), os.path.join(dst, item))
            else:
                if os.path.exists(dst):
                    with open(dst, 'r', encoding='utf-8', errors='ignore') as f:
                        old_content = f.read()
                    with open(src, 'r', encoding='utf-8', errors='ignore') as f:
                        new_content = f.read()
                    if old_content != new_content and not dst.endswith(".conf"): 
                        # We already diffed configs
                        create_diff(old_content, new_content, dst, actual_diff_dir)
                shutil.copy2(src, dst)
                
        copy_and_diff(source_dir, base_dir)
        
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
