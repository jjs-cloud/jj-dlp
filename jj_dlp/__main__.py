"""CLI entry point: argument parsing, startup wiring, and clean shutdown."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import List, Tuple

from jj_dlp import __version__
from jj_dlp.core.config import app as app_config
from jj_dlp.core.config import schema, sites as sites_config, storage
from jj_dlp.core.deps import curses_dep, ffmpeg_dep, registry  # noqa: F401  registers dependencies
from jj_dlp.core.deps.ytdlp_bin import app_root
from jj_dlp.core.engine.app_state import AppState, SingleInstanceError
from jj_dlp.core.engine.disk import DiskSampler, SiteDiskSource
from jj_dlp.core.engine.lifecycle import Lifecycle
from jj_dlp.core.engine.site_engine import SiteEngine
from jj_dlp.core.engine.site_state import SiteState
from jj_dlp.core.notify import crash as notify_crash
from jj_dlp.core.notify import log_buffer
from jj_dlp.core.plugins import get_plugin
from jj_dlp.core.theme import presets as theme_presets
from jj_dlp.core.updater import check as updater_check
from jj_dlp.core.updater import install as updater_install
from jj_dlp.core.updater import merge as updater_merge
from jj_dlp.core.web import start_web_server, stop_web_server


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
    """Return the default data directory: userdata/ next to the installed app root."""
    return app_root() / "userdata"


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser."""
    parser = argparse.ArgumentParser(prog="jj-dlp", description="Multi-site stream recorder built on yt-dlp.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir(),
        help="Directory holding config/, schema/, state/, logs/, recordings/ (default: userdata/ next to the app).",
    )
    parser.add_argument("--debug", action="store_true", help="Show debug-level log lines in the Log tab for this run.")
    parser.add_argument("--version", action="version", version=f"jj-dlp {__version__}")
    parser.add_argument(
        "--finish-update",
        metavar="PATH",
        default=None,
        help="Internal: complete an in-progress update from the extracted release at PATH.",
    )
    return parser


def bootstrap_data_dir(data_dir: Path) -> Path:
    """Ensure required subdirectories and first-run schema/app.json files exist; return schema_path."""
    data_dir = Path(data_dir)
    for sub in ("config/sites", "config/.backups", "schema", "state", "logs"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    schema_path = data_dir / "schema" / "fields.json"
    if not schema_path.exists():
        storage.save_config(schema_path, schema.load_shipped_defaults())
    schema.load(schema_path)

    app_path = data_dir / "config" / "app.json"
    if not app_path.exists():
        app_config.save(data_dir, app_config.get_default(schema_path))

    # config/theme.json's own fallback is a single minimal theme for corrupt-file
    # recovery only; a fresh install needs all three shipped presets (Doc 1 §10.5).
    theme_path = data_dir / "config" / "theme.json"
    if not theme_path.exists():
        presets = list(theme_presets.get_presets().values())
        storage.save_config(theme_path, {"schema_version": 1, "active_theme": "dark", "themes": presets})

    return schema_path


def handle_finish_update(extracted_path: Path, data_dir: Path) -> None:
    """Finish an in-progress update: snapshot old schema, install files, refresh schema, merge config."""
    data_dir = Path(data_dir)
    schema_path = data_dir / "schema" / "fields.json"
    snapshot_path = data_dir / "schema" / ".previous_fields.json"
    if schema_path.exists():
        shutil.copy2(schema_path, snapshot_path)

    updater_install.finish_update(extracted_path, data_dir)

    # schema/fields.json is app-owned: always fully replaced from the freshly-installed
    # package, never merged (Doc 1 §3.7), then preserved config values are merged onto
    # the new defaults it now describes (Doc 1 §9.3).
    storage.save_config(schema_path, schema.load_shipped_defaults())
    schema.load(schema_path)
    updater_merge.apply_merge(data_dir, schema_path)


def _load_sites(
    data_dir: Path, schema_path: Path, labels: List[str], app_state: AppState
) -> List[Tuple[str, dict, SiteState]]:
    """Load and register each selected site's config and runtime SiteState."""
    loaded = []
    for label in labels:
        config = sites_config.load_site(data_dir, label, schema_path)
        site_state = SiteState(data_dir, label)
        app_state.register_site_state(label, site_state)
        loaded.append((label, config, site_state))
    return loaded


def _start_site_engines(
    data_dir: Path, app_state: AppState, app_cfg, sites: List[Tuple[str, dict, SiteState]], schema_path: Path
) -> List[SiteEngine]:
    """Build, register, and start a SiteEngine for every loaded site."""
    engines = []
    for label, config, site_state in sites:
        plugin = get_plugin(config.get("plugin", "chatsite"))
        engine = SiteEngine(data_dir, app_state, app_cfg, label, config, site_state, plugin, schema_path)
        app_state.register_checker(label, engine.checker)
        engine.start()
        engines.append(engine)
        app_state.start_site_services(engine.site_handle, plugin)
    return engines


def run_app(data_dir: Path, debug: bool) -> None:
    """Bootstrap config, start the engine for every selected site, then run the curses frontend."""
    schema_path = bootstrap_data_dir(data_dir)
    notify_crash.configure(data_dir)

    app_state = AppState(data_dir)
    try:
        app_state.acquire_single_instance_lock()
    except SingleInstanceError as exc:
        print(str(exc))
        sys.exit(1)

    lifecycle = Lifecycle(app_state)
    lifecycle.install()
    lifecycle.start_watchdog()

    try:
        check_dependencies()
        # Imported only after dependency checks (e.g. windows-curses) can have installed curses.
        from jj_dlp.frontends.curses import app as curses_app
        from jj_dlp.frontends.curses import picker

        app_cfg = app_config.load(data_dir, schema_path)
        log_buffer.configure(data_dir, app_cfg, debug=debug)

        updater_check.start_background(data_dir, app_cfg, stop_event=lifecycle.shutdown_event)
        web_server = start_web_server(app_state)

        selected_labels = picker.main(data_dir)
        sites = _load_sites(data_dir, schema_path, selected_labels, app_state)
        app_state.set_loaded_sites([label for label, _cfg, _state in sites])
        engines = _start_site_engines(data_dir, app_state, app_cfg, sites, schema_path)

        disk_sources = [
            SiteDiskSource(label, cfg.get("output", {}).get("dir", "recordings"), state, cfg.get("streamers", []))
            for label, cfg, state in sites
        ]
        disk_sampler = DiskSampler(data_dir, app_cfg, disk_sources)
        disk_stop = threading.Event()
        disk_thread = threading.Thread(target=disk_sampler.run_loop, args=(disk_stop,), daemon=True)
        disk_thread.start()

        curses_app.main(
            data_dir, app_state=app_state, sites=sites, disk_sampler=disk_sampler,
            shutdown_event=lifecycle.shutdown_event,
        )

        disk_stop.set()
        for engine in engines:
            engine.stop()
        app_state.stop_all_site_services()
        stop_web_server(web_server)
    finally:
        lifecycle.mark_shutdown_complete()
        app_state.release_single_instance_lock()


def main(argv: List[str] | None = None) -> None:
    """Parse CLI args and start the app."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.finish_update:
        handle_finish_update(Path(args.finish_update), args.data_dir)
        clean_argv = [a for a in (argv or sys.argv[1:]) if a != args.finish_update]
        clean_argv = [a for a in clean_argv if a != updater_install.FINISH_UPDATE_FLAG]
        os.execv(sys.executable, [sys.executable, "-m", "jj_dlp", *clean_argv])
        return  # unreachable; execv replaces this process

    run_app(args.data_dir, args.debug)


if __name__ == "__main__":
    main()
