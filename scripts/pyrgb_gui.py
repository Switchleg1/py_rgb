"""PyInstaller entry point for the windowed build (pyrgbw.exe).

No console is attached, so this binary serves both the GUI and the background
daemon: ``pyrgbw.exe daemon`` is what the "start with Windows" entry runs.
"""

import multiprocessing
import sys

from pyrgb.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    argv = sys.argv[1:]
    if not argv:
        argv = ["gui"]
    sys.exit(main(argv))
