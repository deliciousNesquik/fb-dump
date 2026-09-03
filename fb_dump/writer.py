"""Write artifacts as a tree of files, or print them.

The writer knows nothing about Firebird: it groups statements by target path,
renders each file and writes it. Three operations:

* ``replace_tree`` — full dump: build the complete tree in a staging directory,
  then swap it in. Normally the staging directory sits next to the target and the
  swap is a rename; when the target cannot be renamed (a mount point, the current
  working directory, a directory another process holds open) the tree is rebuilt
  in place: new files first, old entries removed last. Either way no half-written
  file is ever visible under the target's name.
* ``update_tree``  — targeted export: overwrite only the affected files.
* ``write_stdout`` — print every file with a ``-- ===== path =====`` header.
* ``write_script`` — print everything as one script ordered so it can be applied:
  statements are sorted by :class:`~fb_dump.model.Phase`, not grouped per object.

A tree written by fb-dump carries ``.fb-dump.toml`` (its layout). That file is
also the safety marker: a non-empty directory without it is not ours and is not
replaced or written into unless ``--force`` is given; and even a marked tree is
not replaced while it contains entries the layout does not account for (a ``.git``
directory, a README) unless ``--force`` is given.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, TextIO

from . import log
from .layout import MANIFEST, LayoutError, load_manifest
from .model import Artifact, Phase
from .render import Statement, render

Grouped = dict[str, list[Statement]]
_IN_PLACE_STAGING = ".fb-dump-new"


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


# ------------------------------------------------------------------ helpers
def _is_foreign(out_dir: Path) -> bool:
    return out_dir.is_dir() and any(out_dir.iterdir()) and not (out_dir / MANIFEST).exists()


def write_script(artifacts: Iterable[Artifact], stream: TextIO | None = None) -> int:
    """Print one script whose statement order can be executed against an empty database.

    Sorting is stable within a phase, so objects keep the order they were read in
    (categories, then names). Returns the number of statements written."""
    out = stream or sys.stdout
    items = [a for a in artifacts if a.sql and a.sql.strip()]
    items.sort(key=lambda a: a.phase)          # list.sort is stable
    text = render([(a.sql, a.psql) for a in items])
    print(text, file=out)
    return len(items)


def _check_target(out_dir: Path, force: bool) -> None:
    if out_dir.exists() and not out_dir.is_dir():
        raise WriterError(f"{out_dir} exists and is not a directory")
    if _is_foreign(out_dir) and not force:
        raise WriterError(f"{out_dir} is not empty and was not written by fb-dump (no {MANIFEST}); "
                          f"refusing to touch it — pass --force to override")


def _check_unowned_entries(out_dir: Path, owned: Iterable[str] | None, force: bool) -> None:
    """A marked tree may still contain things that are not ours (.git, README…).
    ``owned`` = None disables the check."""
    if owned is None or not out_dir.is_dir():
        return
    allowed = set(owned) | {MANIFEST, _IN_PLACE_STAGING}
    try:
        recorded = load_manifest(out_dir)          # entries of the layout the tree was written with
    except LayoutError:
        recorded = None
    if recorded is not None:
        allowed |= recorded.top_level_entries()
    strangers = sorted(
        e.name for e in out_dir.iterdir()
        if e.name not in allowed and (e.is_dir() or not e.name.lower().endswith(".sql"))
    )
    if strangers and not force:
        raise WriterError(f"{out_dir} contains entries a full dump would delete: {', '.join(strangers[:5])}"
                          f"{'…' if len(strangers) > 5 else ''} — move them out (keep the tree in its own "
                          f"directory) or pass --force")


def precheck_target(out_dir: Path, force: bool = False, owned: Iterable[str] | None = None) -> None:
    """Validate the output directory *before* the schema is read.

    Reading a large schema takes minutes; a target that can never be written to
    should be reported in milliseconds. The same checks run again inside
    ``replace_tree``/``update_tree`` — the directory may change while we read."""
    out_dir = Path(out_dir).resolve()
    _check_target(out_dir, force)
    _check_unowned_entries(out_dir, owned, force)


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


def _cwd_inside(out_dir: Path) -> bool:
    try:
        return Path.cwd().resolve().is_relative_to(out_dir)
    except (OSError, ValueError):
        return False


def _needs_in_place(out_dir: Path) -> bool:
    if not out_dir.is_dir():
        return False
    return os.path.ismount(out_dir) or _cwd_inside(out_dir) or not os.access(out_dir.parent, os.W_OK)


def _replace_in_place(grouped: Grouped, out_dir: Path, manifest: str, staging: Path | None = None) -> int:
    """Rebuild ``out_dir`` without renaming it: write everything into a staging
    directory (inside the tree unless one is supplied), drop the old entries, move
    the new ones up. Not a single atomic step, but no file is ever half-written."""
    own_staging = staging is None
    if staging is None:
        staging = out_dir / _IN_PLACE_STAGING
        _remove(staging)
        staging.mkdir(parents=True)
        try:
            count = _write_files(grouped, staging)
            (staging / MANIFEST).write_text(manifest, encoding="utf-8", newline="\n")
        except Exception:
            _remove(staging)
            raise
    else:
        count = len(grouped)
    for entry in list(out_dir.iterdir()):
        if entry != staging:
            _remove(entry)
    for entry in list(staging.iterdir()):
        entry.rename(out_dir / entry.name)
    staging.rmdir() if own_staging else _remove(staging)
    return count


def replace_tree(grouped: Grouped, out_dir: Path, manifest: str, force: bool = False,
                 owned: Iterable[str] | None = None) -> int:
    """Replace ``out_dir`` with a tree containing exactly ``grouped`` + the manifest.

    ``owned`` names the top-level entries the layout produces; anything else found
    in an existing tree stops the run unless ``force`` is set (None: no such check)."""
    out_dir = Path(out_dir).resolve()
    parent = out_dir.parent
    staging = parent / f".{out_dir.name}.fb-dump-new"
    previous = parent / f".{out_dir.name}.fb-dump-old"

    # A crash between the two renames below leaves the previous tree under
    # `previous` and no `out_dir`: put it back. Anything else there is garbage.
    if previous.is_dir() and not out_dir.exists() and (previous / MANIFEST).exists():
        previous.rename(out_dir)
    _check_target(out_dir, force)
    _check_unowned_entries(out_dir, owned, force)

    if _needs_in_place(out_dir):
        log.debug(f"{out_dir} cannot be renamed (mount point, current directory or read-only parent); "
                  f"rebuilding it in place")
        return _replace_in_place(grouped, out_dir, manifest)

    parent.mkdir(parents=True, exist_ok=True)
    _remove(staging)
    _remove(previous)
    staging.mkdir()
    try:
        count = _write_files(grouped, staging)
        (staging / MANIFEST).write_text(manifest, encoding="utf-8", newline="\n")
        if not out_dir.exists():
            staging.rename(out_dir)
            return count
        try:
            out_dir.rename(previous)
        except OSError as exc:
            # Windows: a directory that is somebody's current directory or holds an
            # open file cannot be renamed. Fall back to rebuilding it in place.
            log.debug(f"{out_dir} cannot be renamed ({exc}); rebuilding it in place")
            return _replace_in_place(grouped, out_dir, manifest, staging=staging)
        try:
            staging.rename(out_dir)
        except Exception:
            previous.rename(out_dir)
            raise
    except Exception:
        _remove(staging)
        raise
    shutil.rmtree(previous, ignore_errors=True)
    if previous.exists():
        log.warning(f"Could not remove the previous tree {previous}; delete it by hand")
    return count


def update_tree(grouped: Grouped, out_dir: Path, manifest: str, force: bool = False) -> int:
    """Overwrite only the files in ``grouped``; stale files are left alone (a full dump prunes them).

    The manifest is added to a directory that was empty or new. A foreign directory
    entered with ``--force`` is left unmarked, so a later full dump still has to be
    forced before it may delete anything there."""
    out_dir = Path(out_dir).resolve()
    _check_target(out_dir, force)
    foreign = _is_foreign(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    count = _write_files(grouped, out_dir)
    marker = out_dir / MANIFEST
    if foreign:
        log.warning(f"{out_dir} was not written by fb-dump; files were added but the directory "
                    f"stays unmarked (no {MANIFEST})")
    elif not marker.exists():
        marker.write_text(manifest, encoding="utf-8", newline="\n")
    return count
