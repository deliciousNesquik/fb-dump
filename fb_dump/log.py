"""Diagnostics on stderr.

stdout is reserved for data (object names in ``--list``, SQL when dumping without
``--out``), so everything here goes to stderr and the tool stays pipe-friendly.
No timestamps and no log files: that is the caller's job (``tee``, a scheduler,
a CI runner).
"""

from __future__ import annotations

import sys

QUIET, NORMAL, VERBOSE = 0, 1, 2

_level = NORMAL


def set_level(level: int) -> None:
    global _level
    _level = level


def _emit(tag: str, msg: str, min_level: int) -> None:
    if _level >= min_level:
        print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    _emit("ERROR", msg, QUIET)


def warning(msg: str) -> None:
    _emit("WARNING", msg, NORMAL)


def info(msg: str) -> None:
    _emit("INFO", msg, NORMAL)


def debug(msg: str) -> None:
    _emit("DEBUG", msg, VERBOSE)
