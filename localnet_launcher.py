"""Точка входа для PyInstaller (см. build.spec)."""

import sys

from localnet.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
