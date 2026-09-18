"""Build a release zip containing jj_dlp/ and bin/, matching core/updater/install.py's layout."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "jj_dlp"
# Per-platform yt-dlp binaries must be placed here (windows/, macos/, linux/)
# before cutting a real release zip - this repo does not ship them (Doc 1 Addendum §A.3).
BIN_DIR = REPO_ROOT / "bin"

# Skip bytecode/cache noise; everything else under jj_dlp/ ships in the release.
_SKIP_SUFFIXES = (".pyc",)
_SKIP_DIRNAMES = ("__pycache__",)


def _iter_package_files():
    """Yield every file under jj_dlp/ that should be included in the release zip."""
    for path in PACKAGE_DIR.rglob("*"):
        if path.is_dir():
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        if any(part in _SKIP_DIRNAMES for part in path.parts):
            continue
        yield path


def _iter_bin_files():
    """Yield every file under bin/ (bundled yt-dlp binaries), if that directory exists."""
    if not BIN_DIR.is_dir():
        return
    for path in BIN_DIR.rglob("*"):
        if path.is_dir():
            continue
        yield path


def build_release(version: str, dist_dir: Path) -> Path:
    """Zip jj_dlp/ and bin/ (rooted at the zip's top level) into dist_dir/jj-dlp-<version>.zip."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    out_path = dist_dir / f"jj-dlp-{version}.zip"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _iter_package_files():
            arcname = path.relative_to(REPO_ROOT)
            zf.write(path, arcname)
        for path in _iter_bin_files():
            arcname = path.relative_to(REPO_ROOT)
            zf.write(path, arcname)
    return out_path


def main() -> None:
    """Read the package version and build dist/jj-dlp-<version>.zip."""
    sys.path.insert(0, str(REPO_ROOT))
    from jj_dlp import __version__

    out_path = build_release(__version__, REPO_ROOT / "dist")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
