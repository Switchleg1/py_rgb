"""PyInstaller entry point for the console build (pyrgb.exe)."""

import multiprocessing
import sys

from pyrgb.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
