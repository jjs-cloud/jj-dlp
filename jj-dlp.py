#!/usr/bin/env python3
"""
jj-dlp  —  multi-site stream recorder with MenuWorks-style curses dashboard
Wrapper script
"""

import sys
import os

# Ensure the parent directory is in sys.path so the jj_dlp package can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jj_dlp.main import main

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        # In case the crash logger is not initialized yet or fails, print to stderr
        print(f"CRITICAL ERROR: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
