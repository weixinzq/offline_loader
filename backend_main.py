"""Standalone background service entry point."""

import sys

from src.interfaces.cli import main


if __name__ == "__main__":
    sys.argv.insert(1, "serve")
    main()
