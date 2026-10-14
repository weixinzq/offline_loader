"""Optional lightweight command-line entry point."""

import sys


if __name__ == "__main__":
    if len(sys.argv) > 1:
        from src.cli_jobs import main
    else:
        from src.app import main
    main()
