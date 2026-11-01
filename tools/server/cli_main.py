"""Server-root CLI shim; deliberately supports the system Python 3.6."""
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    try:
        release = (root / 'current').resolve(strict=True)
        if not (root / 'current').is_symlink() or release.parent != (root / 'releases').resolve():
            raise ValueError('current must point to a direct child of releases')
        entry = release / 'cli_main.py'
        if not entry.is_file() or entry.resolve().parent != release:
            raise ValueError('release CLI is missing or outside the release')
        python = root / '.venv/bin/python'
        os.execv(str(python), [str(python), '-B', str(entry), *sys.argv[1:]])
    except (OSError, ValueError) as error:
        sys.exit('Cannot launch current CLI: ' + str(error))


if __name__ == '__main__':
    main()
