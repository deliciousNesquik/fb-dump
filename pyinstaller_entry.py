"""PyInstaller entry point: builds the CLI into a self-contained executable.

The binary does not need Python; it still needs the Firebird client library
(``fbclient``), which ``firebird-driver`` loads at run time (see README).
"""

import sys

from fb_dump.cli import main

if __name__ == "__main__":
    sys.exit(main())
