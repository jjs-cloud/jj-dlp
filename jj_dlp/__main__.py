"""CLI entry point: argument parsing and startup dispatch."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from jj_dlp import __version__
from jj_dlp.core.deps import curses_dep, ffmpeg_dep, registry  # noqa: F401
from jj_dlp.core.updater import install as updater_install


def check_dependencies() -> None:
    """Check every registered dependency, offering to install anything missing."""
    for dep in registry.get_all():
        ok, message = dep.check()
        if ok:
            continue
        print(f"[{dep.name}] {message}")
        answer = input(f"Install {dep.name} now? [y/N] ").strip().lower()
        if answer != "y":
            continue
        ok, message = dep.install(progress_cb=print)
        print(f"[{dep.name}] {message}")


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

    if args.finish_update:
        updater_install.finish_update(Path(args.finish_update), args.data_dir)
        clean_argv = [a for a in (argv or sys.argv[1:]) if a != args.finish_update]
        clean_argv = [a for a in clean_argv if a != updater_install.FINISH_UPDATE_FLAG]
        os.execv(sys.executable, [sys.executable, "-m", "jj_dlp", *clean_argv])
        return  # unreachable; execv replaces this process

    check_dependencies()
    print("jj-dlp starting...")


if __name__ == "__main__":
    main()
