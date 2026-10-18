"""Allow `python -m showtime_watcher ...`."""

import sys

from showtime_watcher.cli import main

if __name__ == "__main__":
    sys.exit(main())
