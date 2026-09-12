"""CLI entry point: argument parsing and startup dispatch."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from jj_dlp import __version__


def default_data_dir() -> Path:
    """Return the platform-appropriate default data directory."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", str(Path.home()))
        return Path(base) / "jj-dlp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "jj-dlp"
    base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(base) / "jj-dlp"


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(prog="jj-dlp", description="Multi-site stream recorder built on yt-dlp.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir(),
        help="Directory holding config/, schema/, state/, logs/, recordings/ (default: platform data dir).",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging for this run.")
    parser.add_argument("--version", action="version", version=f"jj-dlp {__version__}")
    parser.add_argument(
        "--finish-update",
        metavar="PATH",
        default=None,
        help="Internal: complete an in-progress update from the extracted release at PATH.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse CLI args and start the app."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # TODO: --finish-update is accepted but not yet handled (Doc 1 §9.2, Phase 5).
    print("jj-dlp starting...")


if __name__ == "__main__":
    main()
