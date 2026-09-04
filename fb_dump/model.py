"""Shared data types.

``Artifact`` is the unit of output: one SQL statement addressed to one file. It
decouples "which object produces which SQL" (categories) from "how files are
written" (writer). ``Context`` is what every extractor receives: the bound
schema, the connection dialect, the output layout and a lazily built privilege
index.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from .grants import GrantIndex
from .layout import Layout


class Phase(IntEnum):
    """Where a statement belongs in an *apply* order.

    The tree ignores this — a file keeps its object's own order (definition,
    comments, grants). It matters only for the single-script output, which is
    sorted by phase so the result can be executed against an empty database.

    The order encodes what depends on what: a foreign key needs both tables and
    the referenced primary key; a routine body needs everything it calls, which
    is why every routine is first created as a header with an empty body
    (``ROUTINE_STUB``) — that breaks call cycles without a dependency graph.
    DDL and database triggers come last because, once created, they fire on
    every statement that follows.
    """

    DIALECT = 0          # SET SQL DIALECT, the charset comment
    ROLE = 1             # grantees everywhere, depend on nothing
    COLLATION = 2
    CHARACTER_SET = 3    # ALTER CHARACTER SET ... SET DEFAULT COLLATION
    UDF = 4              # DECLARE EXTERNAL FUNCTION
    GENERATOR = 5
    EXCEPTION = 6
    DOMAIN = 7           # column types depend on domains
    TABLE = 8            # emitted without PK/UNIQUE, so tables never reference each other
    TABLE_ALTER = 9      # identity, SQL SECURITY
    KEY = 10             # PRIMARY KEY, UNIQUE — a foreign key needs them
    ROUTINE_STUB = 11    # headers with empty bodies, package headers, external routines
    CHECK = 12           # a CHECK expression may call a routine, hence after the stubs
    FOREIGN_KEY = 13     # after every table and key exists
    INDEX = 14           # an expression index may call a routine too
    INDEX_STATE = 15     # ALTER INDEX ... INACTIVE
    VIEW = 16            # may use routines, hence after the stubs
    ROUTINE = 17         # real bodies, any order — every callee exists as a stub
    TRIGGER = 18         # bodies may call routines
    TRIGGER_DDL = 19     # fires on subsequent DDL, so dead last among definitions
    COMMENT = 20
    GRANT = 21           # grantees may be roles, users or PSQL objects


@dataclass(frozen=True)
class Artifact:
    path: str            # tree-relative path, e.g. "07_TABLES/ACCOUNT.sql"
    sql: str             # one statement without terminator; lines starting with "--" are emitted verbatim
    psql: bool = False   # True -> wrapped in a SET TERM block (see render.py)
    phase: Phase = Phase.TABLE   # only the single-script output sorts by this


class Context:
    def __init__(self, schema: Any, layout: Layout, dialect: int = 3) -> None:
        self.schema = schema
        self.layout = layout
        self.dialect = dialect
        self._grants: GrantIndex | None = None

    @property
    def grants(self) -> GrantIndex:
        """Privileges indexed by subject; built on first use (one catalog read)."""
        if self._grants is None:
            self._grants = GrantIndex(self.schema.privileges)
        return self._grants

    def is_keyword(self, ident: str) -> bool:
        fn = getattr(self.schema, "is_keyword", None)
        return bool(fn(ident)) if callable(fn) else False
