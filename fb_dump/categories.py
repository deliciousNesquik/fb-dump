"""Object categories: how to enumerate each kind of object and how to turn one
object into the statements of *its* file.

One object — one file, and the file is the complete definition of the object:
DDL, constraints, comments and grants. Splitting "walk the collection"
(``objects``) from "DDL of one object" (``artifacts_for``) lets full, targeted
and list modes share the same code.

The DDL comes from firebird-lib's ``get_sql_for``; what firebird-lib 2.0 does not
know about Firebird 4/5 is added here: ``GENERATED ALWAYS`` identities and their
``INCREMENT BY``, sequence increments, ``DETERMINISTIC`` functions, partial-index
``WHERE``, ``ON COMMIT`` of temporary tables, ``SQL SECURITY`` of tables,
external-engine (UDR) routines, DDL-trigger events, and comments on PSQL functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

from firebird.lib.schema import escape_single_quotes

from . import log
from .grants import IsKeyword, quote_ident, render_database_grants, render_grants
from .layout import CATEGORY_KEYS
from .model import Artifact, Context, Phase

Producer = Callable[[Context, Any, str], Iterator[Artifact]]

_TRIGGER_TYPE_MASK = 0x3 << 13          # RDB$TRIGGER_TYPE bits that say DML / DB / DDL
_DDL_TIME_BIT = 1                       # DDL triggers: bit 0 = AFTER
_DDL_ANY_MASK = 0x7FFFFFFFFFFFFFFF & ~_TRIGGER_TYPE_MASK & ~_DDL_TIME_BIT
_RELATION_GTT_PRESERVE, _RELATION_GTT_DELETE = 4, 5   # RDB$RELATION_TYPE
_IDENTITY_ALWAYS = 0                    # RDB$IDENTITY_TYPE (1 = BY DEFAULT)


def _is_sys(obj: Any) -> bool:
    flag = getattr(obj, "is_sys_object", None)
    return bool(flag()) if callable(flag) else False


def _attr(obj: Any, name: str) -> Any:
    """Raw catalog column, for what firebird-lib loads but does not expose."""
    attrs = getattr(obj, "_attributes", None)
    return attrs.get(name) if isinstance(attrs, dict) else None


@dataclass(frozen=True)
class Category:
    key: str                                            # "table" — also the layout key and the {type} placeholder
    aliases: tuple[str, ...]                            # accepted --type values (the key itself always is)
    objects: Callable[[Any], Iterable[Any]]             # user objects of this kind (system ones filtered)
    artifacts_for: Producer                             # (ctx, obj, path) -> statements of the object's file
    name_of: Callable[[Any], str] = field(default=lambda o: o.name)
    stubbable: bool = False                             # a routine whose header can be created before its body

    def emit(self, ctx: Context, obj: Any, *, stub: bool = False) -> list[Artifact]:
        """Statements of the object's file. ``stub`` additionally prepends a
        header-with-empty-body statement — used only by the single-script output,
        where it lets routines be created in any order (see ``Phase``)."""
        path = ctx.layout.path_for(self.key, self.name_of(obj))
        out: list[Artifact] = []
        if stub and self.stubbable and _external_routine(obj) is None:
            out.append(Artifact(path, _routine_stub(obj), psql=True, phase=Phase.ROUTINE_STUB))
        out += list(self.artifacts_for(ctx, obj, path))
        return out


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
def _qname(obj: Any, is_keyword: IsKeyword = None) -> str:
    """Quoted object name. firebird-lib's ``get_quoted_name`` does not double an
    embedded ``"``; ours does, so hand-built statements go through this."""
    return quote_ident(obj.name, is_keyword)


def _comment_of(obj: Any, kind: str, is_keyword: IsKeyword = None) -> str:
    return f"COMMENT ON {kind} {_qname(obj, is_keyword)} IS '{escape_single_quotes(obj.description)}'"


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
    return render_grants(privileges, namespace, _qname(obj, ctx.is_keyword),
                         owner=getattr(obj, "owner_name", None), is_keyword=ctx.is_keyword)


def _plain_stmts(path: str, stmts: Iterable[str], phase: Phase) -> Iterator[Artifact]:
    for s in stmts:
        yield Artifact(path, s, phase=phase)


def _quote_segments(sql: str, names: Iterable[str], is_keyword: IsKeyword) -> str:
    """firebird-lib joins index/constraint column lists unquoted; quote them when needed."""
    names = list(names)
    quoted = [quote_ident(n, is_keyword) for n in names]
    if quoted == names:
        return sql
    return sql.replace(f"({','.join(names)})", f"({', '.join(quoted)})", 1)


def _external_routine(obj: Any) -> tuple[str, str] | None:
    """(engine, entry point) of a Firebird 3+ external-engine routine, else None."""
    engine = _attr(obj, "RDB$ENGINE_NAME")
    if not engine:
        return None
    return str(engine).strip(), str(_attr(obj, "RDB$ENTRYPOINT") or "").strip()


def _external_clause(engine: str, entrypoint: str) -> str:
    return f"EXTERNAL NAME '{escape_single_quotes(entrypoint)}' ENGINE {quote_ident(engine)}"


def _routine_stub(obj: Any) -> str:
    """Header with an empty body: ``CREATE OR ALTER PROCEDURE p (…) RETURNS (…) AS BEGIN END``.

    firebird-lib builds exactly this for its own two-pass script order, so the
    body it substitutes is the one Firebird accepts for the object kind."""
    create = obj.get_sql_for("create", no_code=True)
    return "CREATE OR ALTER" + create[len("CREATE"):]


def _routine_header(obj: Any) -> str:
    """``CREATE OR ALTER <kind> name (params) RETURNS …`` without the body."""
    create = obj.get_sql_for("create", no_code=True)
    header = create.rpartition("\nAS\n")[0] or create
    return "CREATE OR ALTER" + header[len("CREATE"):]


# ------------------------------------------------------------- per category
def _role(ctx: Context, r: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, r.get_sql_for("create"), phase=Phase.ROLE)
    yield from _plain_stmts(path, _comments(r), Phase.COMMENT)
    yield from _plain_stmts(path, _grants(ctx, "role", r), Phase.GRANT)  # GRANT <role> TO <user>


def _collation(ctx: Context, c: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, c.get_sql_for("create"), phase=Phase.COLLATION)
    yield from _plain_stmts(path, _comments(c), Phase.COMMENT)


def _external_function(ctx: Context, f: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, f.get_sql_for("declare"), phase=Phase.UDF)
    yield from _plain_stmts(path, _comments(f), Phase.COMMENT)
    yield from _plain_stmts(path, _grants(ctx, "function", f), Phase.GRANT)


def _generator(ctx: Context, g: Any, path: str) -> Iterator[Artifact]:
    # Definition only: START WITH / INCREMENT BY as declared (Firebird 4+ keeps them
    # in the catalog); the current value is runtime state and is never written.
    params: dict[str, Any] = {}
    start = getattr(g, "inital_value", None)
    increment = getattr(g, "increment", None)
    if start:
        params["value"] = start
    if increment not in (None, 0, 1):
        params["increment"] = increment
    yield Artifact(path, g.get_sql_for("create", **params), phase=Phase.GENERATOR)
    yield from _plain_stmts(path, _comments(g), Phase.COMMENT)
    yield from _plain_stmts(path, _grants(ctx, "generator", g), Phase.GRANT)


def _exception(ctx: Context, e: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, e.get_sql_for("create_or_alter"), phase=Phase.EXCEPTION)
    yield from _plain_stmts(path, _comments(e), Phase.COMMENT)
    yield from _plain_stmts(path, _grants(ctx, "exception", e), Phase.GRANT)


def _domain(ctx: Context, d: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, d.get_sql_for("create"), phase=Phase.DOMAIN)
    yield from _plain_stmts(path, _comments(d), Phase.COMMENT)


def _table(ctx: Context, t: Any, path: str) -> Iterator[Artifact]:
    # CREATE TABLE without inline PK/UNIQUE, then every constraint as a named
    # ALTER TABLE ADD CONSTRAINT: a constraint change is one statement in the diff.
    # NOT NULL is part of the column definition and is not repeated.
    create = t.get_sql_for("create", no_pk=True, no_unique=True)
    relation_type = _attr(t, "RDB$RELATION_TYPE")
    if relation_type == _RELATION_GTT_PRESERVE:
        create += "\nON COMMIT PRESERVE ROWS"
    elif relation_type == _RELATION_GTT_DELETE:
        create += "\nON COMMIT DELETE ROWS"
    yield Artifact(path, create, phase=Phase.TABLE)

    tname = _qname(t, ctx.is_keyword)
    for col in t.columns:
        # firebird-lib always writes GENERATED BY DEFAULT and never the increment.
        if getattr(col, "is_identity", lambda: False)():
            if _attr(col, "RDB$IDENTITY_TYPE") == _IDENTITY_ALWAYS:
                yield Artifact(path, f"ALTER TABLE {tname} ALTER COLUMN {col.get_quoted_name()} SET GENERATED ALWAYS",
                              phase=Phase.TABLE_ALTER)
            gen = getattr(col, "generator", None)
            increment = getattr(gen, "increment", None)
            if increment not in (None, 0, 1):
                yield Artifact(path, f"ALTER TABLE {tname} ALTER COLUMN {col.get_quoted_name()} SET INCREMENT BY {increment}",
                              phase=Phase.TABLE_ALTER)
    security = _attr(t, "RDB$SQL_SECURITY")
    if security is not None:
        yield Artifact(path, f"ALTER TABLE {tname} ALTER SQL SECURITY {'DEFINER' if security else 'INVOKER'}",
                       phase=Phase.TABLE_ALTER)

    # Sorted by name inside each kind: the catalog query has no ORDER BY, so two
    # foreign keys on one table could otherwise swap places between dumps.
    cons = sorted(t.constraints, key=lambda c: c.name or "")
    ordered = (
        [(Phase.KEY, c) for c in cons if c.is_pkey()]
        + [(Phase.KEY, c) for c in cons if c.is_unique()]
        + [(Phase.CHECK, c) for c in cons if c.is_check()]
        + [(Phase.FOREIGN_KEY, c) for c in cons if c.is_fkey()]
    )
    for phase, c in ordered:
        sql = c.get_sql_for("create")
        index = getattr(c, "index", None)
        if index is not None and not c.is_check():
            sql = _quote_segments(sql, index.segment_names, ctx.is_keyword)
            partner = getattr(c, "partner_constraint", None) if c.is_fkey() else None
            if partner is not None and getattr(partner, "index", None) is not None:
                sql = _quote_segments(sql, partner.index.segment_names, ctx.is_keyword)
        yield Artifact(path, sql, phase=phase)
    yield from _plain_stmts(path, _comments(t, t.columns), Phase.COMMENT)
    yield from _plain_stmts(path, _grants(ctx, "relation", t), Phase.GRANT)


def _index(ctx: Context, i: Any, path: str) -> Iterator[Artifact]:
    sql = i.get_sql_for("create")
    if not i.is_expression():
        sql = _quote_segments(sql, i.segment_names, ctx.is_keyword)
    condition = _attr(i, "RDB$CONDITION_SOURCE")          # Firebird 5 partial index
    if condition:
        condition = str(condition).strip()
        sql += "\n" + (condition if condition.upper().startswith("WHERE") else f"WHERE {condition}")
    yield Artifact(path, sql, phase=Phase.INDEX)
    if i.is_inactive():
        yield Artifact(path, i.get_sql_for("deactivate"), phase=Phase.INDEX_STATE)
    yield from _plain_stmts(path, _comments(i), Phase.COMMENT)


def _function(ctx: Context, f: Any, path: str) -> Iterator[Artifact]:
    external = _external_routine(f)
    if external is not None:
        # A UDR routine is a complete definition that depends on nothing but its
        # parameter types, so it belongs with the stubs: views and other routines
        # emitted later may call it.
        yield Artifact(path, f"{_routine_header(f)}\n{_external_clause(*external)}", psql=True,
                       phase=Phase.ROUTINE_STUB)
    else:
        sql = f.get_sql_for("create_or_alter")
        if getattr(f, "deterministic_flag", None):
            sql = sql.replace("\nAS\n", " DETERMINISTIC\nAS\n", 1)
        yield Artifact(path, sql, psql=True, phase=Phase.ROUTINE)
    # firebird-lib registers the 'comment' action for external UDFs only.
    if getattr(f, "description", None) is not None:
        yield Artifact(path, _comment_of(f, "FUNCTION", ctx.is_keyword), phase=Phase.COMMENT)
    yield from _plain_stmts(path, _grants(ctx, "function", f), Phase.GRANT)


def _view(ctx: Context, v: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, v.get_sql_for("create_or_alter"), phase=Phase.VIEW)
    yield from _plain_stmts(path, _comments(v, v.columns), Phase.COMMENT)
    yield from _plain_stmts(path, _grants(ctx, "relation", v), Phase.GRANT)


def _procedure(ctx: Context, p: Any, path: str) -> Iterator[Artifact]:
    external = _external_routine(p)
    if external is not None:
        yield Artifact(path, f"{_routine_header(p)}\n{_external_clause(*external)}", psql=True,
                       phase=Phase.ROUTINE_STUB)
    else:
        yield Artifact(path, p.get_sql_for("create_or_alter"), psql=True, phase=Phase.ROUTINE)
    yield from _plain_stmts(path, _comments(p, list(p.input_params) + list(p.output_params)), Phase.COMMENT)
    yield from _plain_stmts(path, _grants(ctx, "procedure", p), Phase.GRANT)


def _package(ctx: Context, pkg: Any, path: str) -> Iterator[Artifact]:
    yield Artifact(path, pkg.get_sql_for("create_or_alter"), psql=True, phase=Phase.ROUTINE_STUB)
    if getattr(pkg, "body", None):
        # There is no CREATE OR ALTER PACKAGE BODY; RECREATE is the idempotent form.
        yield Artifact(path, pkg.get_sql_for("recreate", body=True), psql=True, phase=Phase.ROUTINE)
    yield from _plain_stmts(path, _comments(pkg), Phase.COMMENT)
    yield from _plain_stmts(path, _grants(ctx, "package", pkg), Phase.GRANT)


def _ddl_trigger_event(raw_type: int) -> str:
    """Decode a DDL trigger's RDB$TRIGGER_TYPE: bit 0 is the time, bits 1..47 are
    one flag per DDL event (firebird-lib reads the mask as a single code)."""
    from firebird.lib.schema import DDLTrigger

    time = "AFTER" if raw_type & _DDL_TIME_BIT else "BEFORE"
    events = raw_type & ~_TRIGGER_TYPE_MASK & ~_DDL_TIME_BIT
    if events == _DDL_ANY_MASK:
        return f"{time} ANY DDL STATEMENT"
    if not events:
        raise ValueError(f"DDL trigger type {raw_type} names no event")
    names: list[str] = []
    for bit in range(1, 64):
        if events >> bit & 1:
            try:
                names.append(DDLTrigger(bit).name.replace("_", " "))
            except ValueError:
                names.append(f"<unknown DDL event {bit}>")
    return f"{time} {' OR '.join(names)}"


def _trigger(ctx: Context, tr: Any, path: str) -> Iterator[Artifact]:
    external = _external_routine(tr)
    is_ddl = bool(getattr(tr, "is_ddl_trigger", lambda: False)())
    is_db = bool(getattr(tr, "is_db_trigger", lambda: False)())
    # A DDL or database trigger fires on the statements that follow it, so in an
    # apply order it must come after every definition.
    phase = Phase.TRIGGER_DDL if (is_ddl or is_db) else Phase.TRIGGER
    if external is None and not is_ddl:
        yield Artifact(path, tr.get_sql_for("create_or_alter"), psql=True, phase=phase)
    else:
        # Same shape as firebird-lib's CREATE TRIGGER, with the pieces it gets wrong.
        header = f"CREATE OR ALTER TRIGGER {_qname(tr, ctx.is_keyword)}"
        relation = getattr(tr, "relation", None)
        if relation is not None:
            header += f" FOR {_qname(relation, ctx.is_keyword)}"
        header += f" {'ACTIVE' if tr.active else 'INACTIVE'}\n"
        event = _ddl_trigger_event(int(_attr(tr, "RDB$TRIGGER_TYPE"))) if is_ddl else tr.get_type_as_string()
        header += f"{event} POSITION {tr.sequence or 0}\n"
        body = _external_clause(*external) if external is not None else (tr.source or "")
        if not body.strip():
            raise ValueError("trigger has no body and no external engine")
        yield Artifact(path, header + body, psql=True, phase=phase)
    yield from _plain_stmts(path, _comments(tr), Phase.COMMENT)


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
    Category("function", ("function",), _psql_functions, _function, stubbable=True),
    Category("view", ("view",), _plain("views"), _view),
    Category("procedure", ("procedure", "proc"), _procedures, _procedure, stubbable=True),
    Category("package", ("package",), _plain("packages"), _package),
    Category("trigger", ("trigger",), _plain("triggers"), _trigger),
)
assert tuple(c.key for c in CATEGORIES) == CATEGORY_KEYS, "categories and layout keys out of sync"

CATEGORY_BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}
CATEGORY_BY_ALIAS: dict[str, Category] = {
    **{c.key: c for c in CATEGORIES},
    **{alias: c for c in CATEGORIES for alias in c.aliases},
}
CATEGORY_ORDER: dict[str, int] = {c.key: i for i, c in enumerate(CATEGORIES)}
TYPE_CHOICES: tuple[str, ...] = tuple(sorted(CATEGORY_BY_ALIAS))


# ------------------------------------------------------------ database file
def database_preamble(ctx: Context) -> list[Artifact]:
    """The database-level file: dialect, default character set, database comment,
    character-set adjustments, database-level (DDL) grants and memberships of
    system roles such as RDB$ADMIN."""
    schema = ctx.schema
    path = ctx.layout.database
    out = [Artifact(path, f"SET SQL DIALECT {ctx.dialect}", phase=Phase.DIALECT)]
    try:
        charset = schema.default_character_set.name
    except Exception:  # noqa: BLE001
        charset = None
    if charset:
        out.append(Artifact(path, f"-- Default character set: {charset}", phase=Phase.DIALECT))
    description = getattr(schema, "description", None)
    if description:
        out.append(Artifact(path, f"COMMENT ON DATABASE IS '{escape_single_quotes(description)}'",
                            phase=Phase.COMMENT))

    for cs in sorted(getattr(schema, "character_sets", ()) or (), key=lambda c: c.name):
        default = getattr(cs, "default_collation", None)
        if default is not None and default.name != cs.name:
            out.append(Artifact(path, cs.get_sql_for("alter", collation=default), phase=Phase.CHARACTER_SET))
        if getattr(cs, "description", None) is not None:
            out.append(Artifact(path, cs.get_sql_for("comment"), phase=Phase.COMMENT))

    owner = getattr(schema, "owner_name", None)
    stmts, skipped = render_database_grants(ctx.grants.database, owner=owner, is_keyword=ctx.is_keyword)
    out += [Artifact(path, s, phase=Phase.GRANT) for s in stmts]
    if skipped:
        log.warning(f"{skipped} database-level privilege(s) of an unknown kind were not dumped")

    # System roles are not dumped as objects, but who holds them is configuration.
    for role in sorted((r for r in schema.roles if _is_sys(r)), key=lambda r: r.name):
        out += [Artifact(path, s, phase=Phase.GRANT) for s in _grants(ctx, "role", role)]
    return out


__all__ = [
    "CATEGORIES", "CATEGORY_BY_ALIAS", "CATEGORY_BY_KEY", "CATEGORY_ORDER", "TYPE_CHOICES",
    "Category", "Phase", "database_preamble", "quote_ident",
]
