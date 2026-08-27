"""Entry point for ``python -m fb_dump``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
