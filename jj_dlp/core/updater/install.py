"""Download, extract, and relaunch install (Doc 1 §9.2)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from jj_dlp.core.config import state
from jj_dlp.core.deps import ytdlp_bin
from jj_dlp.core.updater.check import GITHUB_REPO, REQUEST_TIMEOUT_SEC

log = logging.getLogger("jj_dlp.updater.install")

FINISH_UPDATE_FLAG = "--finish-update"
RELEASE_BY_TAG_URL = "https://api.github.com/repos/{repo}/releases/tags/{tag}"


def _fetch_release_by_tag(repo: str, tag: str) -> Dict[str, Any]:
    """Fetch a single release's metadata by its tag name."""
    url = RELEASE_BY_TAG_URL.format(repo=repo, tag=tag)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_zip_asset_url(release: Dict[str, Any]) -> str:
    """Return the release's zip asset URL, falling back to the source zipball."""
    for asset in release.get("assets", []):
        if asset.get("name", "").endswith(".zip"):
            return asset["browser_download_url"]
    zipball_url = release.get("zipball_url")
    if not zipball_url:
        raise RuntimeError(f"release {release.get('tag_name')} has no zip asset or zipball")
    return zipball_url


def download_release(tag: str, work_dir: Path) -> Path:
    """Download a release's zip (by tag) into work_dir; return the zip file path."""
    release = _fetch_release_by_tag(GITHUB_REPO, tag)
    url = _find_zip_asset_url(release)
    work_dir.mkdir(parents=True, exist_ok=True)
    zip_path = work_dir / "release.zip"
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp, open(zip_path, "wb") as f:
        shutil.copyfileobj(resp, f)
    return zip_path


def extract_release(zip_path: Path, extract_dir: Path) -> Path:
    """Extract a downloaded release zip into extract_dir; return that directory."""
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def download_and_extract(tag: str) -> Path:
    """Download and extract a release by tag to a fresh temp directory; return the extracted path."""
    work_dir = Path(tempfile.mkdtemp(prefix="jj-dlp-update-"))
    zip_path = download_release(tag, work_dir)
    return extract_release(zip_path, work_dir / "extracted")


def relaunch_for_update(extracted_dir: Path, extra_argv: Optional[List[str]] = None) -> None:
    """Spawn a fresh process with --finish-update <extracted_dir>, then exit this one."""
    argv = [sys.executable, "-m", "jj_dlp"] + (extra_argv or []) + [FINISH_UPDATE_FLAG, str(extracted_dir)]
    log.info("relaunching to finish update: %s", argv)
    subprocess.Popen(argv, close_fds=True, start_new_session=True)
    sys.exit(0)


def _install_dir() -> Path:
    """Return the directory containing the currently installed jj_dlp package."""
    import jj_dlp

    return Path(jj_dlp.__file__).resolve().parent


def _find_package_root(extracted_dir: Path) -> Path:
    """Locate the jj_dlp package directory inside an extracted release tree."""
    direct = extracted_dir / "jj_dlp"
    if direct.is_dir():
        return direct
    matches = list(extracted_dir.glob("*/jj_dlp"))
    if matches:
        return matches[0]
    raise RuntimeError(f"could not find a jj_dlp package under {extracted_dir}")


def _find_bin_dir(extracted_dir: Path) -> Optional[Path]:
    """Locate the bundled bin/ directory alongside jj_dlp in an extracted release tree, if present."""
    candidate = _find_package_root(extracted_dir).parent / "bin"
    return candidate if candidate.is_dir() else None


def _copy_tree_overwrite(source: Path, dest: Path) -> None:
    """Recursively copy source's contents onto dest, overwriting any existing files."""
    for item in source.rglob("*"):
        target = dest / item.relative_to(source)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def finish_update(extracted_dir: Path, data_dir: Path) -> None:
    """Copy extracted release files over the install directory and record the new installed_sha."""
    extracted_dir = Path(extracted_dir)
    data_dir = Path(data_dir)
    source_root = _find_package_root(extracted_dir)
    _copy_tree_overwrite(source_root, _install_dir())

    bin_source = _find_bin_dir(extracted_dir)
    if bin_source is not None:
        _copy_tree_overwrite(bin_source, ytdlp_bin.app_root() / "bin")

    latest_sha = state.get_update_check(data_dir).get("latest_sha_seen")
    state.set_update_check(data_dir, installed_sha=latest_sha)
    log.info("update finished, installed_sha=%s", latest_sha)

    shutil.rmtree(extracted_dir.parent, ignore_errors=True)


def apply_update(tag: str) -> None:
    """Download, extract, and relaunch to install the given release tag. Never returns."""
    extracted_dir = download_and_extract(tag)
    relaunch_for_update(extracted_dir)
