"""Write artifacts as a tree of files, or print them.

The writer knows nothing about Firebird: it groups statements by target path,
renders each file and writes it. Three operations:

* ``replace_tree`` — full dump: build the complete tree in a staging directory
  next to the target, then swap it in. The target is never left half-written.
* ``update_tree``  — targeted export: overwrite only the affected files.
* ``write_stdout`` — print every file with a ``-- ===== path =====`` header.

A tree written by fb-dump carries ``.fb-dump.toml`` (its layout). That file is
also the safety marker: a non-empty directory without it is not ours and is not
replaced or written into unless ``--force`` is given.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Iterable, TextIO

from .layout import MANIFEST
from .model import Artifact
from .render import Statement, render

Grouped = dict[str, list[Statement]]


class WriterError(Exception):
    """The output location cannot be used."""


def group(artifacts: Iterable[Artifact]) -> Grouped:
    """Statements per file, in insertion order (deterministic)."""
    grouped: Grouped = {}
    for art in artifacts:
        if art.sql and art.sql.strip():
            grouped.setdefault(art.path, []).append((art.sql, art.psql))
    return grouped


def write_stdout(grouped: Grouped, stream: TextIO | None = None) -> int:
    out = stream or sys.stdout
    for path, statements in grouped.items():
        print(f"-- ===== {path} =====", file=out)
        print(render(statements), file=out)
    return len(grouped)


def _check_target(out_dir: Path, force: bool) -> None:
    if out_dir.exists() and not out_dir.is_dir():
        raise WriterError(f"{out_dir} exists and is not a directory")
    if out_dir.is_dir() and any(out_dir.iterdir()) and not (out_dir / MANIFEST).exists() and not force:
        raise WriterError(f"{out_dir} is not empty and was not written by fb-dump (no {MANIFEST}); "
                          f"refusing to touch it — pass --force to override")


def _write_files(grouped: Grouped, root: Path) -> int:
    for rel, statements in grouped.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(statements), encoding="utf-8", newline="\n")
    return len(grouped)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def replace_tree(grouped: Grouped, out_dir: Path, manifest: str, force: bool = False) -> int:
    """Atomically replace ``out_dir`` with a tree containing exactly ``grouped`` + the manifest."""
    out_dir = Path(out_dir).resolve()
    parent = out_dir.parent
    staging = parent / f".{out_dir.name}.fb-dump-new"
    previous = parent / f".{out_dir.name}.fb-dump-old"

    # A crash between the two renames below leaves the previous tree under
    # `previous` and no `out_dir`: put it back before doing anything else.
    if previous.is_dir() and not out_dir.exists():
        previous.rename(out_dir)
    _check_target(out_dir, force)
    parent.mkdir(parents=True, exist_ok=True)
    _remove(staging)
    _remove(previous)

    staging.mkdir()
    count = _write_files(grouped, staging)
    (staging / MANIFEST).write_text(manifest, encoding="utf-8", newline="\n")

    if out_dir.exists():
        out_dir.rename(previous)
        try:
            staging.rename(out_dir)
        except Exception:
            previous.rename(out_dir)
            raise
        shutil.rmtree(previous, ignore_errors=True)
    else:
        staging.rename(out_dir)
    return count


def update_tree(grouped: Grouped, out_dir: Path, manifest: str, force: bool = False) -> int:
    """Overwrite only the files in ``grouped``; stale files are left alone (a full dump prunes them)."""
    out_dir = Path(out_dir).resolve()
    _check_target(out_dir, force)
    out_dir.mkdir(parents=True, exist_ok=True)
    count = _write_files(grouped, out_dir)
    marker = out_dir / MANIFEST
    if not marker.exists():
        marker.write_text(manifest, encoding="utf-8", newline="\n")
    return count
