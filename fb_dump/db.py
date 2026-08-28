"""Connecting to Firebird, and the transaction policy.

Every transaction is **read committed + record version + NO WAIT**. WAIT could
hang the process on a lock conflict; NO WAIT fails fast. firebird-lib's
``Schema.bind()`` builds its own read transaction as
``tpb(Isolation.READ_COMMITTED_RECORD_VERSION, access_mode=READ)`` — with the
driver default ``lock_timeout=-1`` (WAIT). The ``tpb`` symbol the schema module
resolves is replaced here so that transaction is always built with
``lock_timeout=0`` (NO WAIT), keeping its isolation and access mode.

``--isolation`` picks what that transaction guarantees:

* ``read-committed`` (default) — read committed + record version. The dump is
  *not* a point-in-time snapshot: DDL committed while it runs may show up in one
  collection and not in another. In exchange, a read-only read-committed
  transaction does not hold back garbage collection.
* ``read-consistency`` (Firebird 4+) — read committed, but every statement sees a
  stable view. firebird-lib loads a collection with one query, so each collection
  is internally consistent while garbage collection is held back only per
  statement, not for the whole run.
* ``snapshot`` — one consistent view of the catalog for the whole run. Firebird's
  SNAPSHOT (concurrency) is pure MVCC: it blocks neither readers nor writers
  (that is SNAPSHOT TABLE STABILITY, a different level). The cost is that record
  versions cannot be collected while the dump runs.

Two levels the driver offers are deliberately not exposed: SNAPSHOT TABLE STABILITY
(``SERIALIZABLE`` in the driver) takes table locks and would block every writer
touching the same system tables, and read committed *without* record version fails
on the first record another transaction is modifying — under NO WAIT that is a
spurious error with nothing gained.

Known trade-offs, deliberately accepted: the patches below are process-global and
happen at import time; fb-dump is a CLI, and ``Schema.bind()`` offers no hook for
its transaction.
"""

from __future__ import annotations

from typing import Any, Callable

import firebird.lib.schema as _fb_schema
from firebird.driver import Isolation
from firebird.lib.schema import COLUMN_TYPES, INTEGRAL_SUBTYPES, Domain, FieldSubType, FieldType, SchemaItem
from firebird.driver import TraAccessMode, driver_config
from firebird.driver import connect as _connect
from firebird.driver import tpb as _driver_tpb

from . import log
from .config import Settings


# Isolation requested on the command line; None keeps whatever the caller asked for.
_ISOLATION: Isolation | None = None

_ISOLATIONS: dict[str, Isolation] = {
    "read-committed": Isolation.READ_COMMITTED_RECORD_VERSION,
    "read-consistency": Isolation.READ_COMMITTED_READ_CONSISTENCY,   # Firebird 4+
    "snapshot": Isolation.SNAPSHOT,
}
_MIN_ENGINE: dict[str, float] = {"read-consistency": 4.0}


def set_isolation(name: str | None) -> None:
    """Choose the isolation of the transaction firebird-lib opens for the catalog."""
    global _ISOLATION
    if name is None:
        _ISOLATION = None
        return
    try:
        _ISOLATION = _ISOLATIONS[name]
    except KeyError:
        raise ValueError(f"unknown isolation {name!r}") from None


def _nowait_tpb(isolation, lock_timeout: int = -1, access_mode=TraAccessMode.WRITE) -> bytes:  # noqa: ANN001, ARG001
    return _driver_tpb(_ISOLATION or isolation, lock_timeout=0, access_mode=access_mode)


_fb_schema.tpb = _nowait_tpb

# Materialise metadata BLOBs instead of streaming them: firebird-lib concatenates a
# PSQL source as a string, and a source above the default 64 KiB threshold comes
# back as a BlobReader, making get_sql_for fail with "can only concatenate str".
driver_config.stream_blob_threshold.value = 256 * 1024 * 1024


# Firebird 4 data types firebird-lib 2.0 has no SQL spelling for: without these a
# domain/column/parameter of such a type raises KeyError inside get_sql_for.
_FB4_TYPES: dict[Any, str] = {
    FieldType.INT128: "INT128",
    FieldType.DEC16: "DECFLOAT(16)",
    FieldType.DEC34: "DECFLOAT(34)",
    FieldType.TIME_TZ: "TIME WITH TIME ZONE",
    FieldType.TIMESTAMP_TZ: "TIMESTAMP WITH TIME ZONE",
    FieldType.TIME_TZ_EX: "TIME WITH TIME ZONE",
    FieldType.TIMESTAMP_TZ_EX: "TIMESTAMP WITH TIME ZONE",
}
for _ft, _sql in _FB4_TYPES.items():
    COLUMN_TYPES.setdefault(_ft, _sql)

_lib_datatype = Domain.datatype


def _datatype(self: Any) -> str:
    """Domain.datatype with Firebird 4 types; every column/parameter type funnels through it."""
    ft = self.field_type
    if ft in _FB4_TYPES:
        if ft is FieldType.INT128 and self.precision and self.sub_type in (FieldSubType.NUMERIC, FieldSubType.DECIMAL):
            return f"{INTEGRAL_SUBTYPES[self.sub_type]}({self.precision}, {-self.scale})"
        return _FB4_TYPES[ft]
    return _lib_datatype.fget(self)  # type: ignore[union-attr]


Domain.datatype = property(_datatype)  # type: ignore[assignment]


def _is_sys_object(self: Any) -> bool:
    """RDB$SYSTEM_FLAG may be NULL in databases that started life on InterBase; NULL means user."""
    return (self._attributes.get("RDB$SYSTEM_FLAG") or 0) > 0


SchemaItem.is_sys_object = _is_sys_object  # type: ignore[method-assign]


def connect(settings: Settings, charset: str | None = None):  # noqa: ANN201
    kwargs: dict[str, Any] = {"charset": charset or settings.charset}
    if settings.user:
        kwargs["user"] = settings.user
    if settings.password:
        kwargs["password"] = settings.password
    if settings.role:
        kwargs["role"] = settings.role
    return _connect(settings.database, **kwargs)


def dialect(con: Any) -> int:
    try:
        return int(getattr(con.info, "sql_dialect", 3))
    except Exception:  # noqa: BLE001
        return 3


# Schema collections fb-dump walks. Legacy databases sometimes hold metadata in
# MIXED encodings: some rows are raw single-byte text (readable only under that
# charset), others contain Unicode outside it (readable only under UTF8). One
# connection charset cannot cover both, and firebird-lib loads a collection with
# a single query — one bad row breaks the whole collection.
_COLLECTIONS = frozenset({
    "collations", "character_sets", "domains", "generators", "exceptions",
    "functions", "procedures", "triggers", "views", "tables", "indices",
    "roles", "packages", "privileges", "dependencies", "constraints",
})


class ResilientSchema:
    """firebird-lib ``Schema`` wrapper: a collection that fails to load on the
    primary connection is re-read through a second connection opened with
    ``--fallback-charset``. The second connection is opened lazily, only when the
    primary actually fails, so for healthy databases nothing changes.

    The fallback rows are read in another transaction, at another moment, under
    another charset — a warning says so whenever it happens."""

    def __init__(self, primary: Any, fallback_factory: Callable[[], Any], fallback_charset: str) -> None:
        self._primary = primary
        self._fallback_factory = fallback_factory
        self._fallback_charset = fallback_charset
        self._fallback_con: Any = None
        self._fallback_schema: Any = None
        self._cache: dict[str, list[Any]] = {}

    def _fallback(self) -> Any:
        if self._fallback_schema is None:
            self._fallback_con = self._fallback_factory()
            self._fallback_schema = self._fallback_con.schema
        return self._fallback_schema

    def _collection(self, name: str) -> list[Any]:
        if name in self._cache:
            return self._cache[name]
        try:
            items = list(getattr(self._primary, name))
        except Exception as exc:  # noqa: BLE001 — transliteration / decode errors
            try:
                items = list(getattr(self._fallback(), name))
            except Exception:  # noqa: BLE001 — fallback failed too: report the original error
                raise exc
            log.warning(f"Collection '{name}' could not be read on the primary connection "
                        f"({type(exc).__name__}: {exc}); read through the fallback connection "
                        f"({self._fallback_charset})")
        self._cache[name] = items
        return items

    def __getattr__(self, name: str) -> Any:
        if name in _COLLECTIONS:
            return self._collection(name)
        return getattr(self._primary, name)

    def close_fallback(self) -> None:
        if self._fallback_con is not None:
            try:
                self._fallback_con.close()
            except Exception as exc:  # noqa: BLE001
                log.debug(f"Could not close the fallback connection: {exc}")
            self._fallback_con = None
            self._fallback_schema = None


def open_schema(settings: Settings, con: Any) -> Any:
    """``con.schema``, wrapped in ``ResilientSchema`` when a fallback charset is configured.

    The isolation is applied here, before the schema binds its read transaction."""
    required = _MIN_ENGINE.get(settings.isolation)
    if required is not None:
        engine = float(getattr(con.info, "engine_version", 0) or 0)
        if engine < required:
            raise RuntimeError(f"--isolation {settings.isolation} needs Firebird {required:.0f}+, "
                               f"this server reports {engine or 'an unknown version'}")
    set_isolation(settings.isolation)
    if not settings.fallback_charset:
        return con.schema
    fb = settings.fallback_charset
    return ResilientSchema(con.schema, lambda: connect(settings, fb), fb)
