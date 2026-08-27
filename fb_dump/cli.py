"""Command line: argument parsing, mode selection, exit codes.

Modes
  full      no names, no --list: dump the whole schema (atomic: all or nothing)
  targeted  names given: dump those objects (best effort, exit 3 if incomplete)
  list      --list: print ``type<TAB>name`` per object

Destination: ``--out DIR`` writes a tree; without it SQL goes to stdout.

Exit codes
  0  success
  1  cannot connect / read / write (infrastructure)
  2  usage error (arguments, layout file, connection settings)
  3  incomplete: some objects could not be dumped or were not found
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__, categories, config, db, layout, log, selection, writer
from .model import Artifact, Context

EXIT_OK, EXIT_INFRA, EXIT_USAGE, EXIT_PARTIAL = 0, 1, 2, 3


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fb-dump",
        description="Dump a Firebird database schema as one .sql file per object.",
        epilog="Without --out the SQL is printed to stdout. Diagnostics always go to stderr.",
    )
    p.add_argument("names", nargs="*", metavar="NAME",
                   help="object names to export; none = full dump")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument("-q", "--quiet", action="store_true", help="errors only")
    verbosity.add_argument("-v", "--verbose", action="store_true", help="debug output")

    conn = p.add_argument_group("connection")
    conn.add_argument("-d", "--database", metavar="DSN",
                      help=f"database: alias, path or HOST:ALIAS_OR_PATH [env {config.ENV_DATABASE}]")
    conn.add_argument("-u", "--user", metavar="USER",
                      help=f"user name [env {config.ENV_USER}]; password only via env {config.ENV_PASSWORD}")
    conn.add_argument("-r", "--role", metavar="ROLE", help=f"SQL role to connect with [env {config.ENV_ROLE}]")
    conn.add_argument("--charset", metavar="CHARSET",
                      help=f"connection character set [env {config.ENV_CHARSET}, default {config.DEFAULT_CHARSET}]")
    conn.add_argument("--fallback-charset", metavar="CHARSET",
                      help="re-read a metadata collection through a second connection with this "
                           "charset when the primary one fails to decode it (mixed-encoding legacy databases)")

    out = p.add_argument_group("output")
    out.add_argument("-o", "--out", metavar="DIR", help="write the tree here instead of stdout")
    out.add_argument("--layout", metavar="PRESET|FILE",
                     help=f"directory layout: {', '.join(layout.PRESETS)} or a TOML file [default "
                          f"{layout.DEFAULT_PRESET}]; a targeted export into an existing tree uses the tree's "
                          f"own {layout.MANIFEST}, and an explicit --layout must match it")
    out.add_argument("--print-layout", action="store_true",
                     help="print the effective layout as TOML (a starting point for your own) and exit")
    out.add_argument("--allow-partial", action="store_true",
                     help="full dump: write the tree even if some objects could not be dumped")
    out.add_argument("--force", action="store_true",
                     help=f"full dump: replace a non-empty directory that has no {layout.MANIFEST} or that "
                          f"contains foreign entries; targeted export: write into such a directory")

    sel = p.add_argument_group("selection")
    sel.add_argument("--type", dest="type", choices=categories.TYPE_CHOICES, metavar="TYPE",
                     help="object type for NAME or as a filter for --list: " + ", ".join(categories.TYPE_CHOICES))
    sel.add_argument("--list", action="store_true", dest="list_mode",
                     help="print 'type<TAB>name' for every object and exit")
    return p


@dataclass
class Collector:
    """Per-object resilience: one object failing is logged, counted and skipped;
    the caller decides whether a partial result may be written."""

    artifacts: list[Artifact] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    _claims: dict[str, str] = field(default_factory=dict)  # casefolded path -> owner label

    def _claim(self, label: str, arts: list[Artifact]) -> bool:
        paths = {a.path for a in arts}
        for path in paths:
            owner = self._claims.get(path.casefold())
            if owner is not None and owner != label:
                log.warning(f"Skipping {label}: its file {path!r} collides with {owner} "
                            f"(same name in a case-insensitive file system)")
                return False
        for path in paths:
            self._claims[path.casefold()] = label
        return True

    def add(self, ctx: Context, cat: categories.Category, obj: Any) -> None:
        label = f"{cat.key} {cat.name_of(obj)}"
        try:
            arts = cat.emit(ctx, obj)
        except Exception as exc:  # noqa: BLE001 — one object must not sink the run
            log.warning(f"Skipping {label}: {exc}")
            self.failures.append(label)
            return
        if self._claim(label, arts):
            self.artifacts.extend(arts)
        else:
            self.failures.append(label)

    def add_section(self, label: str, producer: Callable[[], list[Artifact]]) -> None:
        try:
            arts = producer()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Skipping {label}: {exc}")
            self.failures.append(label)
            return
        if self._claim(label, arts):
            self.artifacts.extend(arts)
        else:
            self.failures.append(label)


def run_list(ctx: Context, type_alias: str | None) -> int:
    cats = [categories.CATEGORY_BY_ALIAS[type_alias]] if type_alias else list(categories.CATEGORIES)
    for cat in cats:
        for name in sorted(cat.name_of(o) for o in cat.objects(ctx.schema)):
            print(f"{cat.key}\t{name}")
    return EXIT_OK


def _preload(ctx: Context) -> None:
    """Load the schema-wide collections per-object code depends on *before* the
    per-object loop, so a collection that cannot be read at all surfaces as an
    infrastructure error (exit 1, charset hint) instead of one failure per object."""
    for name in ("constraints", "character_sets"):
        getattr(ctx.schema, name, None)
    ctx.grants  # noqa: B018 — builds the privilege index


def run_full(ctx: Context, out_dir: Path | None, allow_partial: bool, force: bool) -> int:
    log.info("Reading the schema...")
    _preload(ctx)
    col = Collector()
    col.add_section("database", lambda: categories.database_preamble(ctx))
    for cat in categories.CATEGORIES:
        for obj in sorted(cat.objects(ctx.schema), key=cat.name_of):  # sorted: stable diffs
            col.add(ctx, cat, obj)
    if ctx.grants.unmapped:
        log.debug(f"{len(ctx.grants.unmapped)} privilege(s) of unknown subject types were ignored")
    if left := ctx.grants.unconsumed():
        log.info(f"Privileges on {len(left)} object(s) not in the dump (system objects) were not written")

    if col.failures and not allow_partial:
        log.error(f"{len(col.failures)} object(s) could not be dumped; nothing written "
                  f"(--allow-partial writes the incomplete tree anyway)")
        return EXIT_PARTIAL

    grouped = writer.group(col.artifacts)
    if out_dir is None:
        count = writer.write_stdout(grouped)
        where = "printed"
    else:
        count = writer.replace_tree(grouped, out_dir, ctx.layout.to_toml(), force,
                                    owned=ctx.layout.top_level_entries())
        where = f"written to {out_dir}"
    log.info(f"Done: {count} file(s) {where}; {len(col.failures)} object(s) skipped")
    return EXIT_PARTIAL if col.failures else EXIT_OK


def run_targeted(ctx: Context, out_dir: Path | None, names: list[str], type_alias: str | None,
                 force: bool) -> int:
    resolved = selection.resolve(ctx.schema, names, type_alias)
    for name in resolved.missing:
        log.warning(f"Object not found: {name}")
    if resolved.matches:
        _preload(ctx)

    col = Collector()
    for cat, obj in resolved.matches:
        col.add(ctx, cat, obj)

    grouped = writer.group(col.artifacts)
    if not grouped:
        log.error("Nothing to write")
        return EXIT_PARTIAL
    if out_dir is None:
        count = writer.write_stdout(grouped)
        where = "printed"
    else:
        count = writer.update_tree(grouped, out_dir, ctx.layout.to_toml(), force)
        where = f"written to {out_dir}"
    log.info(f"Done: {count} file(s) {where}; {len(resolved.matches)} object(s), "
             f"{len(col.failures)} skipped, {len(resolved.missing)} not found")
    return EXIT_PARTIAL if (col.failures or resolved.missing) else EXIT_OK


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.list_mode and args.names:
        parser.error("--list cannot be combined with object names")
    if args.list_mode and args.out:
        parser.error("--list prints to stdout; --out makes no sense with it")
    if args.type and not args.names and not args.list_mode:
        parser.error("--type needs object names or --list")
    if args.allow_partial and (args.names or args.list_mode):
        parser.error("--allow-partial applies to the full dump only")
    if args.force and not args.out:
        parser.error("--force applies to --out only")


def _choose_layout(args: argparse.Namespace, out_dir: Path | None) -> layout.Layout:
    """Explicit --layout wins; otherwise a targeted export adopts the tree's own layout."""
    requested = layout.load(args.layout) if args.layout else None
    if args.names and out_dir is not None:
        recorded = layout.load_manifest(out_dir)
        if recorded is not None:
            if requested is not None and requested != recorded:
                raise layout.LayoutError(
                    f"--layout differs from the layout recorded in {out_dir / layout.MANIFEST}; "
                    f"drop --layout to reuse it, or run a full dump to rebuild the tree")
            return recorded
    return requested or layout.preset(layout.DEFAULT_PRESET)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    _validate(parser, args)
    log.set_level(log.QUIET if args.quiet else log.VERBOSE if args.verbose else log.NORMAL)

    out_dir = Path(args.out) if args.out else None
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure) and not sys.stdout.isatty():
        # Same bytes as --out: UTF-8 and LF, whatever the locale or platform.
        reconfigure(encoding="utf-8", newline="\n")
    try:
        lay = _choose_layout(args, out_dir)
    except layout.LayoutError as exc:
        log.error(str(exc))
        return EXIT_USAGE
    if args.print_layout:
        sys.stdout.write(lay.to_toml())
        return EXIT_OK

    try:
        settings = config.resolve(os.environ, database=args.database, user=args.user, role=args.role,
                                  charset=args.charset, fallback_charset=args.fallback_charset)
    except config.ConfigError as exc:
        log.error(str(exc))
        return EXIT_USAGE

    log.debug(f"database={settings.database} user={settings.user or '(driver default)'} "
              f"role={settings.role or '-'} charset={settings.charset} fallback={settings.fallback_charset or '-'}")

    con: Any = None
    schema: Any = None
    try:
        log.info("Connecting (read committed, record version, NO WAIT)...")
        con = db.connect(settings)
        schema = db.open_schema(settings, con)
        ctx = Context(schema, lay, db.dialect(con))
        if args.list_mode:
            return run_list(ctx, args.type)
        if args.names:
            return run_targeted(ctx, out_dir, args.names, args.type, args.force)
        return run_full(ctx, out_dir, args.allow_partial, args.force)
    except writer.WriterError as exc:
        log.error(str(exc))
        return EXIT_INFRA
    except UnicodeDecodeError as exc:
        log.error(f"Cannot decode metadata: {exc}")
        log.error(f"The database charset is probably not {settings.charset}: pass --charset "
                  f"(e.g. WIN1251), or --fallback-charset for mixed-encoding metadata")
        return EXIT_INFRA
    except Exception as exc:  # noqa: BLE001
        log.error(f"{type(exc).__name__}: {exc}")
        log.debug(traceback.format_exc())
        return EXIT_INFRA
    finally:
        if schema is not None and hasattr(schema, "close_fallback"):
            schema.close_fallback()
        if con is not None:
            try:
                con.close()
            except Exception as exc:  # noqa: BLE001 — the connection may already be gone
                log.debug(f"Could not close the connection cleanly: {exc}")
