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
import subprocess

_REPO_BASE = "https://github.com/jjs-cloud/jj-dlp"
_API_BASE   = "https://api.github.com/repos/jjs-cloud/jj-dlp"

# ── Updater version ───────────────────────────────────────────────────────────
# Incremented independently of the main jj-dlp version so we can tell which
# updater logic is actually running during an update.
UPDATER_VERSION = "2.3.6"

# ── Lazy package imports ──────────────────────────────────────────────────────
# Relative imports are deferred to call time so this file is also safe to
# execute as a standalone script (the --stage2 subprocess path, now the
# canonical merge/install mechanism — see the __main__ block below). When run
# as a script __name__ == "__main__" and relative imports would crash at parse
# time if they were at module scope.

class _NullLogger:
    """No-op stand-in for the real logger module, used when this file is
    executing standalone as __main__ (e.g. inside a --stage2 subprocess),
    where `from . import logger` has no parent package to resolve against.
    Lets shared helpers like _fetch_latest_sha() call _logger().dbg(...)
    safely regardless of which context invoked them, instead of needing a
    bespoke duplicate of every such helper for the standalone case."""
    def dbg(self, *args, **kwargs):
        pass


def _logger():
    try:
        from . import logger as _l
        return _l
    except ImportError:
        return _NullLogger()

def _load_global_json() -> dict:
    from .main import _load_global_json as _f
    return _f()

def _save_global_json(data: dict) -> None:
    from .main import _save_global_json as _f
    _f(data)

def _get_preserved_keys(source_dir=None) -> list:
    """Return the list of preserved key names, derived from CONFIG_KEYS.

    Uses the same source_dir-aware loading as _load_config_keys, for the same
    reason: during an update this runs before the new files are copied over
    the old install, so importing the installed package would silently
    return stale data (see _load_config_keys docstring).
    """
    _ck = _load_config_keys(source_dir)
    if not _ck:
        return []
    return [k.name for k in _ck if k.preserve]


class UpdateError(Exception):
    """Custom exception raised during updating."""
    pass


def _get_update_branch() -> str:
    """Return the configured update branch (falls back to 'main' if unset)."""
    try:
        from .main import load_global_config
        branch = load_global_config().get("update_branch", "main")
    except Exception:
        branch = "main"
    return branch or "main"


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
            # ← takes this branch on fresh clone
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
        print(f"\nError downloading update: {e}")
        print("Could not reach GitHub. Check your internet connection (e.g. make sure Wi-Fi is on) and try again.")
        shutil.rmtree(temp_dir, ignore_errors=True)
        input("\nPress Enter to exit...")
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
        print(f"\nError extracting update: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        input("\nPress Enter to exit...")
        return

    # The zip usually contains a single folder like 'jj-dlp-main'
    extracted_items = os.listdir(extract_dir)
    if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_items[0])):
        source_dir = os.path.join(extract_dir, extracted_items[0])
    else:
        source_dir = extract_dir
    _logger().dbg(f"[UPDATER] perform_update: source_dir resolved to {source_dir}")

    try:
        # ── Step 3: Delegate merge + install to a fresh subprocess ────────────
        # The download/extract above always uses whatever code is currently
        # running (this in-memory copy of updater.py), which may be stale.
        # From here on, everything that touches the user's configs and files
        # is handed off to the freshly-extracted updater.py, run as a new
        # subprocess (--stage2), so that any fix shipped in *this* update
        # takes effect on this very cycle instead of lagging by one.
        stage2_script = os.path.join(source_dir, "jj_dlp", "updater.py")
        if not os.path.isfile(stage2_script):
            raise UpdateError(f"Downloaded update is missing updater.py at {stage2_script}")

        stage2_cmd = [sys.executable, stage2_script, "--stage2", source_dir, base_dir, temp_dir, branch]
        _logger().dbg(f"[UPDATER] perform_update: delegating to stage2 subprocess: {stage2_cmd}")
        print("Installing update (handing off to freshly downloaded updater)...")

        result = subprocess.run(stage2_cmd)
        _logger().dbg(f"[UPDATER] perform_update: stage2 subprocess exited with code {result.returncode}")
        if result.returncode != 0:
            raise UpdateError(f"stage2 update process exited with code {result.returncode}")

        _logger().dbg("[UPDATER] perform_update: stage2 completed successfully")
        # stage2 owns its own success/failure messaging, config-merge diffing,
        # marking global.json complete, and its own "Press Enter to exit"
        # prompt (inherited stdio), so nothing further is needed here.

    except UpdateError as e:
        _logger().dbg(f"[UPDATER] perform_update: clean abort: {e}")
    except Exception as e:
        _logger().dbg(f"[UPDATER] perform_update: exception during update: {e}")
        print(f"Error during update: {e}")
        traceback.print_exc()
    finally:
        # stage2 cleans up temp_dir itself on the normal path; this is a
        # defensive no-op in that case and a real cleanup if stage2 never
        # got to run (e.g. missing updater.py, failed subprocess spawn).
        print(f"Cleaning up temporary directory: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        _logger().dbg(f"[UPDATER] perform_update: temp_dir cleanup pass complete")


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


def inject_preserved_keys(new_text, old_config_path, source_dir=None):
    parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)
    try:
        parser.read(old_config_path, encoding='utf-8')
    except Exception:
        return new_text

    for key in _get_preserved_keys(source_dir):
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


def _load_config_keys(source_dir=None):
    """Return the CONFIG_KEYS tuple.

    All merge/comment logic that needs CONFIG_KEYS now runs inside the
    stage2 subprocess, which always executes this very file (updater.py)
    directly out of the freshly-extracted source_dir as __main__. In that
    context, `__file__` already points at the fresh copy on disk, so the
    plain absolute-import fallback below resolves `jj_dlp.config_editor`
    against the fresh package automatically — no synthetic sys.modules
    package or spec_from_file_location trick is needed to dodge stale,
    already-imported CONFIG_KEYS.

    `source_dir` is kept as a parameter for call-site compatibility but is
    no longer used directly; it's implied by where this file lives when
    running as __main__.
    """
    try:
        from .config_editor import CONFIG_KEYS as _ck
        return _ck
    except ImportError:
        try:
            _pkg_dir = os.path.dirname(os.path.abspath(__file__))
            _proj_root = os.path.dirname(_pkg_dir)
            if _proj_root not in sys.path:
                sys.path.insert(0, _proj_root)
            from jj_dlp.config_editor import CONFIG_KEYS as _ck
            return _ck
        except Exception:
            return None


def update_config_comments(text, source_dir=None):
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

    `source_dir` should be the freshly-extracted update directory, when
    available, so the comment text reflects the version being installed
    rather than the version currently running (see _load_config_keys).
    """
    _ck = _load_config_keys(source_dir)
    if not _ck:
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


# ── Standalone stage-2 entry point (the canonical merge/install path) ─────────
#
# updater.py always downloads and extracts itself, then spawns a fresh
# subprocess running THIS file, straight out of the just-extracted
# source_dir, as:
#
#   python <source_dir>/jj_dlp/updater.py --stage2 <source_dir> <base_dir> <temp_dir> [branch]
#
# Doing the merge/copy/install work in a fresh subprocess (rather than
# in-process, using whatever updater.py happened to already be imported)
# means a fix shipped in updater.py takes effect the same cycle it's
# downloaded, instead of lagging by one cycle.
#
# Two callers land here:
#   - v3+ perform_update(), which always delegates here (see perform_update).
#   - legacy pre-v3 installed updater.py, whose in-process perform_update()
#     still calls this same convention directly (the original compat path).
# Both invoke it identically, so there's nothing legacy-specific left to
# maintain here.
#
# Because this file is executed as a plain script (not as a package member),
# relative imports at module scope aren't in play here; this block performs
# the copy/install work using only stdlib + the helper functions defined
# above (already in module scope by the time __main__ runs). CONFIG_KEYS
# lookups via _load_config_keys() DO resolve correctly against the fresh
# on-disk package, since __file__ here already points into source_dir.
#
if __name__ == "__main__":
    import errno as _errno

    if len(sys.argv) in (5, 6) and sys.argv[1] == "--stage2":
        _source_dir = sys.argv[2]
        _base_dir   = sys.argv[3]
        _temp_dir   = sys.argv[4]
        _branch     = sys.argv[5] if len(sys.argv) == 6 else None

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
            # Parity with mark_update_completed()/perform_update(): re-fetch
            # the branch HEAD SHA now, post-install, so that additional
            # commits landing on the branch mid-update don't leave
            # current_sha stale and cause a spurious "Update Available" on
            # the next launch. Falls back to whatever latest_sha was already
            # recorded if the branch is unknown or the re-fetch fails.
            _post_sha = None
            if _branch:
                _post_sha = _fetch_latest_sha(_branch)
                _sdbg(f"mark_done: post-install SHA fetch for branch={_branch}: {_post_sha}")

            _gd = _load_json()
            _ui = _gd.setdefault("update_info", {})
            _ls = _post_sha or _ui.get("latest_sha")
            if _ls:
                _ui["current_sha"] = _ls
                _ui["latest_sha"] = _ls
            _ui["update_available"] = False
            _sdbg(f"mark_done: current_sha={_ui.get('current_sha')} latest_sha={_ui.get('latest_sha')} update_available=False")
            _save_json(_gd)

        # ── PRESERVED_KEYS: read from the freshly downloaded config_editor,
        #    falling back to the installed package if unavailable ───────────
        _PKEYS = _get_preserved_keys(_source_dir)

        _sdbg(f"stage2 starting: version={UPDATER_VERSION} source={_source_dir} base={_base_dir} temp={_temp_dir} branch={_branch}")
        print(f"\n--- jj-dlp Updater Stage 2 (v{UPDATER_VERSION}) ---")
        print("Installing update...")

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

                _new_txt = update_config_comments(_new_txt, _source_dir)
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