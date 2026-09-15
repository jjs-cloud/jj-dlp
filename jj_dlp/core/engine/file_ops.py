"""Move/trim/fixup/split/trash operations on output files."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("jj_dlp.engine.file_ops")

_FFMPEG_BIN = "ffmpeg"


class FileOpError(Exception):
    """Raised when a file operation fails."""


def _unique_path(directory: Path, filename: str) -> Path:
    """Return a path for filename under directory, appending _2, _3, ... on collision."""
    directory = Path(directory)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, ext = os.path.splitext(filename)
    i = 2
    while True:
        candidate = directory / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
        i += 1


def _run_ffmpeg(args: list) -> None:
    """Run ffmpeg with args, raising FileOpError with the stderr tail on failure."""
    cmd = [_FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-10:])
        raise FileOpError(f"ffmpeg failed ({result.returncode}): {tail}")


def open_file(path: str) -> None:
    """Open a file with the OS-native default application."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", path], check=True)
        else:
            subprocess.run(["xdg-open", path], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FileOpError(f"could not open {path}: {exc}") from exc


def open_containing_folder(path: str) -> None:
    """Open the OS-native file browser at the folder containing path."""
    system = platform.system()
    folder = str(Path(path).parent)
    try:
        if system == "Windows":
            subprocess.run(["explorer", "/select,", str(path)], check=False)
        elif system == "Darwin":
            subprocess.run(["open", "-R", str(path)], check=True)
        else:
            subprocess.run(["xdg-open", folder], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FileOpError(f"could not open folder for {path}: {exc}") from exc


def _trash_windows(path: Path) -> bool:
    """Send path to the Windows recycle bin via SHFileOperationW. Returns success."""
    import ctypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = str(path) + "\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))  # type: ignore[attr-defined]
    return result == 0 and not op.fAnyOperationsAborted


def _trash_macos(path: Path) -> bool:
    """Send path to the macOS Trash via Finder AppleScript. Returns success."""
    script = f'tell application "Finder" to delete POSIX file "{path}"'
    result = subprocess.run(["osascript", "-e", script], capture_output=True)
    return result.returncode == 0


def _trash_freedesktop(path: Path) -> bool:
    """Move path into the XDG Trash (~/.local/share/Trash). Returns success."""
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    trash_files = data_home / "Trash" / "files"
    trash_info = data_home / "Trash" / "info"
    try:
        trash_files.mkdir(parents=True, exist_ok=True)
        trash_info.mkdir(parents=True, exist_ok=True)
        target = _unique_path(trash_files, path.name)
        info_path = trash_info / f"{target.name}.trashinfo"
        shutil.move(str(path), str(target))
        deleted_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        info_path.write_text(
            f"[Trash Info]\nPath={path}\nDeletionDate={deleted_at}\n", encoding="utf-8"
        )
        return True
    except OSError:
        return False


def move_to_trash(path: str) -> None:
    """Send a file to the OS trash/recycle bin, or a fallback .trash/ folder."""
    path = Path(path)
    system = platform.system()
    ok = False
    if system == "Windows":
        ok = _trash_windows(path)
    elif system == "Darwin":
        ok = _trash_macos(path)
    else:
        ok = _trash_freedesktop(path)

    if not ok:
        fallback_dir = path.parent / ".trash"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_path(fallback_dir, path.name)
        try:
            shutil.move(str(path), str(target))
        except OSError as exc:
            raise FileOpError(f"could not trash {path}: {exc}") from exc


def permanent_delete(path: str) -> None:
    """Permanently delete a single file."""
    try:
        os.remove(path)
    except OSError as exc:
        raise FileOpError(f"could not delete {path}: {exc}") from exc


def permanent_delete_folder(path: str) -> None:
    """Permanently delete a folder and everything under it."""
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise FileOpError(f"could not delete folder {path}: {exc}") from exc


def move(path: str, destination_dir: str, new_name: Optional[str] = None) -> str:
    """Move a file into destination_dir, resolving name collisions. Returns the new path."""
    src = Path(path)
    dest_dir = Path(destination_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_path(dest_dir, new_name or src.name)
    try:
        shutil.move(str(src), str(target))
    except OSError as exc:
        raise FileOpError(f"could not move {path} to {destination_dir}: {exc}") from exc
    return str(target)


def _finish_processed_file(
    original: Path, tmp_path: Path, target_ext: str, delete_original: bool
) -> str:
    """Place a processed temp file next to original, deleting/keeping the original as asked."""
    if delete_original:
        try:
            original.unlink()
        except OSError as exc:
            raise FileOpError(f"could not remove original {original}: {exc}") from exc
        final = _unique_path(original.parent, f"{original.stem}{target_ext}")
    else:
        final = _unique_path(original.parent, f"{original.stem}_fixed{target_ext}")
    os.replace(tmp_path, final)
    return str(final)


def fixup(path: str, delete_original: bool, convert_to_mp4: bool) -> str:
    """Remux a file via ffmpeg stream copy, optionally to .mp4, optionally deleting the source."""
    original = Path(path)
    target_ext = ".mp4" if convert_to_mp4 else original.suffix
    tmp_path = original.parent / f".{original.stem}.{uuid.uuid4().hex}{target_ext}"
    _run_ffmpeg(["-i", str(original), "-c", "copy", str(tmp_path)])
    return _finish_processed_file(original, tmp_path, target_ext, delete_original)


def trim(path: str, start: str, end: str, delete_original: bool, convert_to_mp4: bool) -> str:
    """Cut [start, end] out of a file via ffmpeg stream copy into a new file."""
    original = Path(path)
    target_ext = ".mp4" if convert_to_mp4 else original.suffix
    tmp_path = original.parent / f".{original.stem}.{uuid.uuid4().hex}{target_ext}"
    _run_ffmpeg(
        ["-i", str(original), "-ss", str(start), "-to", str(end), "-c", "copy", str(tmp_path)]
    )
    return _finish_processed_file(original, tmp_path, target_ext, delete_original)


def split(path: str, at: str) -> tuple:
    """Split a file into two parts at timestamp `at`, both via ffmpeg stream copy."""
    original = Path(path)
    ext = original.suffix
    part1 = _unique_path(original.parent, f"{original.stem}_part1{ext}")
    part2 = _unique_path(original.parent, f"{original.stem}_part2{ext}")
    _run_ffmpeg(["-i", str(original), "-to", str(at), "-c", "copy", str(part1)])
    _run_ffmpeg(["-i", str(original), "-ss", str(at), "-c", "copy", str(part2)])
    return str(part1), str(part2)
