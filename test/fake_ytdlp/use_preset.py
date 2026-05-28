#!/usr/bin/env python3
"""
use_preset.py  —  Activate a fake_ytdlp scenario preset

Usage:
    python use_preset.py <preset_name>

Available presets (in presets/ directory):
    normal_stream
    stall
    ffmpeg_timestamp_discontinuity
    ffmpeg_packet_corrupt
    crash
    slow_start

Example:
    python use_preset.py stall
    # Now run jj-dlp — it will call fake_ytdlp which reads the stall scenario.

    python use_preset.py normal_stream
    # Back to baseline.
"""

import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRESETS_DIR = os.path.join(_HERE, "presets")
_ACTIVE_CONF = os.path.join(_HERE, "fake_ytdlp.conf")


def list_presets():
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(_PRESETS_DIR)
        if f.endswith(".conf")
    )


def main():
    presets = list_presets()

    if len(sys.argv) < 2:
        print("Usage: python use_preset.py <preset_name>\n")
        print("Available presets:")
        for p in presets:
            print(f"  {p}")
        sys.exit(0)

    name = sys.argv[1].strip()
    src = os.path.join(_PRESETS_DIR, f"{name}.conf")

    if not os.path.isfile(src):
        print(f"ERROR: preset '{name}' not found in {_PRESETS_DIR}/")
        print("Available presets:")
        for p in presets:
            print(f"  {p}")
        sys.exit(1)

    # Backup current conf
    backup = _ACTIVE_CONF + ".bak"
    if os.path.isfile(_ACTIVE_CONF):
        shutil.copy2(_ACTIVE_CONF, backup)

    shutil.copy2(src, _ACTIVE_CONF)
    print(f"✓  Activated preset: {name}")
    print(f"   (previous config saved to fake_ytdlp.conf.bak)")


if __name__ == "__main__":
    main()
