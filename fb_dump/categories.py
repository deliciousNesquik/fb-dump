"""Object categories: how to enumerate each kind of object and how to turn one
object into the statements of *its* file.

One object — one file, and the file is the complete definition of the object:
DDL, constraints, comments and grants. Splitting "walk the collection"
(``objects``) from "DDL of one object" (``artifacts_for``) lets full, targeted
and list modes share the same code.

The DDL itself comes from firebird-lib's ``get_sql_for``; what is assembled here
mirrors the sections of ``Schema.get_metadata_ddl`` but per object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from firebird.lib.schema import escape_single_quotes

from .grants import quote_ident, render_database_grants, render_grants
from .layout import CATEGORY_KEYS
from .model import Artifact, Context

Producer = Callable[[Context, Any, str], Iterator[Artifact]]


def _is_sys(obj: Any) -> bool:
    flag = getattr(obj, "is_sys_object", None)
    return bool(flag()) if callable(flag) else False


@dataclass(frozen=True)
class Category:
    key: str                                            # "table" — also the layout key and the {type} placeholder
    aliases: tuple[str, ...]                            # accepted --type values
    objects: Callable[[Any], Iterable[Any]]             # user objects of this kind (system ones filtered)
    artifacts_for: Producer                             # (ctx, obj, path) -> statements of the object's file
    name_of: Callable[[Any], str] = field(default=lambda o: o.name)

    def emit(self, ctx: Context, obj: Any) -> list[Artifact]:
        path = ctx.layout.path_for(self.key, self.name_of(obj))
        return list(self.artifacts_for(ctx, obj, path))


# ---------------------------------------------------------------- collections
def _plain(attr: str) -> Callable[[Any], Iterator[Any]]:
    return lambda s: (o for o in getattr(s, attr) if not _is_sys(o))


def _external_functions(s: Any) -> Iterator[Any]:
    return (f for f in s.functions if f.is_external() and not _is_sys(f))


def _psql_functions(s: Any) -> Iterator[Any]:
    return (f for f in s.functions if not f.is_external() and not f.is_packaged() and not _is_sys(f))


def _procedures(s: Any) -> Iterator[Any]:
    return (p for p in s.procedures if not p.is_packaged() and not _is_sys(p))


def _indices(s: Any) -> Iterator[Any]:
    # Constraint-backed indices (PK/UNIQUE/FK enforcers) are created by their constraint.
    return (i for i in s.indices if not i.is_enforcer() and not _is_sys(i))


# ------------------------------------------------------------ shared pieces
def _comments(obj: Any, children: Iterable[Any] = ()) -> Iterator[str]:
    if getattr(obj, "description", None) is not None:
        yield obj.get_sql_for("comment")
    for child in children:
        if getattr(child, "description", None) is not None:
            yield child.get_sql_for("comment")


def _grants(ctx: Context, namespace: str, obj: Any) -> list[str]:
    privileges = ctx.grants.for_object(namespace, obj.name)
    if not privileges:
        return []
    return render_grants(privileges, namespace, obj.get_quoted_name(),
                         owner=getattr(obj, "owner_name", None), is_keyword=ctx.is_keyword)


def _plain_stmts(path: str, stmts: Iterable[str]) -> Iterator[Artifact]:
    for s in stmts:
        yield Artifact(path, s)


# ------------------------------------------------------------- per category
def _role(ctx: Context, r: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, r.get_sql_for("create"))
    yield from _plain_stmts(path, _comments(r))
    yield from _plain_stmts(path, _grants(ctx, "role", r))  # memberships: GRANT <role> TO <user>


def _collation(ctx: Context, c: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, c.get_sql_for("create"))
    yield from _plain_stmts(path, _comments(c))


def _external_function(ctx: Context, f: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, f.get_sql_for("declare"))
    yield from _plain_stmts(path, _comments(f))
    yield from _plain_stmts(path, _grants(ctx, "function", f))


def _generator(ctx: Context, g: Any, path: str) -> Iterator[Artifact]:
    # Only the definition: the current value is runtime state, not schema.
    yield Artifact(path, g.get_sql_for("create"))
    yield from _plain_stmts(path, _comments(g))
    yield from _plain_stmts(path, _grants(ctx, "generator", g))


def _exception(ctx: Context, e: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, e.get_sql_for("create_or_alter"))
    yield from _plain_stmts(path, _comments(e))
    yield from _plain_stmts(path, _grants(ctx, "exception", e))


def _domain(ctx: Context, d: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, d.get_sql_for("create"))
    yield from _plain_stmts(path, _comments(d))


def _table(ctx: Context, t: Any, path: str) -> Iterator[Artifact]:
    # CREATE TABLE without inline PK/UNIQUE, then every constraint as a named
    # ALTER TABLE ADD CONSTRAINT: a constraint change is one statement in the diff.
    # NOT NULL is part of the column definition and is not repeated.
    yield Artifact(path, t.get_sql_for("create", no_pk=True, no_unique=True))
    cons = list(t.constraints)
    ordered = (
        [c for c in cons if c.is_pkey()]
        + [c for c in cons if c.is_unique()]
        + [c for c in cons if c.is_check()]
        + [c for c in cons if c.is_fkey()]
    )
    for c in ordered:
        yield Artifact(path, c.get_sql_for("create"))
    yield from _plain_stmts(path, _comments(t, t.columns))
    yield from _plain_stmts(path, _grants(ctx, "relation", t))


def _index(ctx: Context, i: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, i.get_sql_for("create"))
    if i.is_inactive():
        yield Artifact(path, i.get_sql_for("deactivate"))
    yield from _plain_stmts(path, _comments(i))


def _function(ctx: Context, f: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, f.get_sql_for("create_or_alter"), psql=True)
    yield from _plain_stmts(path, _comments(f))
    yield from _plain_stmts(path, _grants(ctx, "function", f))


def _view(ctx: Context, v: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, v.get_sql_for("create_or_alter"))
    yield from _plain_stmts(path, _comments(v, v.columns))
    yield from _plain_stmts(path, _grants(ctx, "relation", v))


def _procedure(ctx: Context, p: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, p.get_sql_for("create_or_alter"), psql=True)
    yield from _plain_stmts(path, _comments(p, list(p.input_params) + list(p.output_params)))
    yield from _plain_stmts(path, _grants(ctx, "procedure", p))


def _package(ctx: Context, pkg: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, pkg.get_sql_for("create_or_alter"), psql=True)
    if getattr(pkg, "body", None):
        # There is no CREATE OR ALTER PACKAGE BODY; RECREATE is the idempotent form.
        yield Artifact(path, pkg.get_sql_for("recreate", body=True), psql=True)
    yield from _plain_stmts(path, _comments(pkg))
    yield from _plain_stmts(path, _grants(ctx, "package", pkg))


def _trigger(ctx: Context, tr: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, tr.get_sql_for("create_or_alter"), psql=True)
    yield from _plain_stmts(path, _comments(tr))


# --------------------------------------------------------------------- registry
CATEGORIES: tuple[Category, ...] = (
    Category("role", ("role",), _plain("roles"), _role),
    Category("collation", ("collation",), _plain("collations"), _collation),
    Category("external_function", ("external-function", "udf"), _external_functions, _external_function),
    Category("generator", ("generator", "sequence"), _plain("generators"), _generator),
    Category("exception", ("exception",), _plain("exceptions"), _exception),
    Category("domain", ("domain",), _plain("domains"), _domain),
    Category("table", ("table",), _plain("tables"), _table),
    Category("index", ("index",), _indices, _index),
    Category("function", ("function",), _psql_functions, _function),
    Category("view", ("view",), _plain("views"), _view),
    Category("procedure", ("procedure", "proc"), _procedures, _procedure),
    Category("package", ("package",), _plain("packages"), _package),
    Category("trigger", ("trigger",), _plain("triggers"), _trigger),
)
assert tuple(c.key for c in CATEGORIES) == CATEGORY_KEYS, "categories and layout keys out of sync"

CATEGORY_BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}
CATEGORY_BY_ALIAS: dict[str, Category] = {alias: c for c in CATEGORIES for alias in c.aliases}
CATEGORY_ORDER: dict[str, int] = {c.key: i for i, c in enumerate(CATEGORIES)}
TYPE_CHOICES: tuple[str, ...] = tuple(sorted(CATEGORY_BY_ALIAS))


# ------------------------------------------------------------ database file
def database_preamble(ctx: Context) -> list[Artifact]:
    """The database-level file: dialect, default character set, database comment,
    database-level (DDL) grants."""
    path = ctx.layout.database
    out = [Artifact(path, f"SET SQL DIALECT {ctx.dialect}")]
    try:
        charset = ctx.schema.default_character_set.name
    except Exception:  # noqa: BLE001
        charset = None
    if charset:
        out.append(Artifact(path, f"-- Default character set: {charset}"))
    description = getattr(ctx.schema, "description", None)
    if description:
        out.append(Artifact(path, f"COMMENT ON DATABASE IS '{escape_single_quotes(description)}'"))
    stmts, skipped = render_database_grants(ctx.grants.database, is_keyword=ctx.is_keyword)
    out += [Artifact(path, s) for s in stmts]
    if skipped:
        from . import log
        log.warning(f"{skipped} database-level privilege(s) of an unknown kind were not dumped")
    return out


__all__ = [
    "CATEGORIES", "CATEGORY_BY_ALIAS", "CATEGORY_BY_KEY", "CATEGORY_ORDER", "TYPE_CHOICES",
    "Category", "database_preamble", "quote_ident",
]
