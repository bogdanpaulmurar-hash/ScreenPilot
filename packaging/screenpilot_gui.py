"""PyInstaller entry point for the portable desktop build."""

import sys

from screenpilot.ui.main import main

if __name__ == "__main__":
    sys.exit(main())
