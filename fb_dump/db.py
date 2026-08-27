"""Connecting to Firebird, and the transaction policy.

Every transaction is **read committed + record version + NO WAIT**. WAIT could
hang the process on a lock conflict; NO WAIT fails fast. firebird-lib's
``Schema.bind()`` builds its own read transaction as
``tpb(Isolation.READ_COMMITTED_RECORD_VERSION, access_mode=READ)`` — with the
driver default ``lock_timeout=-1`` (WAIT). The ``tpb`` symbol the schema module
resolves is replaced here so that transaction is always built with
``lock_timeout=0`` (NO WAIT), keeping its isolation and access mode.

Known trade-offs, deliberately accepted:

* Both patches below are process-global and happen at import time; fb-dump is a
  CLI, and ``Schema.bind()`` offers no hook for its transaction.
* READ COMMITTED means the dump is not a point-in-time snapshot: DDL committed
  while the dump runs may show up in some collections and not in others.
"""

from __future__ import annotations

from typing import Any, Callable

import firebird.lib.schema as _fb_schema
from firebird.driver import TraAccessMode, driver_config
from firebird.driver import connect as _connect
from firebird.driver import tpb as _driver_tpb

from . import log
from .config import Settings


def _nowait_tpb(isolation, lock_timeout: int = -1, access_mode=TraAccessMode.WRITE) -> bytes:  # noqa: ANN001, ARG001
    return _driver_tpb(isolation, lock_timeout=0, access_mode=access_mode)


_fb_schema.tpb = _nowait_tpb

# Materialise metadata BLOBs instead of streaming them: firebird-lib concatenates a
# PSQL source as a string, and a source above the default 64 KiB threshold comes
# back as a BlobReader, making get_sql_for fail with "can only concatenate str".
driver_config.stream_blob_threshold.value = 256 * 1024 * 1024


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
    """``con.schema``, wrapped in ``ResilientSchema`` when a fallback charset is configured."""
    if not settings.fallback_charset:
        return con.schema
    fb = settings.fallback_charset
    return ResilientSchema(con.schema, lambda: connect(settings, fb), fb)
